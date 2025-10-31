import unittest

from unittest import expectedFailure, skip

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test.testcases import TransactionTestCase
from django.test.utils import override_settings

from modelsearch.models import IndexEntry
from modelsearch.query import Not, PlainText
from modelsearch.test.testapp import models
from modelsearch.tests.test_backends import BackendTests


@unittest.skipUnless(connection.vendor == "mysql", "The current database is not MySQL")
@override_settings(
    MODELSEARCH_BACKENDS={
        "default": {
            "BACKEND": "modelsearch.backends.database.mysql.mysql",
        }
    }
)
class TestMySQLSearchBackend(BackendTests, TransactionTestCase):
    backend_path = "modelsearch.backends.database.mysql.mysql"

    # Overrides parent method, because there's a slight difference in what the MySQL backend supports/accepts as search queries.
    def test_not(self):
        all_other_titles = {
            "A Clash of Kings",
            "A Game of Thrones",
            "A Storm of Swords",
            "Foundation",
            "Learning Python",
            "The Hobbit",
            "The Two Towers",
            "The Fellowship of the Ring",
            "The Return of the King",
            "The Rust Programming Language",
            "Two Scoops of Django 1.11",
            "Programming Rust",
        }

        results = self.backend.search(
            Not(PlainText("javascript")), models.Book.objects.all()
        )
        self.assertSetEqual({r.title for r in results}, all_other_titles)

        results = self.backend.search(
            ~PlainText("javascript"), models.Book.objects.all()
        )
        self.assertSetEqual({r.title for r in results}, all_other_titles)

        # Tests multiple words
        results = self.backend.search(
            ~PlainText("javascript the"), models.Book.objects.all()
        )
        # NOTE: The difference with the parent method is here. As we're querying NOT 'javascript the', all entries containing both words should be excluded, but MySQL doesn't index stopwords in FULLTEXT indexes by default, so the JavaScript books won't match the query, since the 'the' word is excluded from the index. Therefore, both books will get returned.
        self.assertSetEqual(
            {r.title for r in results},
            all_other_titles
            | {"JavaScript: The Definitive Guide", "JavaScript: The good parts"},
        )

        # Tests multiple words too, but this time the second word is not a stopword
        results = self.backend.search(
            ~PlainText("javascript parts"), models.Book.objects.all()
        )
        self.assertSetEqual(
            {r.title for r in results},
            all_other_titles | {"JavaScript: The Definitive Guide"},
        )

    def test_empty_search(self):
        results = self.backend.search("", models.Book.objects.all())
        self.assertSetEqual(
            {r.title for r in results},
            set(),
        )

        results = self.backend.search(" ", models.Book.objects.all())
        self.assertSetEqual(
            {r.title for r in results},
            set(),
        )

        results = self.backend.search("*", models.Book.objects.all())
        self.assertSetEqual(
            {r.title for r in results},
            set(),
        )

    def test_empty_autocomplete(self):
        results = self.backend.autocomplete("", models.Book.objects.all())
        self.assertSetEqual(
            {r.title for r in results},
            set(),
        )

        results = self.backend.autocomplete(" ", models.Book.objects.all())
        self.assertSetEqual(
            {r.title for r in results},
            set(),
        )

        results = self.backend.autocomplete("*", models.Book.objects.all())
        self.assertSetEqual(
            {r.title for r in results},
            set(),
        )

    def test_symbols_in_search_term(self):
        # symbols as their own tokens should be ignored
        results = self.backend.search("javascript @ parts", models.Book.objects.all())
        self.assertSetEqual(
            {r.title for r in results},
            {"JavaScript: The good parts"},
        )

        results = self.backend.search("javascript parts @", models.Book.objects.all())
        self.assertSetEqual(
            {r.title for r in results},
            {"JavaScript: The good parts"},
        )

        results = self.backend.search("@ javascript parts", models.Book.objects.all())
        self.assertSetEqual(
            {r.title for r in results},
            {"JavaScript: The good parts"},
        )

        # tokens containing both symbols and alphanumerics should not be discarded
        # or treated as equivalent to the same token without symbols
        results = self.backend.search("java@script parts", models.Book.objects.all())
        self.assertSetEqual(
            {r.title for r in results},
            set(),
        )

    def test_autocomplete_with_symbols(self):
        # the * is not part of the autocomplete mechanism, but if someone includes it
        # we want it to be gracefully ignored
        results = self.backend.autocomplete("parts javasc*", models.Book.objects.all())
        self.assertSetEqual(
            {r.title for r in results},
            {"JavaScript: The good parts"},
        )

    def test_lexeme_validation(self):
        from modelsearch.backends.database.mysql.query import Lexeme

        with self.assertRaisesMessage(ValueError, "Lexeme value cannot be empty."):
            Lexeme("")

        Lexeme("hello")  # no error

        with self.assertRaisesMessage(
            ValueError,
            "Lexeme value 'hello world' must consist of alphanumeric characters "
            "and '_' only.",
        ):
            Lexeme("hello world")

        with self.assertRaisesMessage(
            ValueError,
            "Lexeme value 'rimsky@korsakov' must consist of alphanumeric characters "
            "and '_' only.",
        ):
            Lexeme("rimsky@korsakov")

    @skip(
        "The MySQL backend doesn't support choosing individual fields for the search, only (body, title) or (autocomplete) fields may be searched."
    )
    def test_search_on_individual_field(self):
        return super().test_search_on_individual_field()

    @skip("The MySQL backend doesn't support boosting.")
    def test_search_boosting_on_related_fields(self):
        return super().test_search_boosting_on_related_fields()

    @skip("The MySQL backend doesn't support boosting.")
    def test_boost(self):
        return super().test_boost()

    @expectedFailure
    def test_negated_and(self):
        return super().test_negated_and()

    @expectedFailure
    def test_negated_or(self):
        return super().test_negated_or()

    @unittest.skip("The MySQL backend doesn't support MatchAll as an inner expression.")
    def test_search_not_match_none(self):
        return super().test_search_not_match_none()

    @unittest.skip("The MySQL backend doesn't support MatchAll as an inner expression.")
    def test_search_or_match_all(self):
        return super().test_search_or_match_all()

    @unittest.skip("The MySQL backend doesn't support MatchAll as an inner expression.")
    def test_search_or_match_none(self):
        return super().test_search_or_match_none()

    @unittest.skip("The MySQL backend doesn't support MatchAll as an inner expression.")
    def test_search_and_match_all(self):
        return super().test_search_and_match_all()

    @unittest.skip("The MySQL backend doesn't support MatchAll as an inner expression.")
    def test_search_and_match_none(self):
        return super().test_search_and_match_none()

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
        The get_search_field method of MySQLSearchQueryCompiler attempts to support retrieving
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
