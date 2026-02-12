from django.contrib.postgres.operations import CreateExtension, TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("searchtests", "0004_meeting"),
    ]

    operations = [
        TrigramExtension(),
        CreateExtension("fuzzystrmatch"),
    ]
