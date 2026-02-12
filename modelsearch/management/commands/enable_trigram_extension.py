from django.contrib.postgres.operations import TrigramExtension
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = "Enable the PostgreSQL pg_trgm extension required for fuzzy search."

    def handle(self, **options):
        if connection.vendor != "postgresql":
            raise CommandError(
                "This command only works with PostgreSQL databases. "
                f"Current database vendor: {connection.vendor}"
            )

        # Check if extension is already installed
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm';")
            if cursor.fetchone():
                self.stdout.write(
                    self.style.SUCCESS("pg_trgm extension is already enabled.")
                )
                return

        # Use Django's TrigramExtension operation
        operation = TrigramExtension()

        # Create a minimal state for the operation
        executor = MigrationExecutor(connection)
        state = executor.loader.project_state()

        # Try to enable the extension
        try:
            with connection.schema_editor() as schema_editor:
                operation.database_forwards("modelsearch", schema_editor, state, state)
            self.stdout.write(
                self.style.SUCCESS("Successfully enabled pg_trgm extension.")
            )
        except Exception as e:
            raise CommandError(
                f"Failed to enable pg_trgm extension: {e}\n"
                "You may need superuser privileges to create extensions. "
                "Try running as a database superuser or ask your DBA to run:\n"
                "  CREATE EXTENSION IF NOT EXISTS pg_trgm;"
            ) from e
