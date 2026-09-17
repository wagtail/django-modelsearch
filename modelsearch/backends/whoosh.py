"""
A pure-Python full-text search backend for django-modelsearch, built on Whoosh
(the actively-maintained ``whoosh3`` fork: https://pypi.org/project/whoosh3/).

Like the Elasticsearch/OpenSearch backends, this backend maintains its own
on-disk index (a plain directory of files, one index per root model) rather than
querying the database directly the way the fallback database backend does. That
gives it real BM25F relevance ranking, per-field boosting, phrase/fuzzy queries
and prefix autocomplete -- with no external service to run.

Design notes
------------
* One Whoosh index directory per *root* model (``get_model_root``). All concrete
  subclasses that share a root are stored in the same index, keyed by primary
  key (primary keys are shared across the multi-table-inheritance chain, so they
  are unique within a root index).
* Filtering (``.filter(...)`` on the queryset, related-field filters, ranges,
  ``__isnull`` etc.) is delegated entirely to Django: Whoosh produces a
  relevance-ordered list of matching primary keys for the *text* query, and we
  intersect that with the primary keys returned by the (already-filtered)
  queryset. This keeps the backend small and correct without re-implementing
  Django's filter semantics.
"""

import os
import shutil
import tempfile

from django.db import models
from whoosh import index as whoosh_index
from whoosh.analysis import StemmingAnalyzer
from whoosh.fields import ID, KEYWORD, TEXT, Schema
from whoosh.qparser import AndGroup, FuzzyTermPlugin, MultifieldParser, OrGroup
from whoosh.query import And as WAnd
from whoosh.query import Every
from whoosh.query import Not as WNot
from whoosh.query import Or as WOr

from modelsearch.backends.base import (
    BaseIndex,
    BaseSearchBackend,
    BaseSearchQueryCompiler,
    BaseSearchResults,
    get_model_root,
)
from modelsearch.index import (
    AutocompleteField,
    FilterField,
    RelatedFields,
    SearchField,
    class_is_indexed,
    get_indexed_models,
)
from modelsearch.query import (
    And,
    Boost,
    Fuzzy,
    MatchAll,
    Not,
    Or,
    Phrase,
    PlainText,
)


# Maximum length of an edge-ngram generated for autocomplete fields.
MAX_EDGENGRAM = 15


def _edgengrams(text):
    """Expand a string into whitespace-joined edge n-grams for prefix matching."""
    grams = []
    for word in str(text).split():
        word = word.lower()
        for i in range(1, min(len(word), MAX_EDGENGRAM) + 1):
            grams.append(word[:i])
    return " ".join(grams)


class WhooshMapping:
    """Translates a Django model into a Whoosh schema and documents."""

    def __init__(self, model):
        self.model = model
        self.root_model = get_model_root(model)

    # -- naming -------------------------------------------------------------

    def get_content_type(self):
        return self.model._meta.app_label + "." + self.model.__name__

    def get_field_column_name(self, field):
        definition_model = field.get_definition_model(self.model)
        if definition_model is not None and definition_model != self.root_model:
            prefix = (
                definition_model._meta.app_label.lower()
                + "_"
                + definition_model.__name__.lower()
                + "__"
            )
        else:
            prefix = ""

        if isinstance(field, RelatedFields):
            return prefix + field.field_name
        if isinstance(field, AutocompleteField):
            return prefix + field.get_attname(self.model) + "_edgengrams"
        return prefix + field.get_attname(self.model)

    @staticmethod
    def get_boost_field_name(boost):
        return "all_text_boost_" + str(float(boost)).replace(".", "_").replace("-", "_")

    # -- schema -------------------------------------------------------------

    def _boosts_in_fields(self, model, fields):
        boosts = set()
        for field in fields:
            if isinstance(field, RelatedFields):
                related_field = field.get_field(model)
                related_model = related_field.related_model
                boosts |= self._boosts_in_fields(related_model, field.fields)
            elif isinstance(field, SearchField) and field.boost:
                boosts.add(float(field.boost))
        return boosts

    def _collect_boosts(self):
        boosts = set()
        for model in get_indexed_models():
            if get_model_root(model) is not self.root_model:
                continue
            boosts |= self._boosts_in_fields(model, model.get_search_fields())
        return boosts

    def build_schema(self):
        """Build a Whoosh schema covering every model sharing this root."""
        analyzer = StemmingAnalyzer()
        schema = Schema()
        schema.add("pk", ID(stored=True, unique=True))
        schema.add("django_content_type", KEYWORD(stored=True))
        schema.add("all_text", TEXT(analyzer=analyzer))
        schema.add("edgengrams", TEXT(stored=False))

        for boost in self._collect_boosts():
            schema.add(self.get_boost_field_name(boost), TEXT(analyzer=analyzer))

        # Add a per-field TEXT column for every searchable/autocomplete field on
        # every model that shares this root, so that field-restricted searches
        # (``fields=[...]``) work across the inheritance chain.
        for model in get_indexed_models():
            if get_model_root(model) is not self.root_model:
                continue
            mapping = WhooshMapping(model)
            for field in model.get_search_fields():
                mapping._add_field_columns(schema, field, analyzer)

        return schema

    def _add_field_columns(self, schema, field, analyzer, prefix=""):
        if isinstance(field, RelatedFields):
            related_model = field.get_field(self.model).related_model
            related_mapping = WhooshMapping(related_model)
            name = prefix + self.get_field_column_name(field)
            for sub_field in field.fields:
                related_mapping._add_field_columns(
                    schema, sub_field, analyzer, prefix=name + "__"
                )
            return

        if isinstance(field, SearchField):
            name = prefix + self.get_field_column_name(field)
            if name not in schema:
                schema.add(name, TEXT(analyzer=analyzer))
        elif isinstance(field, AutocompleteField):
            name = prefix + self.get_field_column_name(field)
            if name not in schema:
                # Edge-ngrams are pre-expanded at index time; a plain
                # whitespace/standard analyzer (no stemming) matches them.
                schema.add(name, TEXT(stored=False))

    # -- documents ----------------------------------------------------------

    def get_document_id(self, obj):
        return str(obj.pk)

    def _collect(self, model, fields, obj, doc, acc, column_prefix=""):
        """
        Recursively walk ``fields`` for ``obj``, populating:
        - ``doc`` with per-field TEXT columns (for field-restricted search)
        - ``acc['all_text']`` list of all searchable text
        - ``acc['boosts']`` map of boost value -> list of text
        - ``acc['edgengrams']`` list of edge-ngram strings
        """
        for field in fields:
            column = column_prefix + self.get_field_column_name_for(model, field)

            if isinstance(field, RelatedFields):
                value = field.get_value(obj)
                objs = []
                if isinstance(value, (models.Manager, models.QuerySet)):
                    objs = list(value.all())
                elif isinstance(value, models.Model):
                    objs = [value]
                for nested_obj in objs:
                    nested_model = type(nested_obj)
                    WhooshMapping(nested_model)._collect(
                        nested_model,
                        field.fields,
                        nested_obj,
                        doc,
                        acc,
                        column_prefix=column + "__",
                    )
                continue

            if isinstance(field, FilterField):
                continue

            value = field.get_value(obj)
            if value is None:
                continue
            text = str(value)
            if not text:
                continue

            if isinstance(field, AutocompleteField):
                # Autocomplete content is pre-expanded into edge-ngrams and kept
                # out of ``all_text`` (matching the Elasticsearch backend, where
                # AutocompleteField is not ``include_in_all``).
                grams = _edgengrams(text)
                if column in doc:
                    doc[column] = doc[column] + " " + grams
                else:
                    doc[column] = grams
                acc["edgengrams"].append(grams)
                continue

            # SearchField: contributes to its own column, all_text, and boosts.
            if column in doc:
                doc[column] = doc[column] + " " + text
            else:
                doc[column] = text
            acc["all_text"].append(text)
            if field.boost:
                acc["boosts"].setdefault(float(field.boost), []).append(text)

    def get_field_column_name_for(self, model, field):
        return WhooshMapping(model).get_field_column_name(field)

    def get_document(self, obj):
        doc = {
            "pk": str(obj.pk),
            "django_content_type": self._all_content_types(),
        }
        acc = {"all_text": [], "boosts": {}, "edgengrams": []}
        self._collect(self.model, self.model.get_search_fields(), obj, doc, acc)

        doc["all_text"] = " ".join(acc["all_text"])
        doc["edgengrams"] = " ".join(acc["edgengrams"])
        for boost, texts in acc["boosts"].items():
            name = self.get_boost_field_name(boost)
            if name in doc:
                doc[name] = doc[name] + " " + " ".join(texts)
            else:
                doc[name] = " ".join(texts)
        return doc

    def _all_content_types(self):
        types = []
        model = self.model
        while model is not None and issubclass(model, models.Model):
            from modelsearch.index import Indexed

            if issubclass(model, Indexed):
                types.append(model._meta.app_label + "." + model.__name__)
            parents = model._meta.parents
            model = list(parents)[0] if parents else None
        return " ".join(types)


class WhooshIndex(BaseIndex):
    def __init__(self, backend, root_model):
        super().__init__(backend)
        self.backend = backend
        self.root_model = root_model
        self.mapping = WhooshMapping(root_model)
        self.name = root_model._meta.app_label + "_" + root_model.__name__.lower()
        self.path = os.path.join(backend.path, self.name)

    def get_key(self):
        return self.name

    def _open(self, create=False):
        if create or not whoosh_index.exists_in(self.path):
            os.makedirs(self.path, exist_ok=True)
            return whoosh_index.create_in(self.path, self.mapping.build_schema())
        return whoosh_index.open_dir(self.path)

    def add_model(self, model):
        self._open(create=not whoosh_index.exists_in(self.path))

    def refresh(self):
        pass

    def reset(self):
        if os.path.isdir(self.path):
            shutil.rmtree(self.path)
        self._open(create=True)

    def add_item(self, obj):
        self.add_items(obj._meta.model, [obj])

    def add_items(self, model, items):
        if not class_is_indexed(model):
            return
        ix = self._open()
        writer = ix.writer()
        try:
            for item in items:
                mapping = WhooshMapping(type(item))
                writer.update_document(**mapping.get_document(item))
            writer.commit()
        except Exception:
            writer.cancel()
            raise

    def delete_item(self, item):
        if not class_is_indexed(item.__class__):
            return
        if not whoosh_index.exists_in(self.path):
            return
        ix = self._open()
        writer = ix.writer()
        writer.delete_by_term("pk", str(item.pk))
        writer.commit()


class WhooshSearchQueryCompiler(BaseSearchQueryCompiler):
    DEFAULT_OPERATOR = "or"
    HANDLES_ORDER_BY_EXPRESSIONS = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mapping = WhooshMapping(self.queryset.model)
        self.schema = WhooshMapping(get_model_root(self.queryset.model)).build_schema()
        self.remapped_fields = self._remap_fields(self.fields)

    def _remap_fields(self, fields):
        remapped = []
        if fields:
            searchable = {f.field_name: f for f in self.get_searchable_fields()}
            for field_name in fields:
                field = searchable.get(field_name)
                if field is not None:
                    remapped.append(
                        (self.mapping.get_field_column_name(field), field.boost or 1.0)
                    )
        else:
            remapped.append(("all_text", 1.0))
            for boost in self.mapping._collect_boosts():
                remapped.append((self.mapping.get_boost_field_name(boost), boost))
        # Only keep fields that actually exist in the schema.
        return [(name, boost) for name, boost in remapped if name in self.schema]

    def get_searchable_fields(self):
        return self.queryset.model.get_searchable_search_fields()

    def _parser(self, group):
        fieldnames = [name for name, _ in self.remapped_fields] or ["all_text"]
        fieldboosts = dict(self.remapped_fields)
        parser = MultifieldParser(
            fieldnames, schema=self.schema, group=group, fieldboosts=fieldboosts
        )
        return parser

    def _compile(self, query, boost=1.0):
        if isinstance(query, MatchAll):
            return Every()

        if isinstance(query, PlainText):
            group = AndGroup if query.operator == "and" else OrGroup
            parser = self._parser(group)
            wq = parser.parse(query.query_string)
            factor = boost * getattr(query, "boost", 1.0)
            if wq is not None and factor != 1.0:
                wq = wq.with_boost(factor)
            return wq

        if isinstance(query, Phrase):
            parser = self._parser(OrGroup)
            wq = parser.parse('"' + query.query_string + '"')
            return wq

        if isinstance(query, Fuzzy):
            group = AndGroup if query.operator == "and" else OrGroup
            parser = self._parser(group)
            parser.add_plugin(FuzzyTermPlugin())
            text = " ".join(t + "~" for t in query.query_string.split())
            return parser.parse(text)

        if isinstance(query, Boost):
            return self._compile(query.subquery, boost=boost * query.boost)

        if isinstance(query, And):
            return WAnd([self._compile(s, boost) for s in query.subqueries])

        if isinstance(query, Or):
            return WOr([self._compile(s, boost) for s in query.subqueries])

        if isinstance(query, Not):
            return WNot(self._compile(query.subquery, boost))

        raise NotImplementedError(
            f"`{query.__class__.__name__}` is not supported by the Whoosh backend."
        )

    def build_whoosh_query(self):
        return self._compile(self.query)


class WhooshAutocompleteQueryCompiler(WhooshSearchQueryCompiler):
    def _remap_fields(self, fields):
        remapped = []
        if fields:
            autocomplete = {
                f.field_name: f
                for f in self.queryset.model.get_autocomplete_search_fields()
            }
            for field_name in fields:
                field = autocomplete.get(field_name)
                if field is not None:
                    remapped.append((self.mapping.get_field_column_name(field), 1.0))
        else:
            remapped.append(("edgengrams", 1.0))
        return [(name, boost) for name, boost in remapped if name in self.schema]


class WhooshSearchResults(BaseSearchResults):
    supports_facet = True

    def facet(self, field_name):
        from collections import OrderedDict

        from django.db.models import Count

        from modelsearch.backends.base import FilterFieldError

        compiler = self.query_compiler
        field = compiler._get_filterable_field(field_name)
        if field is None:
            raise FilterFieldError(
                'Cannot facet search results with field "'
                + field_name
                + "\". Please add index.FilterField('"
                + field_name
                + "') to "
                + compiler.queryset.model.__name__
                + ".search_fields.",
                field_name=field_name,
            )

        pks = [pk for pk, _ in self._ordered_pks()]
        queryset = compiler.queryset.filter(pk__in=pks)
        results = (
            queryset.values(field_name).annotate(count=Count("pk")).order_by("-count")
        )
        return OrderedDict((result[field_name], result["count"]) for result in results)

    def _ordered_pks(self):
        compiler = self.query_compiler
        index = self.backend.get_index_for_model(compiler.queryset.model)
        if not whoosh_index.exists_in(index.path):
            return []

        wq = compiler.build_whoosh_query()
        if wq is None:
            return []

        ix = index._open()
        with ix.searcher() as searcher:
            hits = searcher.search(wq, limit=None)
            ordered = [(h["pk"], h.score) for h in hits]

        # Intersect with the (already-filtered) queryset's primary keys so that
        # Django applies all filter/related-field/range semantics for us.
        allowed = {str(pk) for pk in compiler.queryset.values_list("pk", flat=True)}
        return [(pk, score) for pk, score in ordered if pk in allowed]

    def _do_search(self):
        results = self._ordered_pks()

        if not self.query_compiler.order_by_relevance:
            # Preserve the queryset's ordering rather than relevance order.
            pks = [pk for pk, _ in results]
            queryset = self.query_compiler.queryset.filter(pk__in=pks)
            if not queryset.query.order_by and not queryset.ordered:
                # Add a stable default ordering to keep pagination consistent.
                queryset = queryset.order_by("-pk")
            objects = list(queryset[self.start : self.stop])
            if self._score_field:
                for obj in objects:
                    setattr(obj, self._score_field, None)
            return objects

        page = results[self.start : self.stop]
        pks = [pk for pk, _ in page]
        scores = dict(page)

        objects = {
            str(obj.pk): obj for obj in self.query_compiler.queryset.filter(pk__in=pks)
        }
        output = []
        for pk, _ in page:
            obj = objects.get(pk)
            if obj is None:
                continue
            if self._score_field:
                setattr(obj, self._score_field, scores.get(pk))
            output.append(obj)
        return output

    def _do_count(self):
        results = self._ordered_pks()
        count = len(results) - self.start
        if self.stop is not None:
            count = min(count, self.stop - self.start)
        return max(count, 0)


class WhooshIndexRebuilder:
    def __init__(self, index):
        self.index = index

    def start(self):
        self.index.reset()
        return self.index

    def finish(self):
        self.index.refresh()


class WhooshSearchBackend(BaseSearchBackend):
    query_compiler_class = WhooshSearchQueryCompiler
    autocomplete_query_compiler_class = WhooshAutocompleteQueryCompiler
    index_class = WhooshIndex
    results_class = WhooshSearchResults
    rebuilder_class = WhooshIndexRebuilder

    def __init__(self, params):
        super().__init__(params)
        path = params.get("PATH")
        if path is None:
            path = os.path.join(tempfile.gettempdir(), "modelsearch_whoosh")
        self.path = path
        os.makedirs(self.path, exist_ok=True)

    def get_index_for_model(self, model):
        return WhooshIndex(self, get_model_root(model))


SearchBackend = WhooshSearchBackend
