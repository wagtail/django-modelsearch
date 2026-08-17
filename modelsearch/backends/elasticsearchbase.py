import json

from collections import OrderedDict
from copy import deepcopy
from datetime import time
from urllib.parse import urlparse

from django.db import DEFAULT_DB_ALIAS, models
from django.db.models import Subquery
from django.db.models.sql import Query
from django.db.models.sql.constants import MULTI, SINGLE
from django.utils.crypto import get_random_string

from modelsearch.backends.base import (
    BaseIndex,
    BaseSearchBackend,
    BaseSearchQueryCompiler,
    BaseSearchResults,
    FilterFieldError,
    get_model_root,
)
from modelsearch.index import (
    AutocompleteField,
    FilterField,
    Indexed,
    RelatedFields,
    SearchField,
    class_is_indexed,
    get_indexed_models,
)
from modelsearch.query import And, Boost, Fuzzy, MatchAll, Not, Or, Phrase, PlainText
from modelsearch.utils import deep_update


class Field:
    def __init__(self, field_name, boost=1):
        self.field_name = field_name
        self.boost = boost

    @property
    def field_name_with_boost(self):
        if self.boost == 1:
            return self.field_name
        else:
            return f"{self.field_name}^{self.boost}"


class ElasticsearchBaseMapping:
    """
    Handles the translation from Django ORM model instances to the data structure stored by Elasticsearch
    for a given model class.
    """

    type_map = {
        "AutoField": "integer",
        "SmallAutoField": "integer",
        "BigAutoField": "long",
        "BinaryField": "binary",
        "BooleanField": "boolean",
        "CharField": "string",
        "CommaSeparatedIntegerField": "string",
        "DateField": "date",
        "DateTimeField": "date",
        "DecimalField": "double",
        "FileField": "string",
        "FilePathField": "string",
        "FloatField": "double",
        "IntegerField": "integer",
        "BigIntegerField": "long",
        "IPAddressField": "string",
        "GenericIPAddressField": "string",
        "NullBooleanField": "boolean",
        "PositiveIntegerField": "integer",
        "PositiveSmallIntegerField": "integer",
        "PositiveBigIntegerField": "long",
        "SlugField": "string",
        "SmallIntegerField": "integer",
        "TextField": "string",
        "TimeField": "keyword",
        "URLField": "string",
    }

    edgengram_analyzer_config = {
        "analyzer": "edgengram_analyzer",
        "search_analyzer": "standard",
    }

    def __init__(self, model):
        self.model = model

    def get_parent(self):
        """Return the mapping for the indexed model that this model inherits from, or None if there isn't one."""
        for base in self.model.__bases__:
            if issubclass(base, Indexed) and issubclass(base, models.Model):
                return type(self)(base)

    def get_field_column_name(self, field):
        """
        Given a search_fields entry, return the name to be used for that field in the Elasticsearch document.
        This is formed as follows:
        - For all types other than RelatedFields, take the attribute name of the field
          (which generally matches the field name, but for a `foo` ForeignKey is `foo_id`)
        - for RelatedFields, take the given relation name
        - If the field/relation is defined on a subclass rather than the base model that directly inherits from
          Indexed, prefix this with "{app_label}_{model_name}__" (to avoid clashes where multiple subclasses have
          fields with the same name but different types)
        - for FilterField, suffix this with "_filter"
        - for AutocompleteField, suffix this with "_edgengrams"
        """
        root_model = get_model_root(self.model)
        definition_model = field.get_definition_model(self.model)

        if definition_model != root_model:
            prefix = (
                definition_model._meta.app_label.lower()
                + "_"
                + definition_model.__name__.lower()
                + "__"
            )
        else:
            prefix = ""

        if isinstance(field, FilterField):
            return prefix + field.get_attname(self.model) + "_filter"
        elif isinstance(field, AutocompleteField):
            return prefix + field.get_attname(self.model) + "_edgengrams"
        elif isinstance(field, SearchField):
            return prefix + field.get_attname(self.model)
        elif isinstance(field, RelatedFields):
            return prefix + field.field_name

    def get_boost_field_name(self, boost):
        """
        Returns the field name that contains all content with a given boost value.
        """
        # replace . with _ to avoid issues with . in field names
        boost = str(float(boost)).replace(".", "_")
        return f"_all_text_boost_{boost}"

    def get_content_type(self):
        """
        Returns the content type as a string for the model.

        For example: "wagtailcore.Page"
                     "myapp.MyModel"
        """
        return self.model._meta.app_label + "." + self.model.__name__

    def get_all_content_types(self):
        """
        Returns all the content type strings that apply to this model.
        This includes the models' content type and all concrete ancestor
        models that inherit from Indexed.

        For example: ["myapp.MyPageModel", "wagtailcore.Page"]
                     ["myapp.MyModel"]
        """
        # Add our content type
        content_types = [self.get_content_type()]

        # Add all ancestor classes content types as well
        ancestor = self.get_parent()
        while ancestor:
            content_types.append(ancestor.get_content_type())
            ancestor = ancestor.get_parent()

        return content_types

    def get_field_mapping(self, field):
        """
        Given a search_fields entry, return the (key, value) pair to include in the mapping definition.
        """
        if isinstance(field, RelatedFields):
            mapping = {"type": "nested", "properties": {}}
            nested_model = field.get_field(self.model).related_model
            nested_mapping = type(self)(nested_model)

            for sub_field in field.fields:
                sub_field_name, sub_field_mapping = nested_mapping.get_field_mapping(
                    sub_field
                )
                mapping["properties"][sub_field_name] = sub_field_mapping

            return self.get_field_column_name(field), mapping
        else:
            mapping = {"type": self.type_map.get(field.get_type(self.model), "string")}

            if isinstance(field, SearchField):
                if mapping["type"] == "string":
                    mapping["type"] = "text"

                if field.boost:
                    mapping["boost"] = field.boost

                mapping["include_in_all"] = True

            if isinstance(field, AutocompleteField):
                mapping["type"] = "text"
                mapping.update(self.edgengram_analyzer_config)

            elif isinstance(field, FilterField):
                if mapping["type"] == "string":
                    mapping["type"] = "keyword"

            if "es_extra" in field.kwargs:
                for key, value in field.kwargs["es_extra"].items():
                    mapping[key] = value

            return self.get_field_column_name(field), mapping

    def get_mapping(self):
        """
        Return the mapping definition for this model. This is a JSON-ish object with a single top-level property
        `properties` containing the definition for each property that will be stored in Elasticsearch, namely:
        - pk - the primary key of the model instance
        - _django_content_type - the list of content type strings for this model and all superclasses
        - _edgengrams - all content from fields with an `AutocompleteField`, indexed for partial word matching
        - one property for each field defined in search_fields, as defined by `get_field_mapping` and with the name
          determined by `get_field_column_name`
        - _all_text - a catch-all field containing the content of all SearchFields, for searching across all fields
        - _all_text_boost_{n} for each distinct boost value that appears in search_fields, containing all content
          with that boost value
        """
        # Make field list
        fields = {
            "pk": {"type": "keyword", "store": True},
            "_django_content_type": {"type": "keyword"},
            "_edgengrams": {"type": "text"},
        }
        fields["_edgengrams"].update(self.edgengram_analyzer_config)

        for field in self.model.get_search_fields():
            key, val = self.get_field_mapping(field)
            fields[key] = val

        # Add _all_text field
        fields["_all_text"] = {"type": "text"}

        unique_boosts = set()

        # Replace {"include_in_all": true} with {"copy_to": ["_all_text", "_all_text_boost_2"]}
        def replace_include_in_all(properties):
            for field_mapping in properties.values():
                if "include_in_all" in field_mapping:
                    if field_mapping["include_in_all"]:
                        field_mapping["copy_to"] = "_all_text"

                        if "boost" in field_mapping:
                            # added to unique_boosts to avoid duplicate fields, or cases like 2.0 and 2
                            unique_boosts.add(field_mapping["boost"])
                            field_mapping["copy_to"] = [
                                field_mapping["copy_to"],
                                self.get_boost_field_name(field_mapping["boost"]),
                            ]
                            del field_mapping["boost"]

                    del field_mapping["include_in_all"]

                if field_mapping["type"] == "nested":
                    replace_include_in_all(field_mapping["properties"])

        replace_include_in_all(fields)
        for boost in unique_boosts:
            fields[self.get_boost_field_name(boost)] = {"type": "text"}

        return {
            "properties": fields,
        }

    def get_document_id(self, obj):
        """
        Return the ID to uniquely identify this document within the index.
        """
        return str(obj.pk)

    def _clean_value(self, value):
        """
        Convert a value to a JSON-serializable type.
        """
        if isinstance(value, time):
            return value.isoformat()
        return value

    def _get_document_field_data(self, field, obj):
        """
        Return the data that an individual search_fields entry should contribute to the search document, as
        a tuple of:
        - field name
        - field value
        - list of values to be added to the _edgengrams field
        """
        value = self._clean_value(field.get_value(obj))
        name = self.get_field_column_name(field)

        if isinstance(field, RelatedFields):
            if isinstance(value, (models.Manager, models.QuerySet)):
                nested_docs = []
                edgengrams = []

                for nested_obj in value.all():
                    nested_doc, extra_edgengrams = self._get_nested_document(
                        field.fields, nested_obj
                    )
                    nested_docs.append(nested_doc)
                    edgengrams.extend(extra_edgengrams)

                return name, nested_docs, edgengrams
            elif isinstance(value, models.Model):
                nested_doc, edgengrams = self._get_nested_document(field.fields, value)
                return name, nested_doc, edgengrams
            elif value is None:
                return name, None, []
            else:
                raise ValueError(f"Unexpected value for RelatedFields: {value}")

        elif isinstance(field, FilterField):
            if isinstance(value, (models.Manager, models.QuerySet)):
                value = list(value.values_list("pk", flat=True))
            elif isinstance(value, models.Model):
                value = value.pk
            elif isinstance(value, (list, tuple)):
                value = [
                    item.pk if isinstance(item, models.Model) else item
                    for item in value
                ]
            return name, value, []

        elif isinstance(field, AutocompleteField):
            return name, value, [value]

        elif isinstance(field, SearchField):
            return name, value, []

        else:
            raise ValueError(f"Unexpected search_fields entry: {field}")

    def _get_nested_document(self, fields, obj):
        """
        Get the document for a related model instance that is to be nested into the main document.
        """
        doc = {}
        edgengrams = []
        model = type(obj)
        mapping = type(self)(model)

        for field in fields:
            name, value, field_edgengrams = mapping._get_document_field_data(field, obj)
            doc[name] = value
            edgengrams.extend(field_edgengrams)

        return doc, edgengrams

    def get_document(self, obj):
        """
        Get the document to be pushed to Elasticsearch for the given model instance.
        """
        doc = {"pk": str(obj.pk), "_django_content_type": self.get_all_content_types()}
        edgengrams = []
        for field in self.model.get_search_fields():
            name, value, field_edgengrams = self._get_document_field_data(field, obj)
            doc[name] = value
            edgengrams.extend(field_edgengrams)

        # Add partials to document
        doc["_edgengrams"] = edgengrams

        return doc

    def __repr__(self):
        return f"<ElasticsearchMapping: {self.model.__name__}>"


class ElasticsearchBaseIndex(BaseIndex):
    def __init__(self, backend, name):
        super().__init__(backend)
        self.es = backend.es
        self.mapping_class = backend.mapping_class
        self.name = name

    def put(self):
        if self.backend.use_new_elasticsearch_api:
            self.es.indices.create(index=self.name, **self.backend.settings)
        else:
            self.es.indices.create(self.name, self.backend.settings)

    def delete(self):
        try:
            if self.backend.use_new_elasticsearch_api:
                self.es.indices.delete(index=self.name)
            else:
                self.es.indices.delete(self.name)
        except self.backend.NotFoundError:
            pass

    def refresh(self):
        if self.backend.use_new_elasticsearch_api:
            self.es.indices.refresh(index=self.name)
        else:
            self.es.indices.refresh(self.name)

    def exists(self):
        return self.es.indices.exists(self.name)

    def is_alias(self):
        return self.es.indices.exists_alias(name=self.name)

    def aliased_indices(self):
        """
        If this index object represents an alias (which appear the same in the
        Elasticsearch API), this method can be used to fetch the list of indices
        the alias points to.

        Use the is_alias method if you need to find out if this an alias. This
        returns an empty list if called on an index.
        """
        return [
            self.backend.index_class(self.backend, index_name)
            for index_name in self.es.indices.get_alias(name=self.name).keys()
        ]

    def put_alias(self, name):
        """
        Creates a new alias to this index. If the alias already exists it will
        be repointed to this index.
        """
        self.es.indices.put_alias(name=name, index=self.name)

    def add_model(self, model):
        # Get mapping
        mapping = self.mapping_class(model)

        # Put mapping
        self.es.indices.put_mapping(index=self.name, body=mapping.get_mapping())

    def add_item(self, item):
        # Make sure the object can be indexed
        if not class_is_indexed(item.__class__):
            return

        # Get mapping
        mapping = self.mapping_class(item.__class__)

        # Add document to index
        if self.backend.use_new_elasticsearch_api:
            self.es.index(
                index=self.name,
                document=mapping.get_document(item),
                id=mapping.get_document_id(item),
            )
        else:
            self.es.index(
                self.name, mapping.get_document(item), id=mapping.get_document_id(item)
            )

    def add_items(self, model, items):
        if not class_is_indexed(model):
            return

        # Get mapping
        mapping = self.mapping_class(model)

        # Create list of actions
        actions = []
        for item in items:
            # Create the action
            action = {"_id": mapping.get_document_id(item)}
            action.update(mapping.get_document(item))
            actions.append(action)

        # Run the actions
        if actions:
            self.backend.bulk(self.es, actions, index=self.name)

    def delete_item(self, item):
        # Make sure the object can be indexed
        if not class_is_indexed(item.__class__):
            return

        # Get mapping
        mapping = self.mapping_class(item.__class__)

        # Delete document
        try:
            self.es.delete(index=self.name, id=mapping.get_document_id(item))
        except self.backend.NotFoundError:
            pass  # Document doesn't exist, ignore this exception

    def process_mptree_path_updated(self, model, old_path, new_path):
        """
        Handle a treebeard.mp_tree.path_updated signal by updating the path and depth fields
        for all affected nodes in the index.
        """
        self.refresh()  # Ensure that any pending changes are visible before we run the update_by_query

        mapping = self.mapping_class(model)
        search_fields = model.get_search_fields()
        path_search_fields = [
            field for field in search_fields if field.field_name == "path"
        ]

        # We need a FilterField("path") in search_fields, otherwise we can't perform the query to
        # do the update - in which case we won't be able to do any tree-based filtering anyhow, so
        # the whole point of keeping path updated becomes moot.
        if not any(isinstance(field, FilterField) for field in path_search_fields):
            return

        script_lines = []
        script_params = {
            "old": old_path,
            "new": new_path,
        }
        for search_field in path_search_fields:
            field_name = mapping.get_field_column_name(search_field)
            script_lines.append(
                f"ctx._source['{field_name}'] = params.new + ctx._source['{field_name}'].substring(params.old.length());"
            )

        if len(old_path) != len(new_path):
            # we also need to update the depth field
            depth_search_fields = [
                field for field in search_fields if field.field_name == "depth"
            ]
            if depth_search_fields:
                script_params["depth_delta"] = (
                    len(new_path) - len(old_path)
                ) // model.steplen
                for search_field in depth_search_fields:
                    field_name = mapping.get_field_column_name(search_field)
                    script_lines.append(
                        f"ctx._source['{field_name}'] = ctx._source['{field_name}'] + params.depth_delta;"
                    )

        self.es.update_by_query(
            index=self.name,
            body={
                "query": {"prefix": {"path_filter": old_path}},
                "script": {
                    "source": "\n".join(script_lines),
                    "lang": "painless",
                    "params": script_params,
                },
            },
            conflicts="proceed",
        )

    def process_nstree_gap_altered(self, model, tree_id, start_index, offset):
        """
        Handle a treebeard.ns_tree.gap_altered signal by updating the left and right values
        for all affected nodes in the index.
        """
        self.refresh()  # Ensure that any pending changes are visible before we run the update_by_query

        mapping = self.mapping_class(model)
        search_fields = model.get_search_fields()
        tree_id_search_fields = [
            field for field in search_fields if field.field_name == "tree_id"
        ]
        lft_search_fields = [
            field for field in search_fields if field.field_name == "lft"
        ]
        rgt_search_fields = [
            field for field in search_fields if field.field_name == "rgt"
        ]

        # search_fields must contain a FilterField for each of tree_id, lft and rgt, otherwise we
        # can't perform the query to do the update - in which case we won't be able to do any
        # tree-based filtering anyhow, so keeping the index in sync becomes moot.
        if not any(isinstance(field, FilterField) for field in tree_id_search_fields):
            return
        if not any(isinstance(field, FilterField) for field in lft_search_fields):
            return
        if not any(isinstance(field, FilterField) for field in rgt_search_fields):
            return

        query = {
            "bool": {
                "filter": [
                    {"term": {"tree_id_filter": tree_id}},
                    {"range": {"rgt_filter": {"gte": start_index}}},
                ]
            }
        }

        script_params = {
            "start_index": start_index,
            "offset": offset,
        }

        script_lines = []
        for search_field in rgt_search_fields:
            rgt_name = mapping.get_field_column_name(search_field)
            script_lines.append(f"ctx._source['{rgt_name}'] += params.offset;")
        for search_field in lft_search_fields:
            lft_name = mapping.get_field_column_name(search_field)
            script_lines.append(
                f"if (ctx._source['{lft_name}'] >= params.start_index) ctx._source['{lft_name}'] += params.offset;"
            )

        self.es.update_by_query(
            index=self.name,
            body={
                "query": query,
                "script": {
                    "source": "\n".join(script_lines),
                    "lang": "painless",
                    "params": script_params,
                },
            },
            conflicts="proceed",
        )

    def process_nstree_subtree_moved(
        self, model, tree_id, lft, rgt, target_tree_id, index_offset, depth_offset
    ):
        """
        Handle a treebeard.ns_tree.subtree_moved signal by updating the left and right values
        for all affected nodes in the index.
        """
        self.refresh()  # Ensure that any pending changes are visible before we run the update_by_query

        mapping = self.mapping_class(model)
        search_fields = model.get_search_fields()
        tree_id_search_fields = [
            field for field in search_fields if field.field_name == "tree_id"
        ]
        lft_search_fields = [
            field for field in search_fields if field.field_name == "lft"
        ]
        rgt_search_fields = [
            field for field in search_fields if field.field_name == "rgt"
        ]
        depth_search_fields = [
            field for field in search_fields if field.field_name == "depth"
        ]

        # search_fields must contain a FilterField for each of tree_id, lft and rgt, otherwise we
        # can't perform the query to do the update - in which case we won't be able to do any
        # tree-based filtering anyhow, so keeping the index in sync becomes moot.
        if not any(isinstance(field, FilterField) for field in tree_id_search_fields):
            return
        if not any(isinstance(field, FilterField) for field in lft_search_fields):
            return
        if not any(isinstance(field, FilterField) for field in rgt_search_fields):
            return

        query = {
            "bool": {
                "filter": [
                    {"term": {"tree_id_filter": tree_id}},
                    {"range": {"lft_filter": {"gte": lft}}},
                    {"range": {"rgt_filter": {"gte": rgt}}},
                ]
            }
        }

        script_params = {
            "index_offset": index_offset,
        }
        if depth_search_fields:
            script_params["depth_offset"] = depth_offset

        script_lines = []
        for search_field in lft_search_fields:
            lft_name = mapping.get_field_column_name(search_field)
            script_lines.append(f"ctx._source['{lft_name}'] += params.index_offset;")
        for search_field in rgt_search_fields:
            rgt_name = mapping.get_field_column_name(search_field)
            script_lines.append(f"ctx._source['{rgt_name}'] += params.index_offset;")
        for search_field in depth_search_fields:
            depth_name = mapping.get_field_column_name(search_field)
            script_lines.append(f"ctx._source['{depth_name}'] += params.depth_offset;")
        if target_tree_id != tree_id:
            script_params["target_tree_id"] = target_tree_id
            for search_field in tree_id_search_fields:
                tree_id_name = mapping.get_field_column_name(search_field)
                script_lines.append(
                    f"ctx._source['{tree_id_name}'] = params.target_tree_id;"
                )

        self.es.update_by_query(
            index=self.name,
            body={
                "query": query,
                "script": {
                    "source": "\n".join(script_lines),
                    "lang": "painless",
                    "params": script_params,
                },
            },
            conflicts="proceed",
        )

    def process_nstree_tree_ids_incremented(self, model, min_tree_id):
        """
        Handle a treebeard.ns_tree.tree_ids_incremented signal by updating the tree_id values
        for all affected nodes in the index.
        """
        self.refresh()  # Ensure that any pending changes are visible before we run the update_by_query

        mapping = self.mapping_class(model)
        search_fields = model.get_search_fields()
        tree_id_search_fields = [
            field for field in search_fields if field.field_name == "tree_id"
        ]

        # search_fields must contain a FilterField for tree_id, otherwise we
        # can't perform the query to do the update - in which case we won't be able to do any
        # tree-based filtering anyhow, so keeping the index in sync becomes moot.
        if not any(isinstance(field, FilterField) for field in tree_id_search_fields):
            return

        query = {
            "range": {"tree_id_filter": {"gte": min_tree_id}},
        }

        script_lines = []
        for search_field in tree_id_search_fields:
            tree_id_name = mapping.get_field_column_name(search_field)
            script_lines.append(f"ctx._source['{tree_id_name}'] += 1;")

        self.es.update_by_query(
            index=self.name,
            body={
                "query": query,
                "script": {
                    "source": "\n".join(script_lines),
                    "lang": "painless",
                },
            },
            conflicts="proceed",
        )

    def reset(self):
        # Delete old index
        self.delete()

        # Create new index
        self.put()

    def get_key(self):
        return self.name

    def __str__(self):
        return self.name


class ElasticsearchBaseSearchQueryCompiler(BaseSearchQueryCompiler):
    mapping_class = ElasticsearchBaseMapping
    DEFAULT_OPERATOR = "or"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mapping = self.mapping_class(self.queryset.model)
        self.remapped_fields = self._remap_fields(self.fields)

    def _remap_fields(self, fields):
        """Convert field names into index column names and add boosts."""

        remapped_fields = []
        if fields:
            searchable_fields = {f.field_name: f for f in self.get_searchable_fields()}
            for field_name in fields:
                field = searchable_fields.get(field_name)
                if field:
                    field_name = self.mapping.get_field_column_name(field)
                    remapped_fields.append(Field(field_name, field.boost or 1))
        else:
            remapped_fields.append(Field("_all_text"))

            models = get_indexed_models()
            unique_boosts = set()
            for model in models:
                if not issubclass(model, self.queryset.model):
                    continue
                for field in model.get_searchable_search_fields():
                    if field.boost:
                        unique_boosts.add(float(field.boost))

            remapped_fields.extend(
                [
                    Field(self.mapping.get_boost_field_name(boost), boost)
                    for boost in unique_boosts
                ]
            )

        return remapped_fields

    def _process_lookup(
        self, field_path, lookup, value, mapping=None, column_name_prefix=""
    ):
        if mapping is None:
            mapping = self.mapping

        field = field_path[0]
        if isinstance(field, RelatedFields):
            # Get the lookup for the next field in the path, and wrap it in a {"nested": ...} query
            related_column_name = mapping.get_field_column_name(field)
            related_column_name_prefix = column_name_prefix + related_column_name + "."
            related_model = field.get_field(mapping.model).related_model
            related_mapping = self.mapping_class(related_model)
            nested_query = self._process_lookup(
                field_path[1:],
                lookup,
                value,
                mapping=related_mapping,
                column_name_prefix=related_column_name_prefix,
            )
            return {
                "nested": {
                    "path": related_column_name,
                    "query": nested_query,
                }
            }

        column_name = column_name_prefix + mapping.get_field_column_name(field)

        if lookup == "exact":
            if isinstance(value, (Query, Subquery)):
                db_alias = self.queryset._db or DEFAULT_DB_ALIAS
                query = value.query if isinstance(value, Subquery) else value
                value = query.get_compiler(db_alias).execute_sql(result_type=SINGLE)
                # The result is either a tuple with one element or None
                if value:
                    value = value[0]

            return {
                "term": {
                    column_name: mapping._clean_value(value),
                }
            }

        if lookup == "isnull":
            query = {
                "exists": {
                    "field": column_name,
                }
            }

            if value:
                query = {"bool": {"must_not": query}}

            return query

        if lookup in ["startswith", "prefix"]:
            return {
                "prefix": {
                    column_name: mapping._clean_value(value),
                }
            }

        if lookup in ["gt", "gte", "lt", "lte"]:
            return {
                "range": {
                    column_name: {
                        lookup: mapping._clean_value(value),
                    }
                }
            }

        if lookup == "range":
            lower, upper = value

            return {
                "range": {
                    column_name: {
                        "gte": mapping._clean_value(lower),
                        "lte": mapping._clean_value(upper),
                    }
                }
            }

        if lookup == "in":
            if isinstance(value, (Query, Subquery)):
                db_alias = self.queryset._db or DEFAULT_DB_ALIAS
                query = value.query if isinstance(value, Subquery) else value
                resultset = query.get_compiler(db_alias).execute_sql(result_type=MULTI)
                value = [row[0] for chunk in resultset for row in chunk]

            elif not isinstance(value, list):
                value = list(value)
            return {
                "terms": {
                    column_name: [mapping._clean_value(val) for val in value],
                }
            }

    def _process_match_none(self):
        return {"bool": {"must_not": {"match_all": {}}}}

    def _connect_filters(self, filters, connector, negated):
        if filters:
            if len(filters) == 1:
                filter_out = filters[0]
            elif connector == "AND":
                filter_out = {
                    "bool": {"must": [fil for fil in filters if fil is not None]}
                }
            elif connector == "OR":
                filter_out = {
                    "bool": {"should": [fil for fil in filters if fil is not None]}
                }

            if negated:
                filter_out = {"bool": {"must_not": filter_out}}

            return filter_out

    def _compile_plaintext_query(self, query, fields, boost=1.0):
        match_query = {"query": query.query_string}

        if query.operator != "or":
            match_query["operator"] = query.operator

        if len(fields) == 1:
            if boost != 1.0 or fields[0].boost != 1.0:
                match_query["boost"] = boost * fields[0].boost
            return {"match": {fields[0].field_name: match_query}}
        else:
            if boost != 1.0:
                match_query["boost"] = boost
            match_query["fields"] = [field.field_name_with_boost for field in fields]

            return {"multi_match": match_query}

    def _compile_fuzzy_query(self, query, fields):
        match_query = {
            "query": query.query_string,
            "fuzziness": "AUTO",
        }

        if query.operator != "or":
            match_query["operator"] = query.operator

        if len(fields) == 1:
            if fields[0].boost != 1.0:
                match_query["boost"] = fields[0].boost
            return {"match": {fields[0].field_name: match_query}}
        else:
            match_query["fields"] = [field.field_name_with_boost for field in fields]
            return {"multi_match": match_query}

    def _compile_phrase_query(self, query, fields):
        if len(fields) == 1:
            if fields[0].boost != 1.0:
                return {
                    "match_phrase": {
                        fields[0].field_name: {
                            "query": query.query_string,
                            "boost": fields[0].boost,
                        }
                    }
                }
            else:
                return {"match_phrase": {fields[0].field_name: query.query_string}}
        else:
            return {
                "multi_match": {
                    "query": query.query_string,
                    "fields": [field.field_name_with_boost for field in fields],
                    "type": "phrase",
                }
            }

    def _compile_query(self, query, field, boost=1.0):
        if isinstance(query, MatchAll):
            match_all_query = {}

            if boost != 1.0:
                match_all_query["boost"] = boost

            return {"match_all": match_all_query}

        elif isinstance(query, And):
            return {
                "bool": {
                    "must": [
                        self._compile_query(child_query, field, boost)
                        for child_query in query.subqueries
                    ]
                }
            }

        elif isinstance(query, Or):
            return {
                "bool": {
                    "should": [
                        self._compile_query(child_query, field, boost)
                        for child_query in query.subqueries
                    ]
                }
            }

        elif isinstance(query, Not):
            return {
                "bool": {"must_not": self._compile_query(query.subquery, field, boost)}
            }

        elif isinstance(query, PlainText):
            return self._compile_plaintext_query(query, [field], boost)

        elif isinstance(query, Fuzzy):
            return self._compile_fuzzy_query(query, [field])

        elif isinstance(query, Phrase):
            return self._compile_phrase_query(query, [field])

        elif isinstance(query, Boost):
            return self._compile_query(query.subquery, field, boost * query.boost)

        else:
            raise NotImplementedError(
                f"`{query.__class__.__name__}` is not supported by the Elasticsearch search backend."
            )

    def get_inner_query(self):
        fields = self.remapped_fields

        # Handle MatchAll and PlainText separately as they were supported
        # before "search query classes" was implemented and we'd like to
        # keep the query the same as before
        if isinstance(self.query, MatchAll):
            return {"match_all": {}}

        elif isinstance(self.query, PlainText):
            return self._compile_plaintext_query(self.query, fields)

        elif isinstance(self.query, Phrase):
            return self._compile_phrase_query(self.query, fields)

        elif isinstance(self.query, Fuzzy):
            return self._compile_fuzzy_query(self.query, fields)

        elif isinstance(self.query, Not):
            return {
                "bool": {
                    "must_not": [
                        self._compile_query(self.query.subquery, field)
                        for field in fields
                    ]
                }
            }

        else:
            return self._join_and_compile_queries(self.query, fields)

    def _join_and_compile_queries(self, query, fields, boost=1.0):
        if len(fields) == 1:
            return self._compile_query(query, fields[0], boost)
        else:
            # Compile a query for each field then combine with disjunction
            # max (or operator which takes the max score out of each of the
            # field queries)
            field_queries = []
            for field in fields:
                field_queries.append(self._compile_query(query, field, boost))

            return {"dis_max": {"queries": field_queries}}

    def get_content_type_filter(self):
        # Query content_type using a "match" query. See comment in
        # ElasticsearchBaseMapping.get_document for more details
        content_type = self.mapping_class(self.queryset.model).get_content_type()

        return {"match": {"_django_content_type": content_type}}

    def get_filters(self):
        # Filter by content type
        filters = [self.get_content_type_filter()]

        # Apply filters from queryset
        queryset_filters = self._get_filters_from_queryset()
        if queryset_filters:
            filters.append(queryset_filters)

        return filters

    def get_query(self):
        inner_query = self.get_inner_query()
        filters = self.get_filters()

        if len(filters) == 1:
            return {
                "bool": {
                    "must": inner_query,
                    "filter": filters[0],
                }
            }
        elif len(filters) > 1:
            return {
                "bool": {
                    "must": inner_query,
                    "filter": filters,
                }
            }
        else:
            return inner_query

    def get_searchable_fields(self):
        return self.queryset.model.get_searchable_search_fields()

    def get_sort(self):
        # Ordering by relevance is the default in Elasticsearch
        if self.order_by_relevance:
            return

        # Get queryset and make sure its ordered
        if self.queryset.ordered:
            sort = []

            for reverse, field in self._get_order_by():
                column_name = self.mapping.get_field_column_name(field)

                sort.append({column_name: "desc" if reverse else "asc"})

            return sort

        else:
            # Order by pk field descending
            return [{"pk": "desc"}]

    def __repr__(self):
        return json.dumps(self.get_query())


class ElasticsearchBaseSearchResults(BaseSearchResults):
    fields_param_name = "stored_fields"
    supports_facet = True

    def facet(self, field_name):
        # Get field
        field = self.query_compiler._get_filterable_field(field_name)
        if field is None:
            raise FilterFieldError(
                'Cannot facet search results with field "'
                + field_name
                + "\". Please add index.FilterField('"
                + field_name
                + "') to "
                + self.query_compiler.queryset.model.__name__
                + ".search_fields.",
                field_name=field_name,
            )

        # Build body
        body = self._get_es_body()
        column_name = self.query_compiler.mapping.get_field_column_name(field)

        body["aggregations"] = {
            field_name: {
                "terms": {
                    "field": column_name,
                    "missing": 0,
                }
            }
        }

        # Send to Elasticsearch
        response = self._backend_do_search(
            body,
            index=self.backend.get_index_for_model(
                self.query_compiler.queryset.model
            ).name,
            size=0,
        )

        return OrderedDict(
            [
                (bucket["key"] if bucket["key"] != 0 else None, bucket["doc_count"])
                for bucket in response["aggregations"][field_name]["buckets"]
            ]
        )

    def _get_es_body(self, for_count=False):
        body = {"query": self.query_compiler.get_query()}

        if not for_count:
            sort = self.query_compiler.get_sort()

            if sort is not None:
                body["sort"] = sort

        return body

    def _get_results_from_hits(self, hits):
        """
        Yields Django model instances from a page of hits returned by Elasticsearch
        """
        # Get pks from results
        pks = [hit["fields"]["pk"][0] for hit in hits]
        scores = {str(hit["fields"]["pk"][0]): hit["_score"] for hit in hits}

        # Initialise results dictionary
        results = {str(pk): None for pk in pks}

        # Find objects in database and add them to dict
        for obj in self.query_compiler.queryset.filter(pk__in=pks):
            results[str(obj.pk)] = obj

            if self._score_field:
                setattr(obj, self._score_field, scores.get(str(obj.pk)))

        # Yield results in order given by Elasticsearch
        for pk in pks:
            result = results[str(pk)]
            if result:
                yield result

    def _backend_do_search(self, body, **kwargs):
        if self.backend.use_new_elasticsearch_api:
            # As of Elasticsearch 7.15, the 'body' parameter is deprecated; instead, the top-level
            # keys of the body dict are now kwargs in their own right
            return self.backend.es.search(**body, **kwargs)

        else:
            # Send the search query to the backend.
            return self.backend.es.search(body=body, **kwargs)

    def _do_search(self):
        PAGE_SIZE = 100

        if self.stop is not None:
            limit = self.stop - self.start
        else:
            limit = None

        use_scroll = limit is None or limit > PAGE_SIZE

        body = self._get_es_body()
        params = {
            "index": self.backend.get_index_for_model(
                self.query_compiler.queryset.model
            ).name,
            "_source": False,
            self.fields_param_name: "pk",
        }

        if use_scroll:
            params.update(
                {
                    "scroll": "2m",
                    "size": PAGE_SIZE,
                }
            )

            # The scroll API doesn't support offset, manually skip the first results
            skip = self.start

            # Send to Elasticsearch
            page = self._backend_do_search(body, **params)

            while True:
                hits = page["hits"]["hits"]

                if len(hits) == 0:
                    break

                # Get results
                if skip < len(hits):
                    for result in self._get_results_from_hits(hits):
                        if limit is not None and limit == 0:
                            break

                        if skip == 0:
                            yield result

                            if limit is not None:
                                limit -= 1
                        else:
                            skip -= 1

                    if limit is not None and limit == 0:
                        break
                else:
                    # Skip whole page
                    skip -= len(hits)

                # Fetch next page of results
                if "_scroll_id" not in page:
                    break

                page = self.backend.es.scroll(scroll_id=page["_scroll_id"], scroll="2m")

            # Clear the scroll
            if "_scroll_id" in page:
                self.backend.es.clear_scroll(scroll_id=page["_scroll_id"])
        else:
            params.update(
                {
                    "from_": self.start,
                    "size": limit or PAGE_SIZE,
                }
            )

            # Send to Elasticsearch
            hits = self._backend_do_search(body, **params)["hits"]["hits"]

            # Get results
            for result in self._get_results_from_hits(hits):
                yield result

    def _do_count(self):
        # Get count
        hit_count = self.backend.es.count(
            index=self.backend.get_index_for_model(
                self.query_compiler.queryset.model
            ).name,
            body=self._get_es_body(for_count=True),
        )["count"]

        # Add limits
        hit_count -= self.start
        if self.stop is not None:
            hit_count = min(hit_count, self.stop - self.start)

        return max(hit_count, 0)


class ElasticsearchAutocompleteQueryCompilerImpl:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Convert field names into index column names
        # Note: this overrides ElasticsearchBaseSearchQueryCompiler by using autocomplete fields instead of searchable fields
        if self.fields:
            fields = []
            autocomplete_fields = {
                f.field_name: f
                for f in self.queryset.model.get_autocomplete_search_fields()
            }
            for field_name in self.fields:
                if field_name in autocomplete_fields:
                    field_name = self.mapping.get_field_column_name(
                        autocomplete_fields[field_name]
                    )

                fields.append(field_name)

            self.remapped_fields = fields
        else:
            self.remapped_fields = None

    def get_inner_query(self):
        fields = self.remapped_fields or ["_edgengrams"]
        fields = [Field(field) for field in fields]
        if len(fields) == 0:
            # No fields. Return a query that'll match nothing
            return {"bool": {"must_not": {"match_all": {}}}}
        elif isinstance(self.query, PlainText):
            return self._compile_plaintext_query(self.query, fields)
        elif isinstance(self.query, MatchAll):
            return {"match_all": {}}
        else:
            raise NotImplementedError(
                f"`{self.query.__class__.__name__}` is not supported for autocomplete queries."
            )


class ElasticsearchBaseAutocompleteQueryCompiler(
    ElasticsearchAutocompleteQueryCompilerImpl, ElasticsearchBaseSearchQueryCompiler
):
    pass


class ElasticsearchIndexRebuilder:
    def __init__(self, index):
        self.index = index

    def reset_index(self):
        self.index.reset()

    def start(self):
        # Reset the index
        self.reset_index()

        return self.index

    def finish(self):
        self.index.refresh()


class ElasticsearchAtomicIndexRebuilder(ElasticsearchIndexRebuilder):
    def __init__(self, index):
        self.alias = index
        self.index = index.backend.index_class(
            index.backend, self.alias.name + "_" + get_random_string(7).lower()
        )

    def reset_index(self):
        # Delete old index using the alias
        # This should delete both the alias and the index
        # FIXME: it doesn't - it just fails with "The provided expression [...] matches an alias, specify the corresponding concrete indices instead."
        # Just as well this isn't actually called anywhere then...
        self.alias.delete()

        # Create new index
        self.index.put()

        # Create a new alias
        self.index.put_alias(self.alias.name)

    def start(self):
        # Create the new index
        self.index.put()

        return self.index

    def finish(self):
        self.index.refresh()

        if self.alias.is_alias():
            # Update existing alias, then delete the old index

            # Find index that alias currently points to, we'll delete it after
            # updating the alias
            old_index = self.alias.aliased_indices()

            # Update alias to point to new index
            self.index.put_alias(self.alias.name)

            # Delete old index
            # aliased_indices() can return multiple indices. Delete them all
            for index in old_index:
                if index.name != self.index.name:
                    index.delete()

        else:
            # self.alias doesn't currently refer to an alias in Elasticsearch.
            # This means that either nothing exists in ES with that name or
            # there is currently an index with the that name

            # Run delete on the alias, just in case it is currently an index.
            # This happens on the first rebuild after switching ATOMIC_REBUILD on
            self.alias.delete()

            # Create the alias
            self.index.put_alias(self.alias.name)


class ElasticsearchBaseSearchBackend(BaseSearchBackend):
    mapping_class = ElasticsearchBaseMapping
    index_class = ElasticsearchBaseIndex
    query_compiler_class = ElasticsearchBaseSearchQueryCompiler
    autocomplete_query_compiler_class = ElasticsearchBaseAutocompleteQueryCompiler
    results_class = ElasticsearchBaseSearchResults
    basic_rebuilder_class = ElasticsearchIndexRebuilder
    atomic_rebuilder_class = ElasticsearchAtomicIndexRebuilder
    catch_indexing_errors = True
    timeout_kwarg_name = "timeout"
    use_new_elasticsearch_api = False

    settings = {
        "settings": {
            "analysis": {
                "analyzer": {
                    "ngram_analyzer": {
                        "type": "custom",
                        "tokenizer": "standard",
                        "filter": ["asciifolding", "lowercase", "ngram"],
                    },
                    "edgengram_analyzer": {
                        "type": "custom",
                        "tokenizer": "standard",
                        "filter": ["asciifolding", "lowercase", "edgengram"],
                    },
                },
                "tokenizer": {
                    "ngram_tokenizer": {
                        "type": "ngram",
                        "min_gram": 3,
                        "max_gram": 15,
                    },
                    "edgengram_tokenizer": {
                        "type": "edge_ngram",
                        "min_gram": 2,
                        "max_gram": 15,
                        "side": "front",
                    },
                },
                "filter": {
                    "ngram": {"type": "ngram", "min_gram": 3, "max_gram": 15},
                    "edgengram": {"type": "edge_ngram", "min_gram": 1, "max_gram": 15},
                },
            },
            "index": {
                "max_ngram_diff": 12,
            },
        }
    }

    def _get_host_config_from_url(self, url):
        """Given a parsed URL, return the host configuration to be added to self.hosts"""
        use_ssl = url.scheme == "https"
        port = url.port or (443 if use_ssl else 80)

        http_auth = None
        if url.username is not None and url.password is not None:
            http_auth = (url.username, url.password)

        return {
            "host": url.hostname,
            "port": port,
            "url_prefix": url.path,
            "use_ssl": use_ssl,
            "verify_certs": use_ssl,
            "http_auth": http_auth,
        }

    def _get_options_from_host_urls(self, urls):
        """Given a list of parsed URLs, return a dict of additional options to be passed into the
        Elasticsearch constructor; necessary for options that aren't valid as part of the 'hosts' config
        """
        return {}

    def __init__(self, params):
        super().__init__(params)

        # Get settings
        self.hosts = params.pop("HOSTS", None)
        self.index_prefix = params.pop("INDEX_PREFIX", "")
        self.timeout = params.pop("TIMEOUT", 10)

        if params.pop("ATOMIC_REBUILD", True):
            self.rebuilder_class = self.atomic_rebuilder_class
        else:
            self.rebuilder_class = self.basic_rebuilder_class

        self.settings = deepcopy(
            self.settings
        )  # Make the class settings attribute as instance settings attribute
        self.settings = deep_update(self.settings, params.pop("INDEX_SETTINGS", {}))

        # Get Elasticsearch interface
        # Any remaining params are passed into the Elasticsearch constructor
        options = params.pop("OPTIONS", {})

        # If HOSTS is not set, convert URLS setting to HOSTS
        if self.hosts is None:
            es_urls = params.pop("URLS", ["http://localhost:9200"])
            # if es_urls is not a list, convert it to a list
            if isinstance(es_urls, str):
                es_urls = [es_urls]

            parsed_urls = [urlparse(url) for url in es_urls]

            self.hosts = [self._get_host_config_from_url(url) for url in parsed_urls]
            options.update(self._get_options_from_host_urls(parsed_urls))

        options[self.timeout_kwarg_name] = self.timeout

        self.es = self.client_class(hosts=self.hosts, **options)

    def get_index_for_model(self, model):
        # Split models up into separate indices based on their root model.
        # For example, all page-derived models get put together in one index,
        # while images and documents each have their own index.
        root_model = get_model_root(model)
        index_name = (
            self.index_prefix
            + root_model._meta.app_label.lower()
            + "_"
            + root_model.__name__.lower()
        )

        return self.index_class(self, index_name)


SearchBackend = ElasticsearchBaseSearchBackend
