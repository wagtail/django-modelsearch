import argparse
import os

import django

from django.core.management import call_command


def main():
    parser = argparse.ArgumentParser(
        description="Run Django tests with a specified search backend."
    )
    parser.add_argument(
        "--backend",
        required=True,
        help="Specify the search backend (db, elasticsearch7, ..., whoosh).",
    )

    args, rest = parser.parse_known_args()

    os.environ["SEARCH_BACKEND"] = args.backend
    os.environ["DJANGO_SETTINGS_MODULE"] = "modelsearch.test.settings"

    django.setup()

    call_command("test", *rest)


if __name__ == "__main__":
    main()
