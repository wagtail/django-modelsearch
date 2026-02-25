from django.contrib.postgres.operations import (
    CreateExtension,
    TrigramExtension,
    UnaccentExtension,
)
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("searchtests", "0004_meeting"),
    ]

    operations = [
        TrigramExtension(),
        UnaccentExtension(),
        CreateExtension("fuzzystrmatch"),
    ]
