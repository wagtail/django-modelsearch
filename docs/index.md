(modelsearch)=

# Welcome to the Django-modelsearch documentation!

```{toctree}
---
maxdepth: 2
---
indexing
searching
backends
developing
```

## Installation

Install with PIP, then add to `INSTALLED_APPS` in your Django settings:

```shell
pip install modelsearch
```

```python
# settings.py

INSTALLED_APPS = [
    ...
    "modelsearch",
    ...
]
```

If you are using PostgreSQL, you must additionally add `django.contrib.postgres` to your [`INSTALLED_APPS`](https://docs.djangoproject.com/en/stable/ref/settings/#std-setting-INSTALLED_APPS) setting.

## Configuration

Django Modelsearch indexes into database by default, and it'll use the built-in full text search features of SQLite, MySQL, MariaDB and PostgreSQL with a fallback for other databases.

To configure an external server like Elasticsearch or OpenSearch, set the `MODELSEARCH_BACKENDS` setting in your Django settings:

```python
MODELSEARCH_BACKENDS = {
    'default': {
        'BACKEND': 'modelsearch.backends.elasticsearch9',
        'URLS': ['https://localhost:9200'],
        'INDEX_PREFIX': 'test_',  # Indexes are named {prefix}{app_label}_{model_name}
        'TIMEOUT': 5,
        'OPTIONS': {
            # Options to pass a kwargs to the client
        },
        'INDEX_SETTINGS': {
            # Additional index settings
        },
    }
}
```

Set the `BACKEND` for the version of Elasticsearch/OpenSearch you are using:

-   `modelsearch.backends.elasticsearch7` (Elasticsearch 7.x)
-   `modelsearch.backends.elasticsearch8` (Elasticsearch 8.x)
-   `modelsearch.backends.elasticsearch9` (Elasticsearch 9.x)
-   `modelsearch.backends.opensearch2` (OpenSearch 2.x)
-   `modelsearch.backends.opensearch3` (OpenSearch 3.x)

For more details on backend configuration, see [](modelsearch_backends).

## Indexing

Models need to be indexed before they can be searched. Each searchable model needs to inherit from the `modelsearch.index.Indexed` class and define a list of `search_fields`:

```python
from django.db import models
from modelsearch import index

class Person(index.Indexed, models.Model):
    first_name = models.CharField(max_length=30)
    last_name = models.CharField(max_length=30)

    search_fields = [
        index.SearchField('first_name'),
        index.SearchField('last_name'),
    ]
```

Once that's defined, you can then run the `rebuild_modelsearch_index` management command which will create the index, mappings, and copy all the data.

After initial indexing is complete, Django Modelsearch will use signals to keep the data in sync with your model.

For more information on indexing see [](modelsearch_indexing)

## Searching

Django modelsearch extends the Django ORM to allow QuerySets to be used for search.

To make a model searchable, you need to create a QuerySet that inherits from `modelsearch.queryset.SearchableQuerySetMixin` and use it on the model's `objects` attribute. This will add the `.search()` method to all QuerySets for the model:

```python
class PersonQuerySet(SearchableQuerySetMixin, QuerySet):
    pass


class Person(index.Indexed, models.Model):
    # ...

    objects = PersonQuerySet.as_manager()

```

You can now search using `Person.objects.search(..)`.

For more information on searching see [](modelsearch_searching).
