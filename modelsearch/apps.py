from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Tags, Warning, register
from django.db import connection


class ModelSearchAppConfig(AppConfig):
    name = "modelsearch"
    label = "modelsearch"
    verbose_name = "Django ModelSearch"
    default_auto_field = "django.db.models.AutoField"
    backend_setting_name = "MODELSEARCH_BACKENDS"

    def ready(self):
        from modelsearch.signal_handlers import register_signal_handlers

        register_signal_handlers()

        if connection.vendor == "postgresql":
            # Only PostgreSQL has support for tsvector weights
            from modelsearch.backends.database.postgres.weights import set_weights

            set_weights()

        self.get_model("IndexEntry").add_generic_relations()

    def get_search_backend_config(self):
        search_backends = getattr(settings, self.backend_setting_name, {})

        # Make sure the default backend is always defined
        search_backends.setdefault(
            "default",
            {
                "BACKEND": "modelsearch.backends.database",
            },
        )

        return search_backends

    @register(Tags.compatibility, Tags.database)
    def check_if_sqlite_version_is_supported(app_configs, **kwargs):
        if connection.vendor == "sqlite":
            import sqlite3

            from modelsearch.backends.database.sqlite.utils import fts5_available

            if sqlite3.sqlite_version_info < (3, 19, 0):  # pragma: no cover
                return [
                    Warning(
                        "Your SQLite version is older than 3.19.0. A fallback search backend will be used instead.",
                        hint="Upgrade your SQLite version to at least 3.19.0",
                        id="modelsearch.W002",
                        obj=ModelSearchAppConfig,
                    )
                ]
            elif not fts5_available():  # pragma: no cover
                return [
                    Warning(
                        "Your SQLite installation is missing the fts5 extension. A fallback search backend will be used instead.",
                        hint="Upgrade your SQLite installation to a version with fts5 enabled",
                        id="modelsearch.W003",
                        obj=ModelSearchAppConfig,
                    )
                ]
        return []
