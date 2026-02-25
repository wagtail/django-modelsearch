from django.contrib.postgres.operations import TrigramExtension
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = "Enable pg_trgm and unaccent extensions and create GIN trigram indexes for fuzzy search."

    def handle(self, **options):
        if connection.vendor != "postgresql":
            raise CommandError(
                "This command only works with PostgreSQL databases. "
                f"Current database vendor: {connection.vendor}"
            )

        # Enable pg_trgm extension
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm';")
            pg_trgm_exists = bool(cursor.fetchone())

        if pg_trgm_exists:
            self.stdout.write("pg_trgm extension is already enabled.")
        else:
            operation = TrigramExtension()
            executor = MigrationExecutor(connection)
            state = executor.loader.project_state()
            try:
                with connection.schema_editor() as schema_editor:
                    operation.database_forwards(
                        "modelsearch", schema_editor, state, state
                    )
                self.stdout.write(self.style.SUCCESS("Enabled pg_trgm extension."))
            except Exception as e:
                raise CommandError(
                    f"Failed to enable pg_trgm extension: {e}\n"
                    "You may need superuser privileges to create extensions. "
                    "Try running as a database superuser or ask your DBA to run:\n"
                    "  CREATE EXTENSION IF NOT EXISTS pg_trgm;"
                ) from e

        # Create accent-sensitive GIN indexes (for Fuzzy() without unaccent=True)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS modelsearch_title_text_trgm "
                    "ON modelsearch_indexentry USING gin (title_text gin_trgm_ops);"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS modelsearch_body_text_trgm "
                    "ON modelsearch_indexentry USING gin (body_text gin_trgm_ops);"
                )
            self.stdout.write(
                self.style.SUCCESS("Created accent-sensitive GIN trigram indexes.")
            )
        except Exception as e:
            raise CommandError(f"Failed to create trigram indexes: {e}") from e

        # Create expression GIN indexes on f_unaccent(col) (for Fuzzy(unaccent=True))
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS modelsearch_title_text_unaccent_trgm "
                    "ON modelsearch_indexentry USING gin (f_unaccent(title_text) gin_trgm_ops);"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS modelsearch_body_text_unaccent_trgm "
                    "ON modelsearch_indexentry USING gin (f_unaccent(body_text) gin_trgm_ops);"
                )
            self.stdout.write(
                self.style.SUCCESS("Created accent-insensitive GIN trigram indexes.")
            )
        except Exception as e:
            raise CommandError(f"Failed to create unaccent trigram indexes: {e}") from e

        self.stdout.write(
            self.style.SUCCESS(
                "\nSetup complete. Fuzzy search is ready.\n"
                "  Fuzzy('query')               — accent-sensitive\n"
                "  Fuzzy('query', unaccent=True) — accent-insensitive"
            )
        )
