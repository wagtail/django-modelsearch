# Backend loading
# Based on the Django cache framework
# https://github.com/django/django/blob/5d263dee304fdaf95e18d2f0619d6925984a7f02/django/core/cache/__init__.py

from importlib import import_module

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from modelsearch.conf import get_app_config


class InvalidSearchBackendError(ImproperlyConfigured):
    pass


def import_backend(dotted_path):
    """
    There's two formats for the dotted_path.
    One with the backend class (old) and one without (new)
    eg:
      old: modelsearch.backends.elasticsearch.ElasticsearchSearchBackend
      new: modelsearch.backends.elasticsearch

    If a new style dotted path was specified, this function would
    look for a backend class from the "SearchBackend" attribute.
    """
    try:
        # New
        backend_module = import_module(dotted_path)
        return backend_module.SearchBackend
    except ImportError as e:
        try:
            # Old
            return import_string(dotted_path)
        except ImportError:
            raise ImportError from e


def get_search_backend(backend="default", **kwargs):
    """
    Get the search backend instance for the given backend name. This name can be:

    - An identifier for a backend as defined in ``MODELSEARCH_BACKENDS``
    - A dotted path to a backend class, in the form ``"modelsearch.backends.elasticsearch"`` or ``"modelsearch.backends.elasticsearch.ElasticsearchSearchBackend"``.
      If a path to a module is given, ``get_search_backend`` will attempt to find a ``SearchBackend`` class within that module.

    If no name is specified, ``default`` will be used; this defaults to the ``modelsearch.backends.database`` backend if not specified in ``MODELSEARCH_BACKENDS``.

    All options within the ``MODELSEARCH_BACKENDS`` entry (except for ``BACKEND`` itself) will be passed to the backend class during instantiation. Additional
    keyword arguments will also be passed to the backend class (and override options from ``MODELSEARCH_BACKENDS``).
    """
    search_backends = get_app_config().get_search_backend_config()

    # Try to find the backend
    try:
        # Try to get the MODELSEARCH_BACKENDS entry for the given backend name first
        conf = search_backends[backend]
    except KeyError:
        try:
            # Trying to import the given backend, in case it's a dotted path
            import_backend(backend)
        except ImportError as e:
            raise InvalidSearchBackendError(
                f"Could not find backend '{backend}': {e}"
            ) from e
        params = kwargs
    else:
        # Backend is a conf entry
        params = conf.copy()
        params.update(kwargs)
        backend = params.pop("BACKEND")

    # Try to import the backend
    try:
        backend_cls = import_backend(backend)
    except ImportError as e:
        raise InvalidSearchBackendError(
            f"Could not find backend '{backend}': {e}"
        ) from e

    # Create backend
    return backend_cls(params)


def get_search_backends_with_name(with_auto_update=False):
    search_backends = get_app_config().get_search_backend_config()
    for backend, params in search_backends.items():
        if with_auto_update and params.get("AUTO_UPDATE", True) is False:
            continue

        yield backend, get_search_backend(backend)


def get_search_backends(with_auto_update=False):
    # For backwards compatibility
    return (
        backend
        for _, backend in get_search_backends_with_name(
            with_auto_update=with_auto_update
        )
    )
