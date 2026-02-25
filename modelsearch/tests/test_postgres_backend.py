import unittest

from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.db import NotSupportedError, ProgrammingError, connection
from django.test import TestCase
from django.test.utils import override_settings

from modelsearch.models import IndexEntry
from modelsearch.query import Fuzzy, Phrase
from modelsearch.test.testapp import models
from modelsearch.tests.test_backends import BackendTests


@unittest.skipUnless(
    connection.vendor == "postgresql", "The current database is not PostgreSQL"
)
@override_settings(
    MODELSEARCH_BACKENDS={
        "default": {
            "BACKEND": "modelsearch.backends.database.postgres.postgres",
        }
    }
)
class TestPostgresSearchBackend(BackendTests, TestCase):
    backend_path = "modelsearch.backends.database.postgres.postgres"

    def test_weights(self):
        from ..backends.database.postgres.weights import (
            BOOSTS_WEIGHTS,
            WEIGHTS_VALUES,
            determine_boosts_weights,
            get_weight,
        )

        self.assertListEqual(
            BOOSTS_WEIGHTS, [(10, "A"), (2, "B"), (0.5, "C"), (0.25, "D")]
        )
        self.assertListEqual(WEIGHTS_VALUES, [0.025, 0.05, 0.2, 1.0])

        self.assertEqual(get_weight(15), "A")
        self.assertEqual(get_weight(10), "A")
        self.assertEqual(get_weight(9.9), "B")
        self.assertEqual(get_weight(2), "B")
        self.assertEqual(get_weight(1.9), "C")
        self.assertEqual(get_weight(0), "D")
        self.assertEqual(get_weight(-1), "D")

        self.assertListEqual(
            determine_boosts_weights([1]), [(1, "A"), (0, "B"), (0, "C"), (0, "D")]
        )
        self.assertListEqual(
            determine_boosts_weights([-1]), [(-1, "A"), (-1, "B"), (-1, "C"), (-1, "D")]
        )
        self.assertListEqual(
            determine_boosts_weights([-1, 1, 2]),
            [(2, "A"), (1, "B"), (-1, "C"), (-1, "D")],
        )
        self.assertListEqual(
            determine_boosts_weights([0, 1, 2, 3]),
            [(3, "A"), (2, "B"), (1, "C"), (0, "D")],
        )
        self.assertListEqual(
            determine_boosts_weights([0, 0.25, 0.75, 1, 1.5]),
            [(1.5, "A"), (1, "B"), (0.5, "C"), (0, "D")],
        )
        self.assertListEqual(
            determine_boosts_weights([0, 1, 2, 3, 4, 5, 6]),
            [(6, "A"), (4, "B"), (2, "C"), (0, "D")],
        )
        self.assertListEqual(
            determine_boosts_weights([-2, -1, 0, 1, 2, 3, 4]),
            [(4, "A"), (2, "B"), (0, "C"), (-2, "D")],
        )

    def test_search_tsquery_chars(self):
        """
        Checks that tsquery characters are correctly escaped
        and do not generate a PostgreSQL syntax error.
        """

        # Simple quote should be escaped inside each tsquery term.
        results = self.backend.search("L'amour piqué par une abeille", models.Book)
        self.assertCountEqual([r.title for r in results], [])
        results = self.backend.search("'starting quote", models.Book)
        self.assertCountEqual([r.title for r in results], [])
        results = self.backend.search("ending quote'", models.Book)
        self.assertCountEqual([r.title for r in results], [])
        results = self.backend.search("double quo''te", models.Book)
        self.assertCountEqual([r.title for r in results], [])
        results = self.backend.search("triple quo'''te", models.Book)
        self.assertCountEqual([r.title for r in results], [])

        # Now suffixes.
        results = self.backend.search("Something:B", models.Book)
        self.assertCountEqual([r.title for r in results], [])
        results = self.backend.search("Something:*", models.Book)
        self.assertCountEqual([r.title for r in results], [])
        results = self.backend.search("Something:A*BCD", models.Book)
        self.assertCountEqual([r.title for r in results], [])

        # Now the AND operator.
        results = self.backend.search("first & second", models.Book)
        self.assertCountEqual([r.title for r in results], [])

        # Now the OR operator.
        results = self.backend.search("first | second", models.Book)
        self.assertCountEqual([r.title for r in results], [])

        # Now the NOT operator.
        results = self.backend.search("first & !second", models.Book)
        self.assertCountEqual([r.title for r in results], [])

        # Now the phrase operator.
        results = self.backend.search("first <-> second", models.Book)
        self.assertCountEqual([r.title for r in results], [])

    def test_autocomplete_tsquery_chars(self):
        """
        Checks that tsquery characters are correctly escaped
        and do not generate a PostgreSQL syntax error.
        """

        # Simple quote should be escaped inside each tsquery term.
        results = self.backend.autocomplete(
            "L'amour piqué par une abeille", models.Book
        )
        self.assertCountEqual([r.title for r in results], [])
        results = self.backend.autocomplete("'starting quote", models.Book)
        self.assertCountEqual([r.title for r in results], [])
        results = self.backend.autocomplete("ending quote'", models.Book)
        self.assertCountEqual([r.title for r in results], [])
        results = self.backend.autocomplete("double quo''te", models.Book)
        self.assertCountEqual([r.title for r in results], [])
        results = self.backend.autocomplete("triple quo'''te", models.Book)
        self.assertCountEqual([r.title for r in results], [])

        # Backslashes should be escaped inside each tsquery term.
        results = self.backend.autocomplete("backslash\\", models.Book)
        self.assertCountEqual([r.title for r in results], [])

        # Now suffixes.
        results = self.backend.autocomplete("Something:B", models.Book)
        self.assertCountEqual([r.title for r in results], [])
        results = self.backend.autocomplete("Something:*", models.Book)
        self.assertCountEqual([r.title for r in results], [])
        results = self.backend.autocomplete("Something:A*BCD", models.Book)
        self.assertCountEqual([r.title for r in results], [])

        # Now the AND operator.
        results = self.backend.autocomplete("first & second", models.Book)
        self.assertCountEqual([r.title for r in results], [])

        # Now the OR operator.
        results = self.backend.autocomplete("first | second", models.Book)
        self.assertCountEqual([r.title for r in results], [])

        # Now the NOT operator.
        results = self.backend.autocomplete("first & !second", models.Book)
        self.assertCountEqual([r.title for r in results], [])

        # Now the phrase operator.
        results = self.backend.autocomplete("first <-> second", models.Book)
        self.assertCountEqual([r.title for r in results], [])

    @unittest.skip(
        "The Postgres backend doesn't support MatchAll as an inner expression."
    )
    def test_search_not_match_none(self):
        return super().test_search_not_match_none()

    @unittest.skip(
        "The Postgres backend doesn't support MatchAll as an inner expression."
    )
    def test_search_or_match_all(self):
        return super().test_search_or_match_all()

    @unittest.skip(
        "The Postgres backend doesn't support MatchAll as an inner expression."
    )
    def test_search_or_match_none(self):
        return super().test_search_or_match_none()

    @unittest.skip(
        "The Postgres backend doesn't support MatchAll as an inner expression."
    )
    def test_search_and_match_all(self):
        return super().test_search_and_match_all()

    @unittest.skip(
        "The Postgres backend doesn't support MatchAll as an inner expression."
    )
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
        The get_search_field method of PostgresSearchQueryCompiler attempts to support retrieving
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

    def test_fuzzy_search(self):
        """
        Test fuzzy search returns results for approximate matches.
        """
        # "Javascrip" is a typo for "JavaScript" - fuzzy search should find it
        results = self.backend.search(Fuzzy("Javascrip"), models.Book)
        self.assertCountEqual(
            [r.title for r in results],
            ["JavaScript: The good parts", "JavaScript: The Definitive Guide"],
        )

    def test_fuzzy_search_single_word(self):
        """
        Test fuzzy search with a single misspelled word.

        "Pythn" is a typo for "Python". Since fuzzy search goes through
        IndexEntry, it also matches books whose body_text contains "Python"
        (e.g. via get_programming_language_display).
        """
        results = self.backend.search(Fuzzy("Pythn"), models.Book)
        titles = [r.title for r in results]
        self.assertIn("Learning Python", titles)
        self.assertIn("Two Scoops of Django 1.11", titles)

    def test_fuzzy_search_no_match(self):
        """
        Test fuzzy search returns no results when there's no approximate match.
        """
        results = self.backend.search(Fuzzy("xyznonexistent"), models.Book)
        self.assertCountEqual([r.title for r in results], [])

    def test_fuzzy_search_exact_match(self):
        """
        Test fuzzy search also works with exact matches.
        """
        results = self.backend.search(Fuzzy("JavaScript"), models.Book)
        self.assertCountEqual(
            [r.title for r in results],
            ["JavaScript: The good parts", "JavaScript: The Definitive Guide"],
        )

    def test_fuzzy_search_with_operator(self):
        """
        Test fuzzy search with different operators.
        """
        # Default operator is "or"
        results = self.backend.search(Fuzzy("Rust Programming"), models.Book)
        # Should match books with "Rust" or "Programming" in the title
        self.assertTrue(len(list(results)) > 0)

    def test_fuzzy_autocomplete(self):
        """
        Test fuzzy search works with autocomplete.

        "Learnin" is a typo for "Learning" - fuzzy autocomplete should find it.
        """
        results = self.backend.autocomplete(Fuzzy("Learnin"), models.Book)
        self.assertCountEqual(
            [r.title for r in results],
            ["Learning Python"],
        )

    def test_fuzzy_search_without_trigram_extension(self):
        """
        Test that fuzzy search raises NotSupportedError with a helpful message
        when pg_trgm extension is not installed.
        """
        # Simulate the ProgrammingError that PostgreSQL raises when pg_trgm is missing
        error = ProgrammingError(
            "function similarity(character varying, unknown) does not exist"
        )
        with mock.patch(
            "modelsearch.backends.database.postgres.postgres.PostgresSearchResults.get_queryset",
            side_effect=error,
        ):
            with self.assertRaises(NotSupportedError) as context:
                list(self.backend.search(Fuzzy("JavaScript"), models.Book))

            self.assertIn("pg_trgm", str(context.exception))
            self.assertIn("enable_trigram", str(context.exception))


@unittest.skipUnless(
    connection.vendor == "postgresql", "The current database is not PostgreSQL"
)
@override_settings(
    MODELSEARCH_BACKENDS={
        "default": {
            "BACKEND": "modelsearch.backends.database.postgres.postgres",
            "FUZZY_SIMILARITY_THRESHOLD": 0.5,
        }
    }
)
class TestPostgresFuzzyThreshold(TestCase):
    """Test configurable fuzzy similarity threshold."""

    fixtures = ["search"]
    backend_path = "modelsearch.backends.database.postgres.postgres"

    def setUp(self):
        BackendTests.setUp(self)

    def test_fuzzy_threshold_from_settings(self):
        """
        Test that FUZZY_SIMILARITY_THRESHOLD setting is respected.

        "Javsrip" has ~0.375 word similarity to "JavaScript".
        With threshold 0.5, it should NOT match.
        With default threshold 0.3, it would match.
        """
        results = list(self.backend.search(Fuzzy("Javsrip"), models.Book))
        # With threshold 0.5, "Javsrip" (word similarity ~0.375) should not match
        self.assertEqual(results, [])

    def test_backend_has_threshold_attribute(self):
        """Test that the backend has the fuzzy_similarity_threshold attribute."""
        self.assertEqual(self.backend.fuzzy_similarity_threshold, 0.5)


@unittest.skipUnless(
    connection.vendor == "postgresql", "The current database is not PostgreSQL"
)
@override_settings(
    MODELSEARCH_BACKENDS={
        "default": {
            "BACKEND": "modelsearch.backends.database.postgres.postgres",
            "FUZZY_PREFIX_BOOST": 0.5,
        }
    }
)
class TestPostgresFuzzyPrefixBoost(TestCase):
    """Test configurable fuzzy prefix boost."""

    fixtures = ["search"]
    backend_path = "modelsearch.backends.database.postgres.postgres"

    def setUp(self):
        BackendTests.setUp(self)

    def test_backend_has_prefix_boost_attribute(self):
        """Test that the backend has the fuzzy_prefix_boost attribute."""
        self.assertEqual(self.backend.fuzzy_prefix_boost, 0.5)

    def test_fuzzy_prefix_boost_ranking(self):
        """
        Test that prefix boost affects ranking order.

        With prefix boost enabled, a book whose title starts with the query
        should rank higher than one that just contains the query.
        """
        # Create two books - one starts with "Python", one has it in the middle
        models.Book.objects.filter(title__icontains="Python").delete()

        book_prefix = models.Book.objects.create(
            title="Python Programming Guide",
            publication_date="2020-01-01",
            number_of_pages=300,
        )
        book_middle = models.Book.objects.create(
            title="Learning Python Today",
            publication_date="2020-01-01",
            number_of_pages=300,
        )
        self.backend.add(book_prefix)
        self.backend.add(book_middle)

        # Search for "Python" - prefix match should rank higher
        results = list(self.backend.search(Fuzzy("Python"), models.Book))
        titles = [r.title for r in results]

        # Both created books should be found
        self.assertIn("Python Programming Guide", titles)
        self.assertIn("Learning Python Today", titles)

        # The one starting with "Python" should be first due to prefix boost
        self.assertEqual(results[0].title, "Python Programming Guide")


@unittest.skipUnless(
    connection.vendor == "postgresql", "The current database is not PostgreSQL"
)
@override_settings(
    MODELSEARCH_BACKENDS={
        "default": {
            "BACKEND": "modelsearch.backends.database.postgres.postgres",
            "FUZZY_ALGORITHM": "levenshtein",
        }
    }
)
class TestPostgresFuzzyLevenshtein(TestCase):
    """Test Levenshtein distance fuzzy search algorithm."""

    fixtures = ["search"]
    backend_path = "modelsearch.backends.database.postgres.postgres"

    def setUp(self):
        BackendTests.setUp(self)

    def test_backend_has_algorithm_attribute(self):
        """Test that the backend has the fuzzy_algorithm attribute."""
        self.assertEqual(self.backend.fuzzy_algorithm, "levenshtein")

    def test_levenshtein_fuzzy_search(self):
        """
        Test Levenshtein fuzzy search returns results for approximate matches.
        """
        # "Pythn" is a typo for "Python" - 1 edit distance
        results = self.backend.search(Fuzzy("Pythn"), models.Book)
        self.assertIn("Learning Python", [r.title for r in results])

    def test_levenshtein_fuzzy_search_no_match(self):
        """
        Test Levenshtein fuzzy search returns no results when there's no match.
        """
        results = self.backend.search(Fuzzy("xyznonexistent"), models.Book)
        self.assertCountEqual([r.title for r in results], [])

    def test_levenshtein_fuzzy_search_exact_match(self):
        """
        Test Levenshtein fuzzy search works with exact matches.
        """
        results = self.backend.search(Fuzzy("Python"), models.Book)
        self.assertIn("Learning Python", [r.title for r in results])

    def test_levenshtein_without_extension(self):
        """
        Test that Levenshtein search raises NotSupportedError with a helpful
        message when fuzzystrmatch extension is not installed.
        """
        error = ProgrammingError(
            "function levenshtein(character varying, unknown) does not exist"
        )
        with mock.patch(
            "modelsearch.backends.database.postgres.postgres.PostgresSearchResults.get_queryset",
            side_effect=error,
        ):
            with self.assertRaises(NotSupportedError) as context:
                list(self.backend.search(Fuzzy("Python"), models.Book))

            self.assertIn("fuzzystrmatch", str(context.exception))
            self.assertIn("enable_fuzzystrmatch", str(context.exception))


@unittest.skipUnless(
    connection.vendor == "postgresql", "The current database is not PostgreSQL"
)
@override_settings(
    MODELSEARCH_BACKENDS={
        "default": {
            "BACKEND": "modelsearch.backends.database.postgres.postgres",
            "FUZZY_ALGORITHM": "levenshtein",
            "FUZZY_SIMILARITY_THRESHOLD": 0.2,
            "FUZZY_PREFIX_BOOST": 0.5,
        }
    }
)
class TestPostgresFuzzyLevenshteinRanking(TestCase):
    """Test Levenshtein ranking with real-world French examples."""

    backend_path = "modelsearch.backends.database.postgres.postgres"

    def setUp(self):
        from django.conf import settings as django_settings

        from modelsearch.backends import get_search_backend

        for backend_name, backend_conf in django_settings.MODELSEARCH_BACKENDS.items():
            if backend_conf["BACKEND"] == self.backend_path:
                self.backend = get_search_backend(backend_name)
                break

        models.Book.objects.all().delete()

    def _create_and_index(self, titles):
        books = []
        for title in titles:
            book = models.Book.objects.create(
                title=title,
                publication_date="2020-01-01",
                number_of_pages=100,
            )
            self.backend.add(book)
            books.append(book)
        return books

    def test_verre_prefix_match_ranks_first(self):
        """
        Searching "verre" should rank titles starting with "Verre"
        above titles containing "verre" elsewhere.
        """
        self._create_and_index(
            [
                "Ampoule de médicament en verre (vide)",
                "Cloison en verre",
                "Verre à boire",
                "Saladier en verre",
                "Verre plat (Bâtiment / BTP)",
            ]
        )

        results = list(self.backend.search(Fuzzy("verre"), models.Book))
        titles = [r.title for r in results]

        # All should be found
        self.assertEqual(len(titles), 5)

        # The first result should be a title starting with "Verre"
        # thanks to prefix boost
        self.assertIn(titles[0], ["Verre à boire", "Verre plat (Bâtiment / BTP)"])

    def test_hyphenated_word_splitting(self):
        """
        Searching "lave" should match "lave-vaisselle" by splitting on hyphens,
        and "Lave-vaisselle" should rank above "Façade de lave-vaisselle"
        thanks to prefix boost.
        """
        self._create_and_index(
            [
                "Façade de lave-vaisselle",
                "Lave-vaisselle",
                "Lave-linge",
            ]
        )

        results = list(self.backend.search(Fuzzy("lave"), models.Book))
        titles = [r.title for r in results]

        # All three should be found
        self.assertEqual(len(titles), 3)

        # Titles starting with "Lave" should be in the top 2
        self.assertIn(titles[0], ["Lave-vaisselle", "Lave-linge"])
        self.assertIn(titles[1], ["Lave-vaisselle", "Lave-linge"])

        # "Façade de lave-vaisselle" should be last (no prefix boost)
        self.assertEqual(titles[2], "Façade de lave-vaisselle")

    def test_case_insensitive_matching(self):
        """
        Levenshtein matching should be case-insensitive.
        "verre" should match "Verre à boire" with full similarity.
        """
        self._create_and_index(["Verre à boire", "Foundation"])

        results = list(self.backend.search(Fuzzy("verre"), models.Book))
        titles = [r.title for r in results]

        self.assertIn("Verre à boire", titles)
        self.assertNotIn("Foundation", titles)


@unittest.skipUnless(
    connection.vendor == "postgresql", "The current database is not PostgreSQL"
)
@override_settings(
    MODELSEARCH_BACKENDS={
        "default": {
            "BACKEND": "modelsearch.backends.database.postgres.postgres",
        }
    }
)
class TestPostgresFuzzyUnaccent(TestCase):
    """Test accent-insensitive fuzzy search using Fuzzy(unaccent=True)."""

    backend_path = "modelsearch.backends.database.postgres.postgres"

    def setUp(self):
        BackendTests.setUp(self)
        models.Book.objects.all().delete()

    def _create_and_index(self, titles):
        books = []
        for title in titles:
            book = models.Book.objects.create(
                title=title,
                publication_date="2020-01-01",
                number_of_pages=100,
            )
            self.backend.add(book)
            books.append(book)
        return books

    def test_unaccented_query_matches_accented_title(self):
        """
        Searching "ecole" (no accent) with unaccent=True should match
        a title containing "école" (with accent).
        """
        self._create_and_index(["École de musique", "Foundation"])

        results = list(self.backend.search(Fuzzy("ecole", unaccent=True), models.Book))
        titles = [r.title for r in results]

        self.assertIn("École de musique", titles)
        self.assertNotIn("Foundation", titles)

    def test_accented_query_matches_accented_title(self):
        """
        Searching "école" (with accent) with unaccent=True should also match
        "École de musique" since both are stripped of accents before comparison.
        """
        self._create_and_index(["École de musique", "Foundation"])

        results = list(self.backend.search(Fuzzy("école", unaccent=True), models.Book))
        titles = [r.title for r in results]

        self.assertIn("École de musique", titles)

    def test_unaccent_without_extension_raises_helpful_error(self):
        """
        Test that accent-insensitive fuzzy search raises NotSupportedError with
        a helpful message when the unaccent extension or f_unaccent() is missing.
        """
        error = ProgrammingError("function f_unaccent(text) does not exist")
        with mock.patch(
            "modelsearch.backends.database.postgres.postgres.PostgresSearchResults.get_queryset",
            side_effect=error,
        ):
            with self.assertRaises(NotSupportedError) as context:
                list(self.backend.search(Fuzzy("ecole", unaccent=True), models.Book))

            self.assertIn("unaccent", str(context.exception))
            self.assertIn("enable_unaccent", str(context.exception))


@unittest.skipUnless(
    connection.vendor == "postgresql", "The current database is not PostgreSQL"
)
@override_settings(
    MODELSEARCH_BACKENDS={
        "default": {
            "BACKEND": "modelsearch.backends.database.postgres.postgres",
            "SEARCH_CONFIG": "dutch",
        }
    }
)
class TestPostgresLanguageTextSearch(TestCase):
    backend_path = "modelsearch.backends.database.postgres.postgres"

    def setUp(self):
        # get search backend by backend_path
        BackendTests.setUp(self)

        book = models.Book.objects.create(
            title="Nu is beter dan nooit",
            publication_date="1999-05-01",
            number_of_pages=333,
        )
        self.backend.add(book)
        self.book = book

    def test_search_language_plain_text(self):
        results = self.backend.search("Nu is beter dan nooit", models.Book)
        self.assertEqual(list(results), [self.book])

        results = self.backend.search("is beter", models.Book)
        self.assertEqual(list(results), [self.book])

        # search deals even with variations
        results = self.backend.search("zijn beter", models.Book)
        self.assertEqual(list(results), [self.book])

        # search deals even when there are minor typos
        results = self.backend.search("zij beter dan", models.Book)
        self.assertEqual(list(results), [self.book])

    def test_search_language_phrase_text(self):
        results = self.backend.search(Phrase("Nu is beter"), models.Book)
        self.assertEqual(list(results), [self.book])

        results = self.backend.search(Phrase("Nu zijn beter"), models.Book)
        self.assertEqual(list(results), [self.book])
