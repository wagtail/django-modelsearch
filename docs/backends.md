(modelsearch_backends)=

# Backends

Django Modelsearch has support for multiple backends, giving you the choice between using the database for search or an external service such as Elasticsearch.

You can configure which backend to use with the `MODELSEARCH_BACKENDS` setting:

```python
MODELSEARCH_BACKENDS = {
    'default': {
        'BACKEND': 'modelsearch.backends.database',
    }
}
```

(modelsearch_backends_auto_update)=

## `AUTO_UPDATE`

By default, Django Modelsearch will automatically keep all indexes up to date. This could impact peformance as each save will trigger the indexing to occur.

The `AUTO_UPDATE` setting allows you to disable this for the backend:

```python
MODELSEARCH_BACKENDS = {
    'default': {
        'BACKEND': ...,
        'AUTO_UPDATE': False,
    }
}
```

If you have disabled auto-update, you must run the `rebuild_modelsearch_index` command on a regular basis to keep the index in sync with the database.

(modelsearch_backends_atomic_rebuild)=

## `ATOMIC_REBUILD`

By default (when using the Elasticsearch backend), Django Modelsearch creates a new index when the `rebuild_modelsearch_index` is run, reindexes the content into the new index then, using an alias, activates the new index. Then deletes the old index.

If creating new indexes is not an option for you, you can disable this behaviour by setting `ATOMIC_REBUILD` to `False`. This will make Django Modelsearch delete the index then build a new one. Note that this will cause the search engine to not return results until the rebuild is complete.

## `BACKEND`

Here's a list of backends that Django Modelsearch supports out of the box.

(modelsearch_backends_database)=

### Database Backend (default)

`modelsearch.backends.database`

The database search backend searches content in the database using the full-text search features of the database backend in use (such as PostgreSQL FTS, SQLite FTS5).
This backend is intended to be used for development and also should be good enough to use in production on sites that don't require any Elasticsearch specific features.

If you use the PostgreSQL database backend, you must add `django.contrib.postgres` to your [`INSTALLED_APPS`](https://docs.djangoproject.com/en/stable/ref/settings/#std-setting-INSTALLED_APPS) setting.

(modelsearch_backends_elasticsearch)=

### Elasticsearch/OpenSearch Backends

Elasticsearch versions 7, 8, and 9 are supported. OpenSearch 2 and 3 are supported.

You'll need to install the [elasticsearch-py](https://elasticsearch-py.readthedocs.io/) package for Elasticsearch and for OpenSearch, you'll need the [opensearch-py](https://pypi.org/project/opensearch-py/) package. The major version of the package must match the installed version of Elasticsearch/OpenSearch:

```sh
pip install "elasticsearch>=7,<8"  # for Elasticsearch 7.x
```

```sh
pip install "elasticsearch>=8,<9"  # for Elasticsearch 8.x
```

```sh
pip install "elasticsearch>=9,<10"  # for Elasticsearch 9.x
```

```sh
pip install "opensearch-py>=2,<3"  # for OpenSearch 2.x
```

```sh
pip install "opensearch-py>=3,<4"  # for OpenSearch 3.x
```

Then configure the backend in ``MODELSEARCH_SETTINGS`` in your Django settings:

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

Any defined key in `OPTIONS` is passed directly to the Elasticsearch/OpenSearch constructor as a case-sensitive keyword argument (for example `'max_retries': 1`).

A username and password may be optionally supplied to the `URL` field to provide authentication credentials for the Elasticsearch/OpenSearch service:

```python
MODELSEARCH_BACKENDS = {
    'default': {
        ...
        'URLS': ['https://username:password@localhost:9200'],
        ...
    }
}
```

`INDEX_SETTINGS` is a dictionary used to override the default settings to create the index. The default settings are defined inside the `ElasticsearchSearchBackend` class in the module `modelsearch/backends/elasticsearch7.py`. Any new key is added and any existing key, if not a dictionary, is replaced with the new value. Here's a sample of how to configure the number of shards and set the Italian LanguageAnalyzer as the default analyzer:

```python
MODELSEARCH_BACKENDS = {
    'default': {
        ...,
        'INDEX_SETTINGS': {
            'settings': {
                'index': {
                    'number_of_shards': 1,
                },
                'analysis': {
                    'analyzer': {
                        'default': {
                            'type': 'italian'
                        }
                    }
                }
            }
        }
    }
```

If you prefer not to run an Elasticsearch server in development or production, there are many hosted services available, including [Bonsai](https://bonsai.io/), which offers a free account suitable for testing and development. To use Bonsai:

-   Sign up for an account at `Bonsai`
-   Use your Bonsai dashboard to create a Cluster.
-   Configure `URLS` in the Elasticsearch entry in `MODELSEARCH_BACKENDS` using the Cluster URL from your Bonsai dashboard
-   Run `./manage.py rebuild_modelsearch_index`

(opensearch)=

### Amazon AWS OpenSearch

The OpenSearch backend is compatible with [Amazon OpenSearch Service](https://aws.amazon.com/opensearch-service/), but requires additional configuration to handle IAM based authentication. This can be done with the [requests-aws4auth](https://pypi.org/project/requests-aws4auth/) package along with the following configuration:

```python
from elasticsearch import RequestsHttpConnection
from requests_aws4auth import AWS4Auth

MODELSEARCH_BACKENDS = {
    'default': {
        'BACKEND': 'modelsearch.backends.opensearch2',
        'INDEX_PREFIX': 'test_',
        'TIMEOUT': 5,
        'HOSTS': [{
            'host': 'YOURCLUSTER.REGION.es.amazonaws.com',
            'port': 443,
            'use_ssl': True,
            'verify_certs': True,
            'http_auth': AWS4Auth('ACCESS_KEY', 'SECRET_KEY', 'REGION', 'es'),
        }],
        'OPTIONS': {
            'connection_class': RequestsHttpConnection,
        },
    }
}
```

## Rolling Your Own

Django Modelsearch backends implement the interface defined in `modelsearch/backends/base.py`. At a minimum, the backend's `search()` method must return a collection of objects or `model.objects.none()`. For a fully-featured search backend, examine the Elasticsearch backend code in `elasticsearch.py`.
