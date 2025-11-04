import datetime

from warnings import warn

from django.db.models import OrderBy
from django.db.models.functions.datetime import Extract as ExtractDate
from django.db.models.functions.datetime import ExtractYear
from django.db.models.lookups import Lookup
from django.db.models.query import QuerySet
from django.db.models.sql.where import NothingNode, WhereNode

from modelsearch.index import class_is_indexed, get_indexed_models
from modelsearch.query import MATCH_ALL, PlainText


class FilterError(Exception):
    pass


class FieldError(Exception):
    def __init__(self, *args, field_name=None, **kwargs):
        self.field_name = field_name
        super().__init__(*args, **kwargs)


class SearchFieldError(FieldError):
    pass


class FilterFieldError(FieldError):
    pass


class OrderByFieldError(FieldError):
    pass


class BaseSearchQueryCompiler:
    """
    Represents a search query translated into an expression that the search backend can understand,
    incorporating the necessary filters, ordering, and other query parameters originating from
    either the search query or the queryset. No actual querying happens at the point of
    instantiating this; that's initiated by the _do_search() or _do_count() methods of the
    associated SearchResults object.
    """

    DEFAULT_OPERATOR = "or"
    HANDLES_ORDER_BY_EXPRESSIONS = False

    def __init__(
        self,
        queryset,
        query,
        fields=None,
        operator=None,
        order_by_relevance=True,
    ):
        self.queryset = queryset
        if query is None:
            warn(
                "Querying `None` is deprecated, use `MATCH_ALL` instead.",
                DeprecationWarning,
                stacklevel=5,
            )
            query = MATCH_ALL
        elif isinstance(query, str):
            query = PlainText(query, operator=operator or self.DEFAULT_OPERATOR)
        self.query = query
        self.fields = fields
        self.order_by_relevance = order_by_relevance

    def _get_filterable_field(self, field_attname):
        # Get field
        field = {
            field.get_attname(self.queryset.model): field
            for field in self.queryset.model.get_filterable_search_fields()
        }.get(field_attname, None)

        return field

    def _process_lookup(self, field, lookup, value):
        raise NotImplementedError

    def _process_match_none(self):
        raise NotImplementedError

    def _connect_filters(self, filters, connector, negated):
        raise NotImplementedError

    def _process_filter(self, field_attname, lookup, value, check_only=False):
        # Get the field
        field = self._get_filterable_field(field_attname)

        if field is None:
            raise FilterFieldError(
                f'Cannot filter search results with field "{field_attname}". Please add index.FilterField("{field_attname}") to {self.queryset.model.__name__}.search_fields.',
                field_name=field_attname,
            )

        # Process the lookup
        if not check_only:
            result = self._process_lookup(field, lookup, value)

            if result is None:
                raise FilterError(
                    f'Could not apply filter on search results: "{field_attname}__{lookup} = {value}". Lookup "{lookup}" not recognised.'
                )

            return result

    def _get_filters_from_where_node(self, where_node, check_only=False):
        # Check if this is a leaf node
        if isinstance(where_node, Lookup):
            if isinstance(where_node.lhs, ExtractDate):
                if not isinstance(where_node.lhs, ExtractYear):
                    raise FilterError(
                        f'Cannot apply filter on search results: "{where_node.lhs.lookup_name}" queries are not supported.'
                    )
                else:
                    field_attname = where_node.lhs.lhs.target.attname
                    lookup = where_node.lookup_name
                    if lookup == "gte":
                        # filter on year(date) >= value
                        # i.e. date >= Jan 1st of that year
                        value = datetime.date(int(where_node.rhs), 1, 1)
                    elif lookup == "gt":
                        # filter on year(date) > value
                        # i.e. date >= Jan 1st of the next year
                        value = datetime.date(int(where_node.rhs) + 1, 1, 1)
                        lookup = "gte"
                    elif lookup == "lte":
                        # filter on year(date) <= value
                        # i.e. date < Jan 1st of the next year
                        value = datetime.date(int(where_node.rhs) + 1, 1, 1)
                        lookup = "lt"
                    elif lookup == "lt":
                        # filter on year(date) < value
                        # i.e. date < Jan 1st of that year
                        value = datetime.date(int(where_node.rhs), 1, 1)
                    elif lookup == "exact":
                        # filter on year(date) == value
                        # i.e. date >= Jan 1st of that year and date < Jan 1st of the next year
                        filter1 = self._process_filter(
                            field_attname,
                            "gte",
                            datetime.date(int(where_node.rhs), 1, 1),
                            check_only=check_only,
                        )
                        filter2 = self._process_filter(
                            field_attname,
                            "lt",
                            datetime.date(int(where_node.rhs) + 1, 1, 1),
                            check_only=check_only,
                        )
                        if check_only:
                            return
                        else:
                            return self._connect_filters(
                                [filter1, filter2], "AND", False
                            )
                    else:  # pragma: no cover
                        raise FilterError(
                            f'Cannot apply filter on search results: "{where_node.lhs.lookup_name}" queries are not supported.'
                        )
            else:
                field_attname = where_node.lhs.target.attname
                lookup = where_node.lookup_name
                value = where_node.rhs

            # Ignore pointer fields that show up in specific page type queries
            if field_attname.endswith("_ptr_id"):
                return

            # Process the filter
            return self._process_filter(
                field_attname, lookup, value, check_only=check_only
            )

        elif isinstance(where_node, NothingNode):
            if check_only:
                return
            else:
                return self._process_match_none()

        elif isinstance(where_node, WhereNode):
            # Get child filters
            connector = where_node.connector
            child_filters = [
                self._get_filters_from_where_node(child, check_only=check_only)
                for child in where_node.children
            ]

            if not check_only:
                child_filters = [
                    child_filter for child_filter in child_filters if child_filter
                ]
                return self._connect_filters(
                    child_filters, connector, where_node.negated
                )

        else:  # pragma: no cover
            raise FilterError(
                f"Could not apply filter on search results: Unknown where node: {type(where_node)}"
            )

    def _get_filters_from_queryset(self, check_only=False):
        return self._get_filters_from_where_node(
            self.queryset.query.where, check_only=check_only
        )

    def _get_order_by(self):
        if self.order_by_relevance:
            return

        for field_name in self.queryset.query.order_by:
            reverse = False

            if isinstance(field_name, OrderBy):
                if self.HANDLES_ORDER_BY_EXPRESSIONS:
                    continue
                else:
                    raise OrderByFieldError(
                        f'Sorting search results with "{field_name}" is not supported by this search backend.',
                        field_name=field_name,
                    )

            if field_name.startswith("-"):
                reverse = True
                field_name = field_name[1:]

            field = self._get_filterable_field(field_name)

            if field is None:
                raise OrderByFieldError(
                    f'Cannot sort search results with field "{field_name}". Please add index.FilterField("{field_name}") to {self.queryset.model.__name__}.search_fields.',
                    field_name=field_name,
                )

            yield reverse, field

    def check(self):
        # Check search fields
        if self.fields:
            allowed_fields = {
                field.field_name
                for field in self.queryset.model.get_searchable_search_fields()
            }

            for field_name in self.fields:
                if field_name not in allowed_fields:
                    raise SearchFieldError(
                        f'Cannot search with field "{field_name}". Please add index.SearchField("{field_name}") to {self.queryset.model.__name__}.search_fields.',
                        field_name=field_name,
                    )

        # Check where clause
        # Raises FilterFieldError if an unindexed field is being filtered on
        self._get_filters_from_queryset(check_only=True)

        # Check order by
        # Raises OrderByFieldError if an unindexed field is being used to order by
        list(self._get_order_by())


class BaseSearchResults:
    """
    A lazily-evaluated object representing the results of a search query. This emulates the
    slicing behaviour of a Django QuerySet, but with the results not necessarily coming from
    the database.
    """

    supports_facet = False

    def __init__(self, backend, query_compiler, prefetch_related=None):
        self.backend = backend
        self.query_compiler = query_compiler
        self.prefetch_related = prefetch_related
        self.start = 0
        self.stop = None
        self._results_cache = None
        self._count_cache = None
        self._score_field = None
        # Attach the model to mimic a QuerySet so that we can inspect it after
        # doing a search, e.g. to get the model's name in a paginator.
        # The query_compiler may be None, e.g. when using EmptySearchResults.
        self.model = query_compiler.queryset.model if query_compiler else None

    def _set_limits(self, start=None, stop=None):
        if stop is not None:
            if self.stop is not None:
                self.stop = min(self.stop, self.start + stop)
            else:
                self.stop = self.start + stop

        if start is not None:
            if self.stop is not None:
                self.start = min(self.stop, self.start + start)
            else:
                self.start = self.start + start

    def _clone(self):
        """
        Returns a copy of this object with the same options in place.
        """
        klass = self.__class__
        new = klass(
            self.backend, self.query_compiler, prefetch_related=self.prefetch_related
        )
        new.start = self.start
        new.stop = self.stop
        new._score_field = self._score_field
        return new

    def _do_search(self):
        """
        To be implemented by subclasses - performs the actual search query.
        """
        raise NotImplementedError

    def _do_count(self):
        """
        To be implemented by subclasses - returns the result count.
        """
        raise NotImplementedError

    def results(self):
        """
        Returns the search results, caching them to avoid repeated queries.
        """
        if self._results_cache is None:
            self._results_cache = list(self._do_search())
        return self._results_cache

    def count(self):
        """
        Returns the count of search results, caching it to avoid repeated queries.
        """
        if self._count_cache is None:
            if self._results_cache is not None:
                self._count_cache = len(self._results_cache)
            else:
                self._count_cache = self._do_count()
        return self._count_cache

    def __getitem__(self, key):
        new = self._clone()

        if isinstance(key, slice):
            # Set limits
            start = int(key.start) if key.start is not None else None
            stop = int(key.stop) if key.stop is not None else None
            new._set_limits(start, stop)

            # Copy results cache
            if self._results_cache is not None:
                new._results_cache = self._results_cache[key]

            return new
        else:
            if self._results_cache is not None:
                return self._results_cache[key]

            new.start = self.start + key
            new.stop = self.start + key + 1
            return list(new)[0]

    def __iter__(self):
        return iter(self.results())

    def __len__(self):
        return len(self.results())

    def __repr__(self):
        data = list(self[:21])
        if len(data) > 20:
            data[-1] = "...(remaining elements truncated)..."
        return f"<SearchResults {data!r}>"

    def annotate_score(self, field_name):
        clone = self._clone()
        clone._score_field = field_name
        return clone

    def facet(self, field_name):
        raise NotImplementedError("This search backend does not support faceting")


class EmptySearchResults(BaseSearchResults):
    def __init__(self):
        super().__init__(None, None)

    def _clone(self):
        return self.__class__()

    def _do_search(self):
        return []

    def _do_count(self):
        return 0


class BaseIndex:
    """
    Manages some subset of objects in the data store.
    The base class provides do-nothing implementations of the indexing operations. Use this
    directly for search backends that do not maintain an index, such as the fallback database
    backend. Subclass it for backends that need to do something.
    """

    def __init__(self, backend):
        self.backend = backend

    def get_key(self):
        """
        Returns a hashable value that uniquely identifies this index within the search backend.
        """
        return "default"

    def add_model(self, model):
        """
        Performs any configuration required for this index to accept documents of the given model.
        """
        pass

    def refresh(self):
        """
        Performs any housekeeping required by the index so that recently-updated data is visible to searches.
        """
        pass

    def reset(self):
        """
        Resets the index to its initial state, deleting all data.
        """
        pass

    def add_item(self, obj):
        """
        Adds a single object to the index.
        """
        self.add_items(obj._meta.model, [obj])

    def add_items(self, model, items):
        """
        Adds multiple objects of the same model to the index.
        """
        pass

    def delete_item(self, item):
        """
        Deletes a single object from the index.
        """
        pass


class BaseSearchBackend:
    query_compiler_class = None
    autocomplete_query_compiler_class = None
    index_class = BaseIndex
    results_class = None
    rebuilder_class = None
    catch_indexing_errors = False

    def __init__(self, params):
        pass

    def get_index_for_model(self, model):
        """
        Returns the index to be used for the given model.
        """
        return self.index_class(self)

    def get_index_for_object(self, obj):
        """
        Returns the index to be used for the given model instance.
        """
        return self.get_index_for_model(obj._meta.model)

    def all_indexes(self):
        """
        Returns a sequence of all indexes used by this backend.
        """
        seen_keys = set()
        for model in get_indexed_models():
            index = self.get_index_for_model(model)
            key = index.get_key()
            if key not in seen_keys:
                seen_keys.add(key)
                yield index

    def refresh_indexes(self):
        """
        Refreshes all indexes used by this backend. This performs any housekeeping required by the
        index so that recently-updated data is visible to searches.
        """
        for index in self.all_indexes():
            index.refresh()

    def reset_indexes(self):
        """
        Resets all indexes used by this backend. This deletes all data from the indexes.
        """
        for index in self.all_indexes():
            index.reset()

    def add(self, obj):
        """
        Adds a single object to the data store managed by this backend.
        """
        self.get_index_for_object(obj).add_item(obj)

    def add_bulk(self, model, obj_list):
        """
        Adds multiple objects of the same model to the data store managed by this backend.
        """
        self.get_index_for_model(model).add_items(model, obj_list)

    def delete(self, obj):
        """
        Deletes a single object from the data store managed by this backend.
        """
        self.get_index_for_object(obj).delete_item(obj)

    def _search(self, query_compiler_class, query, model_or_queryset, **kwargs):
        # Find model/queryset
        if isinstance(model_or_queryset, QuerySet):
            model = model_or_queryset.model
            queryset = model_or_queryset
        else:
            model = model_or_queryset
            queryset = model_or_queryset.objects.all()

        # Model must be a class that is in the index
        if not class_is_indexed(model):
            return EmptySearchResults()

        # Check that there's still a query string after the clean up
        if query == "":
            return EmptySearchResults()

        # Search
        search_query_compiler = query_compiler_class(queryset, query, **kwargs)

        # Check the query
        search_query_compiler.check()

        return self.results_class(self, search_query_compiler)

    def search(
        self,
        query,
        model_or_queryset,
        fields=None,
        operator=None,
        order_by_relevance=True,
    ):
        """
        Performs a whole-word search.
        """
        return self._search(
            self.query_compiler_class,
            query,
            model_or_queryset,
            fields=fields,
            operator=operator,
            order_by_relevance=order_by_relevance,
        )

    def autocomplete(
        self,
        query,
        model_or_queryset,
        fields=None,
        operator=None,
        order_by_relevance=True,
    ):
        """
        Performs an autocomplete (partial word match) search.
        """
        if self.autocomplete_query_compiler_class is None:
            raise NotImplementedError(
                "This search backend does not support the autocomplete API"
            )

        return self._search(
            self.autocomplete_query_compiler_class,
            query,
            model_or_queryset,
            fields=fields,
            operator=operator,
            order_by_relevance=order_by_relevance,
        )


def get_model_root(model):
    """
    This function finds the root model for any given model. The root model is
    the highest concrete model that it descends from. If the model doesn't
    descend from another concrete model then the model is it's own root model so
    it is returned.

    Examples:
    >>> get_model_root(wagtailcore.Page)
    wagtailcore.Page

    >>> get_model_root(myapp.HomePage)
    wagtailcore.Page

    >>> get_model_root(wagtailimages.Image)
    wagtailimages.Image
    """
    if model._meta.parents:
        parent_model = list(model._meta.parents.items())[0][0]
        return get_model_root(parent_model)

    return model
