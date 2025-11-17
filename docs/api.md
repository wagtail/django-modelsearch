(modelsearch_api_reference)=
# API Reference

```{eval-rst}
.. automodule:: modelsearch.backends
.. autofunction:: get_search_backend

.. automodule:: modelsearch.backends.base
.. autoclass:: BaseSearchBackend

    Helper classes
    --------------

    .. autoattribute:: query_compiler_class
    .. autoattribute:: autocomplete_query_compiler_class
    .. autoattribute:: index_class
    .. autoattribute:: results_class
    .. autoattribute:: rebuilder_class

    Index management
    ----------------

    .. autoattribute:: catch_indexing_errors
    .. automethod:: get_index_for_model
    .. automethod:: get_index_for_object
    .. automethod:: all_indexes
    .. automethod:: refresh_indexes
    .. automethod:: reset_indexes

    Indexing operations
    -------------------

    .. automethod:: add
    .. automethod:: add_bulk
    .. automethod:: delete

    Searching
    ---------

    .. automethod:: search
    .. automethod:: autocomplete
    .. automethod:: _search

.. autoclass:: BaseIndex

    .. automethod:: get_key
    .. automethod:: add_model
    .. automethod:: refresh
    .. automethod:: reset
    .. automethod:: add_item
    .. automethod:: add_items
    .. automethod:: delete_item

.. autoclass:: BaseSearchResults

    .. automethod:: _do_search
    .. automethod:: _do_count

.. autoclass:: BaseSearchQueryCompiler

    .. autoattribute:: HANDLES_ORDER_BY_EXPRESSIONS
    .. automethod:: check
    .. automethod:: _get_filters_from_queryset
    .. automethod:: _get_filters_from_where_node
    .. automethod:: _get_order_by
    .. automethod:: _process_lookup
    .. automethod:: _process_match_none
    .. automethod:: _connect_filters
```
