from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Enable the PostgreSQL unaccent extension and create the f_unaccent() immutable wrapper function."

    def handle(self, **options):
        if connection.vendor != "postgresql":
            raise CommandError(
                "This command only works with PostgreSQL databases. "
                f"Current database vendor: {connection.vendor}"
            )

        # Enable unaccent extension
        try:
            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS unaccent;")
            self.stdout.write(self.style.SUCCESS("Enabled unaccent extension."))
        except Exception as e:
            raise CommandError(
                f"Failed to enable unaccent extension: {e}\n"
                "You may need superuser privileges to create extensions. "
                "Try running as a database superuser or ask your DBA to run:\n"
                "  CREATE EXTENSION IF NOT EXISTS unaccent;"
            ) from e

        # Create immutable f_unaccent() wrapper (required for expression indexes)
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    CREATE OR REPLACE FUNCTION f_unaccent(text)
                        RETURNS text
                        LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
                    $func$
                    SELECT public.unaccent('public.unaccent', $1)
                    $func$;
                """)
            self.stdout.write(
                self.style.SUCCESS("Created f_unaccent() immutable wrapper function.")
            )
        except Exception as e:
            raise CommandError(f"Failed to create f_unaccent() function: {e}") from e

        self.stdout.write(
            self.style.SUCCESS(
                "\nSetup complete. Accent-insensitive search support is ready.\n"
                "  Use Fuzzy('query', unaccent=True) for accent-insensitive matching."
            )
        )
