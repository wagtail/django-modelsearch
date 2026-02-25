from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Enable the PostgreSQL fuzzystrmatch extension required for Levenshtein fuzzy search."

    def handle(self, **options):
        if connection.vendor != "postgresql":
            raise CommandError(
                "This command only works with PostgreSQL databases. "
                f"Current database vendor: {connection.vendor}"
            )

        try:
            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;")
            self.stdout.write(self.style.SUCCESS("Enabled fuzzystrmatch extension."))
        except Exception as e:
            raise CommandError(
                f"Failed to enable fuzzystrmatch extension: {e}\n"
                "You may need superuser privileges to create extensions. "
                "Try running as a database superuser or ask your DBA to run:\n"
                "  CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;"
            ) from e

        self.stdout.write(
            self.style.SUCCESS(
                "\nSetup complete. Levenshtein fuzzy search is ready.\n"
                "  Set FUZZY_ALGORITHM = 'levenshtein' in your backend config to use it."
            )
        )
