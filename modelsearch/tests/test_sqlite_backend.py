import sqlite3
import unittest

from unittest import skip

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.testcases import TestCase
from django.test.utils import override_settings

from modelsearch.backends.database.sqlite.utils import fts5_available
from modelsearch.models import IndexEntry
from modelsearch.test.testapp import models
from modelsearch.tests.test_backends import BackendTests


@unittest.skipUnless(
    connection.vendor == "sqlite", "The current database is not SQLite"
)
@unittest.skipIf(
    sqlite3.sqlite_version_info < (3, 19, 0), "This SQLite version is not supported"
)
@unittest.skipUnless(fts5_available(), "The SQLite fts5 extension is not available")
@override_settings(
    MODELSEARCH_BACKENDS={
        "default": {
            "BACKEND": "modelsearch.backends.database.sqlite.sqlite",
        }
    }
)
class TestSQLiteSearchBackend(BackendTests, TestCase):
    backend_path = "modelsearch.backends.database.sqlite.sqlite"

    @skip("The SQLite backend doesn't support boosting.")
    def test_search_boosting_on_related_fields(self):
        return super().test_search_boosting_on_related_fields()

    @skip("The SQLite backend doesn't support boosting.")
    def test_boost(self):
        return super().test_boost()

    @skip("The SQLite backend doesn't score annotations.")
    def test_annotate_score(self):
        return super().test_annotate_score()

    @skip("The SQLite backend doesn't score annotations.")
    def test_annotate_score_with_slice(self):
        return super().test_annotate_score_with_slice()

    @skip("The SQLite backend doesn't support searching on specified fields.")
    def test_autocomplete_with_fields_arg(self):
        return super().test_autocomplete_with_fields_arg()

    @skip("The SQLite backend doesn't guarantee correct ranking of results.")
    def test_ranking(self):
        return super().test_ranking()

    # TODO: figure out why this really fails ("'Not' object has no attribute 'as_sql'")
    @unittest.skip(
        "The SQLite backend doesn't support MatchAll as an inner expression."
    )
    def test_search_not_match_none(self):
        return super().test_search_not_match_none()

    @unittest.skip(
        "The SQLite backend doesn't support MatchAll as an inner expression."
    )
    def test_search_or_match_all(self):
        return super().test_search_or_match_all()

    # TODO: figure out why this fails (returns all results)
    @unittest.skip(
        "The SQLite backend doesn't support MatchAll as an inner expression."
    )
    def test_search_or_match_none(self):
        return super().test_search_or_match_none()

    @unittest.skip(
        "The SQLite backend doesn't support MatchAll as an inner expression."
    )
    def test_search_and_match_all(self):
        return super().test_search_and_match_all()

    def test_reset_indexes(self):
        """
        After running backend.reset_indexes(), search should return no results.
        """
        self.backend.reset_indexes()
        results = self.backend.search("JavaScript", models.Book)
        self.assertEqual(results.count(), 0)

    @unittest.expectedFailure
    def test_get_search_field_for_related_fields(self):
        """
        The get_search_field method of SQLiteSearchQueryCompiler attempts to support retrieving
        search fields across relations with double-underscore notation. This is not yet supported
        in actual searches, so test this in isolation.
        """
        # retrieve an arbitrary SearchResults object to extract a compiler object from
        results = self.backend.search("JavaScript", models.Book)
        compiler = results.query_compiler
        search_field = compiler.get_search_field("authors__name")
        self.assertIsNotNone(search_field)
        self.assertEqual(search_field.field_name, "name")

    def test_index_entry_model(self):
        book = models.Book.objects.get(title="Programming Rust")
        ct = ContentType.objects.get_for_model(models.Book)
        index_entry = IndexEntry.objects.get(object_id=book.id, content_type=ct)
        self.assertEqual(index_entry.model, "book")
        self.assertEqual(str(index_entry), "book: Programming Rust")

        from modelsearch.models import SQLiteFTSIndexEntry

        sqlite_fts_entry = SQLiteFTSIndexEntry.objects.get(index_entry=index_entry)
        self.assertEqual(
            str(sqlite_fts_entry), "SQLiteFTSIndexEntry: book: Programming Rust"
        )
