from django.db import connection, migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("modelsearch", "0002_customise_indexentry"),
    ]

    if connection.vendor == "postgresql":
        operations = [
            migrations.AddField(
                model_name="indexentry",
                name="title_text",
                field=models.TextField(default=""),
            ),
            migrations.AddField(
                model_name="indexentry",
                name="body_text",
                field=models.TextField(default=""),
            ),
            # GIN trigram indexes for efficient fuzzy search.
            # These require the pg_trgm extension. The CREATE INDEX is
            # wrapped in a try/catch so it silently skips if pg_trgm
            # is not yet enabled. Users who enable pg_trgm later can
            # create these indexes manually for better performance:
            #   CREATE INDEX ... USING gin (title_text gin_trgm_ops);
            #   CREATE INDEX ... USING gin (body_text gin_trgm_ops);
            migrations.RunSQL(
                sql="""
                    DO $$
                    BEGIN
                        CREATE INDEX modelsear_title_text_trgm
                            ON modelsearch_indexentry USING gin (title_text gin_trgm_ops);
                    EXCEPTION WHEN undefined_object THEN
                        NULL;
                    END $$;
                """,
                reverse_sql="DROP INDEX IF EXISTS modelsear_title_text_trgm;",
            ),
            migrations.RunSQL(
                sql="""
                    DO $$
                    BEGIN
                        CREATE INDEX modelsear_body_text_trgm
                            ON modelsearch_indexentry USING gin (body_text gin_trgm_ops);
                    EXCEPTION WHEN undefined_object THEN
                        NULL;
                    END $$;
                """,
                reverse_sql="DROP INDEX IF EXISTS modelsear_body_text_trgm;",
            ),
        ]
    else:
        operations = []
