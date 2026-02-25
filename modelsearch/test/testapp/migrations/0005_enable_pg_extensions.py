from django.core.management import call_command
from django.db import connection, migrations


def enable_pg_extensions(apps, schema_editor):
    if connection.vendor != "postgresql":
        return
    call_command("enable_unaccent", verbosity=0)
    call_command("enable_trigram", verbosity=0)
    call_command("enable_fuzzystrmatch", verbosity=0)


class Migration(migrations.Migration):
    dependencies = [
        ("searchtests", "0004_meeting"),
        ("modelsearch", "0003_add_text_fields"),
    ]

    operations = [
        migrations.RunPython(enable_pg_extensions, migrations.RunPython.noop),
    ]
