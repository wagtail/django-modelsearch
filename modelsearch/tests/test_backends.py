import unittest

from collections import OrderedDict
from datetime import date, time
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core import management
from django.db import connection
from django.db.models import F, Q, Subquery
from django.test import TestCase
from django.test.utils import override_settings
from taggit.models import Tag

from modelsearch.backends import (
    InvalidSearchBackendError,
    get_search_backend,
    get_search_backends,
)
from modelsearch.backends.base import BaseSearchBackend, FieldError, FilterFieldError
from modelsearch.backends.database.fallback import DatabaseSearchBackend
from modelsearch.backends.database.sqlite.utils import fts5_available
from modelsearch.models import IndexEntry
from modelsearch.query import (
    MATCH_ALL,
    MATCH_NONE,
    And,
    Boost,
    Not,
    Or,
    Phrase,
    PlainText,
)
from modelsearch.test.testapp import models


class BackendTestSetupMixin:
    def setUp(self):
        # Search MODELSEARCH_BACKENDS for an entry that uses the given backend path
        for backend_name, backend_conf in settings.MODELSEARCH_BACKENDS.items():
            if backend_conf["BACKEND"] == self.backend_path:
                self.backend = get_search_backend(backend_name)
                self.backend_name = backend_name
                break
        else:
            # no conf entry found - skip tests for this backend
            raise unittest.SkipTest(
                f"No MODELSEARCH_BACKENDS entry for the backend {self.backend_path}"
            )

        # HACK: This is a hack to delete all the index entries that may be present in the test database before each test is run.
        self.clear_index_entries()

        management.call_command(
            "rebuild_modelsearch_index",
            backend_name=self.backend_name,
            stdout=StringIO(),
            chunk_size=50,
        )

    def clear_index_entries(self):
        IndexEntry.objects.all().delete()


class BackendTests(BackendTestSetupMixin):
    # To test a specific backend, subclass BackendTests and define self.backend_path.
    backend_path = None

    fixtures = ["search"]

    # SEARCH TESTS

    def test_search_simple(self):
        results = self.backend.search("JavaScript", models.Book)
        self.assertCountEqual(
            [r.title for r in results],
            ["JavaScript: The good parts", "JavaScript: The Definitive Guide"],
        )
        self.assertEqual(results.model, models.Book)

    def test_search_via_queryset(self):
        results = models.Book.objects.search("JavaScript", backend=self.backend_name)
        self.assertCountEqual(
            [r.title for r in results],
            ["JavaScript: The good parts", "JavaScript: The Definitive Guide"],
        )

    def test_search_via_queryset_with_filter(self):
        results = models.Book.objects.filter(number_of_pages__gt=500).search(
            "JavaScript", backend=self.backend_name
        )
        self.assertCountEqual(
            [r.title for r in results],
            ["JavaScript: The Definitive Guide"],
        )

    def test_search_via_queryset_with_order_by_param(self):
        results = models.Book.objects.search(
            "JavaScript", backend=self.backend_name, order_by="number_of_pages"
        )
        self.assertEqual(
            [r.title for r in results],
            [
                "JavaScript: The good parts",
                "JavaScript: The Definitive Guide",
            ],
        )

        results = models.Book.objects.search(
            "JavaScript", backend=self.backend_name, order_by="-number_of_pages"
        )
        self.assertEqual(
            [r.title for r in results],
            [
                "JavaScript: The Definitive Guide",
                "JavaScript: The good parts",
            ],
        )

    def test_search_count(self):
        results = self.backend.search("JavaScript", models.Book)
        self.assertEqual(results.count(), 2)

    def test_search_blank(self):
        # Blank searches should never return anything
        results = self.backend.search("", models.Book)
        self.assertSetEqual(set(results), set())

    def test_search_all(self):
        results = self.backend.search(MATCH_ALL, models.Book)
        self.assertSetEqual(set(results), set(models.Book.objects.all()))

    def test_search_match_none(self):
        results = self.backend.search(MATCH_NONE, models.Book)
        self.assertFalse(list(results))

    def test_search_not_match_none(self):
        results = self.backend.search(Not(MATCH_NONE), models.Book)
        self.assertSetEqual(set(results), set(models.Book.objects.all()))

    def test_search_not_match_all(self):
        results = self.backend.search(Not(MATCH_ALL), models.Book)
        self.assertFalse(list(results))

    def test_search_or_match_none(self):
        results = self.backend.search(PlainText("javascript") | MATCH_NONE, models.Book)
        self.assertCountEqual(
            [r.title for r in results],
            ["JavaScript: The good parts", "JavaScript: The Definitive Guide"],
        )

    def test_search_or_match_all(self):
        results = self.backend.search(PlainText("javascript") | MATCH_ALL, models.Book)
        self.assertSetEqual(set(results), set(models.Book.objects.all()))

    def test_search_and_match_all(self):
        results = self.backend.search(PlainText("javascript") & MATCH_ALL, models.Book)
        self.assertCountEqual(
            [r.title for r in results],
            ["JavaScript: The good parts", "JavaScript: The Definitive Guide"],
        )

    def test_search_and_match_none(self):
        results = self.backend.search(PlainText("javascript") & MATCH_NONE, models.Book)
        self.assertFalse(list(results))

    def test_search_does_not_return_results_from_wrong_model(self):
        # https://github.com/wagtail/wagtail/issues/10188 - if a term matches some other
        # model to the one being searched, this match should not leak into the results
        # (e.g. returning the object with the same ID)
        results = self.backend.search("thrones", models.Author)
        self.assertSetEqual(set(results), set())

    def test_ranking(self):
        # Note: also tests the "or" operator
        results = list(
            self.backend.search("JavaScript Definitive", models.Book, operator="or")
        )
        # "JavaScript: The Definitive Guide" should be first
        self.assertEqual(
            [r.title for r in results],
            ["JavaScript: The Definitive Guide", "JavaScript: The good parts"],
        )

        self.assertEqual(results[0].title, "JavaScript: The Definitive Guide")

        results = list(
            self.backend.search("JavaScript good", models.Book, operator="or")
        )
        # "JavaScript: The good parts" should be first
        self.assertEqual(
            [r.title for r in results],
            ["JavaScript: The good parts", "JavaScript: The Definitive Guide"],
        )

    def test_annotate_score(self):
        results = self.backend.search("JavaScript", models.Book).annotate_score(
            "_score"
        )

        for result in results:
            self.assertIsInstance(result._score, float)

    def test_annotate_score_with_slice(self):
        # #3431 - Annotate score wasn't being passed to new queryset when slicing
        results = self.backend.search("JavaScript", models.Book).annotate_score(
            "_score"
        )[:10]

        for result in results:
            self.assertIsInstance(result._score, float)

    def test_multiple_slice(self):
        results = self.backend.search(MATCH_ALL, models.Book)
        sliced_results = results[:3][:6]
        self.assertEqual(len(sliced_results), 3)

    def test_count_should_respect_slicing(self):
        results = self.backend.search(MATCH_ALL, models.Book)
        sliced_results = results[2:5]
        self.assertEqual(sliced_results.count(), 3)

    def test_count_cache(self):
        results = self.backend.search("JavaScript", models.Book)
        self.assertEqual(results.count(), 2)
        with self.assertNumQueries(0):
            self.assertEqual(results.count(), 2)

    def test_results_cache(self):
        results = self.backend.search("JavaScript", models.Book)
        self.assertEqual(len(list(results)), 2)
        with self.assertNumQueries(0):
            self.assertEqual(results.count(), 2)
            self.assertEqual(len(list(results)), 2)

    def test_search_and_operator(self):
        # Should not return "JavaScript: The good parts" as it does not have "Definitive"
        results = self.backend.search(
            "JavaScript Definitive", models.Book, operator="and"
        )
        self.assertCountEqual(
            [r.title for r in results], ["JavaScript: The Definitive Guide"]
        )

    def test_search_on_child_class(self):
        # Searches on a child class should only return results that have the child class as well
        # and all results should be instances of the child class
        results = self.backend.search(MATCH_ALL, models.Novel)
        self.assertSetEqual(set(results), set(models.Novel.objects.all()))

    def test_search_child_class_field_from_parent(self):
        # Searches the Book model for content that exists in the Novel model
        # Note: "Westeros" only occurs in the Novel.setting field
        # All results should be instances of the parent class
        results = self.backend.search("Westeros", models.Book)

        self.assertCountEqual(
            [r.title for r in results],
            ["A Game of Thrones", "A Clash of Kings", "A Storm of Swords"],
        )

        self.assertIsInstance(results[0], models.Book)

    def test_search_on_individual_field(self):
        # The following query shouldn't search the Novel.setting field so none
        # of the Novels set in "Westeros" should be returned
        results = self.backend.search(
            "Westeros Hobbit", models.Book, fields=["title"], operator="or"
        )

        self.assertCountEqual([r.title for r in results], ["The Hobbit"])

    def test_search_on_no_fields(self):
        # fields=[] should return no results
        results = self.backend.search(
            "hobbit",
            models.Book,
            fields=[],
        )

        self.assertCountEqual([r.title for r in results], [])

    def test_search_on_unknown_field(self):
        with self.assertRaises(FieldError):
            list(
                self.backend.search(
                    "Westeros Hobbit", models.Book, fields=["unknown"], operator="or"
                )
            )

    def test_search_on_non_searchable_field(self):
        with self.assertRaises(FieldError):
            list(
                self.backend.search(
                    "Westeros Hobbit",
                    models.Book,
                    fields=["number_of_pages"],
                    operator="or",
                )
            )

    def test_search_on_related_fields(self):
        results = self.backend.search("Bilbo Baggins", models.Novel)

        self.assertCountEqual(
            [r.title for r in results],
            [
                "The Hobbit",
                "The Fellowship of the Ring",
                "The Two Towers",
                "The Return of the King",
            ],
        )

    def test_search_on_related_fields_reverse_one_to_one(self):
        # "hobbit" is part of the search record for Bilbo Baggins via RelatedFields("novel_as_protagonist")
        results = self.backend.search("hobbit", models.Character)

        self.assertCountEqual(
            [r.name for r in results],
            [
                "Bilbo Baggins",
            ],
        )

    def test_search_boosting_on_related_fields(self):
        # Bilbo Baggins is the protagonist of "The Hobbit" but not any of the "Lord of the Rings" novels.
        # As the protagonist has more boost than other characters, "The Hobbit" should always be returned
        # first
        results = list(self.backend.search("Bilbo Baggins", models.Novel))

        self.assertEqual(results[0].title, "The Hobbit")

        # The remaining results should be scored equally so their rank is undefined
        self.assertCountEqual(
            [r.title for r in results[1:]],
            ["The Fellowship of the Ring", "The Two Towers", "The Return of the King"],
        )

    def test_search_on_nested_related_fields(self):
        results = list(self.backend.search("Doyle", models.Meeting))
        self.assertCountEqual(
            [r.name for r in results],
            ["Stand-up meeting"],
        )

    def test_search_callable_field(self):
        # "Django Two scoops" only mentions "Python" in its "get_programming_language_display"
        # callable field
        results = self.backend.search("Python", models.Book)

        self.assertCountEqual(
            [r.title for r in results], ["Learning Python", "Two Scoops of Django 1.11"]
        )

    def test_search_all_unindexed(self):
        self.backend.add_bulk(models.UnindexedBook, models.UnindexedBook.objects.all())
        self.backend.refresh_indexes()
        # There should be no index entries for UnindexedBook
        results = self.backend.search(MATCH_ALL, models.UnindexedBook)
        self.assertEqual(results.count(), 0)
        self.assertEqual(len(results), 0)
        self.assertEqual(results[:10].count(), 0)

    # AUTOCOMPLETE TESTS

    def test_autocomplete(self):
        # This one shouldn't match "Django Two scoops" as "get_programming_language_display"
        # isn't an autocomplete field
        results = self.backend.autocomplete("Py", models.Book)

        self.assertCountEqual(
            [r.title for r in results],
            [
                "Learning Python",
            ],
        )

    def test_autocomplete_via_queryset(self):
        results = models.Book.objects.autocomplete("Py", backend=self.backend_name)

        self.assertCountEqual(
            [r.title for r in results],
            [
                "Learning Python",
            ],
        )

    def test_autocomplete_via_queryset_with_filter(self):
        results = models.Book.objects.filter(number_of_pages__gt=500).autocomplete(
            "Javasc", backend=self.backend_name
        )

        self.assertCountEqual(
            [r.title for r in results],
            [
                "JavaScript: The Definitive Guide",
            ],
        )

    def test_autocomplete_via_queryset_with_order_by_param(self):
        results = models.Book.objects.autocomplete(
            "JavaSc", backend=self.backend_name, order_by="number_of_pages"
        )
        self.assertEqual(
            [r.title for r in results],
            [
                "JavaScript: The good parts",
                "JavaScript: The Definitive Guide",
            ],
        )

        results = models.Book.objects.autocomplete(
            "JavaSc", backend=self.backend_name, order_by="-number_of_pages"
        )
        self.assertEqual(
            [r.title for r in results],
            [
                "JavaScript: The Definitive Guide",
                "JavaScript: The good parts",
            ],
        )

    def test_autocomplete_uses_autocompletefield(self):
        # Autocomplete should only require an AutocompleteField, not a SearchField
        # TODO: given that partial_match=True has no effect as of Wagtail 5, also test that
        # AutocompleteField is actually being respected, and it's not just relying on the
        # presence of a SearchField (with or without partial_match)
        results = self.backend.autocomplete("Georg", models.Author)
        self.assertCountEqual(
            [r.name for r in results],
            [
                "George R.R. Martin",
            ],
        )

    def test_autocomplete_with_fields_arg(self):
        results = self.backend.autocomplete("Georg", models.Author, fields=["name"])
        self.assertCountEqual(
            [r.name for r in results],
            [
                "George R.R. Martin",
            ],
        )

    def test_autocomplete_not_affected_by_stemming(self):
        # If SEARCH_CONFIG is set, stemming will be enabled.
        # But we want to disable this for autocomplete as stemmed words don't always match on prefixes
        # See: https://www.postgresql.org/docs/9.1/datatype-textsearch.html#DATATYPE-TSQUERY
        results = self.backend.autocomplete("Learni", models.Book)

        self.assertCountEqual(
            [r.title for r in results],
            [
                "Learning Python",
            ],
        )

    def test_autocomplete_hyphenated_term(self):
        models.Book.objects.create(
            title="Poseidon-1234ABC",
            number_of_pages=350,
            publication_date=date(1961, 11, 10),
        )
        self.backend.get_index_for_model(models.Book).refresh()

        results = self.backend.autocomplete("poseidon-1234", models.Book)

        self.assertCountEqual(
            [r.title for r in results],
            [
                "Poseidon-1234ABC",
            ],
        )

    def test_autocomplete_trailing_hyphen(self):
        models.Book.objects.create(
            title="Poseidon-1234ABC",
            number_of_pages=350,
            publication_date=date(1961, 11, 10),
        )
        self.backend.get_index_for_model(models.Book).refresh()

        results = self.backend.autocomplete("poseidon-", models.Book)

        self.assertCountEqual(
            [r.title for r in results],
            [
                "Poseidon-1234ABC",
            ],
        )

    # FILTERING TESTS

    def test_filter_exact_value(self):
        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(number_of_pages=440)
        )

        self.assertCountEqual(
            [r.title for r in results],
            ["The Return of the King", "The Rust Programming Language"],
        )

    def test_filter_exact_value_on_parent_model_field(self):
        results = self.backend.search(
            MATCH_ALL, models.Novel.objects.filter(number_of_pages=440)
        )

        self.assertCountEqual([r.title for r in results], ["The Return of the King"])

    def test_filter_exact_values_list_subquery(self):
        protagonist = (
            models.Character.objects.filter(name="Frodo Baggins")
            .order_by("novel_id")
            .values_list("pk", flat=True)[:1]
        )
        cases = {
            "implicit": protagonist,
            "explicit": Subquery(protagonist),
        }

        for case, subquery in cases.items():
            with self.subTest(case=case):
                results = self.backend.search(
                    MATCH_ALL,
                    models.Novel.objects.filter(protagonist_id=subquery),
                )

                self.assertCountEqual(
                    [r.title for r in results],
                    ["The Fellowship of the Ring"],
                )

    def test_filter_lt(self):
        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(number_of_pages__lt=440)
        )

        self.assertCountEqual(
            [r.title for r in results],
            [
                "The Hobbit",
                "JavaScript: The good parts",
                "The Fellowship of the Ring",
                "Foundation",
                "The Two Towers",
            ],
        )

    def test_filter_lte(self):
        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(number_of_pages__lte=440)
        )

        self.assertCountEqual(
            [r.title for r in results],
            [
                "The Return of the King",
                "The Rust Programming Language",
                "The Hobbit",
                "JavaScript: The good parts",
                "The Fellowship of the Ring",
                "Foundation",
                "The Two Towers",
            ],
        )

    def test_filter_gt(self):
        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(number_of_pages__gt=440)
        )

        self.assertCountEqual(
            [r.title for r in results],
            [
                "JavaScript: The Definitive Guide",
                "Learning Python",
                "A Clash of Kings",
                "A Game of Thrones",
                "Two Scoops of Django 1.11",
                "A Storm of Swords",
                "Programming Rust",
            ],
        )

    def test_filter_gte(self):
        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(number_of_pages__gte=440)
        )

        self.assertCountEqual(
            [r.title for r in results],
            [
                "The Return of the King",
                "The Rust Programming Language",
                "JavaScript: The Definitive Guide",
                "Learning Python",
                "A Clash of Kings",
                "A Game of Thrones",
                "Two Scoops of Django 1.11",
                "A Storm of Swords",
                "Programming Rust",
            ],
        )

    def test_filter_or(self):
        results = self.backend.search(
            MATCH_ALL,
            models.Book.objects.filter(
                Q(number_of_pages=440) | Q(number_of_pages=1160)
            ),
        )

        self.assertCountEqual(
            [r.title for r in results],
            [
                "The Return of the King",
                "The Rust Programming Language",
                "Learning Python",
            ],
        )

    def test_filter_not(self):
        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(~Q(number_of_pages__gt=200))
        )

        self.assertCountEqual(
            [r.title for r in results],
            [
                "JavaScript: The good parts",
            ],
        )

    def test_filter_in_list(self):
        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(number_of_pages__in=[440, 1160])
        )

        self.assertCountEqual(
            [r.title for r in results],
            [
                "The Return of the King",
                "The Rust Programming Language",
                "Learning Python",
            ],
        )

    def test_filter_in_iterable(self):
        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(number_of_pages__in=iter([440, 1160]))
        )

        self.assertCountEqual(
            [r.title for r in results],
            [
                "The Return of the King",
                "The Rust Programming Language",
                "Learning Python",
            ],
        )

    def test_filter_in_values_list_subquery(self):
        values = models.Book.objects.filter(number_of_pages__lt=440).values_list(
            "number_of_pages", flat=True
        )
        cases = {
            "implicit": values,
            "explicit": Subquery(values),
        }
        for case, subquery in cases.items():
            with self.subTest(case=case):
                results = self.backend.search(
                    MATCH_ALL, models.Book.objects.filter(number_of_pages__in=subquery)
                )

                self.assertCountEqual(
                    [r.title for r in results],
                    [
                        "The Hobbit",
                        "JavaScript: The good parts",
                        "The Fellowship of the Ring",
                        "Foundation",
                        "The Two Towers",
                    ],
                )

    def test_filter_isnull_true(self):
        # Note: We don't know the birth dates of any of the programming guide authors
        results = self.backend.search(
            MATCH_ALL, models.Author.objects.filter(date_of_birth__isnull=True)
        )

        self.assertCountEqual(
            [r.name for r in results],
            [
                "David Ascher",
                "Mark Lutz",
                "David Flanagan",
                "Douglas Crockford",
                "Daniel Roy Greenfeld",
                "Audrey Roy Greenfeld",
                "Carol Nichols",
                "Steve Klabnik",
                "Jim Blandy",
                "Jason Orendorff",
            ],
        )

    def test_filter_isnull_false(self):
        # Note: We know the birth dates of all of the novel authors
        results = self.backend.search(
            MATCH_ALL, models.Author.objects.filter(date_of_birth__isnull=False)
        )

        self.assertCountEqual(
            [r.name for r in results],
            ["Isaac Asimov", "George R.R. Martin", "J. R. R. Tolkien"],
        )

    def test_filter_prefix(self):
        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(title__startswith="Th")
        )

        self.assertCountEqual(
            [r.title for r in results],
            [
                "The Hobbit",
                "The Fellowship of the Ring",
                "The Two Towers",
                "The Return of the King",
                "The Rust Programming Language",
            ],
        )

    def test_filter_and_operator(self):
        results = self.backend.search(
            MATCH_ALL,
            models.Book.objects.filter(number_of_pages=440)
            & models.Book.objects.filter(publication_date=date(1955, 10, 20)),
        )

        self.assertCountEqual([r.title for r in results], ["The Return of the King"])

    def test_filter_or_operator(self):
        results = self.backend.search(
            MATCH_ALL,
            models.Book.objects.filter(number_of_pages=440)
            | models.Book.objects.filter(number_of_pages=1160),
        )

        self.assertCountEqual(
            [r.title for r in results],
            [
                "Learning Python",
                "The Return of the King",
                "The Rust Programming Language",
            ],
        )

    def test_filter_on_non_filterable_field(self):
        with self.assertRaises(FieldError):
            list(
                self.backend.search(
                    MATCH_ALL, models.Author.objects.filter(name__startswith="Issac")
                )
            )

    def test_search_with_date_filter(self):
        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(publication_date__gt=date(2000, 6, 1))
        )
        self.assertEqual(len(results), 4)

        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(publication_date__year__gte=2000)
        )
        self.assertEqual(len(results), 5)

        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(publication_date__year__gt=2000)
        )
        self.assertEqual(len(results), 4)

        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(publication_date__year__lte=1954)
        )
        self.assertEqual(len(results), 4)

        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(publication_date__year__lt=1954)
        )
        self.assertEqual(len(results), 2)

        results = self.backend.search(
            MATCH_ALL, models.Book.objects.filter(publication_date__year=1954)
        )
        self.assertEqual(len(results), 2)

    def test_search_with_time_filter(self):
        results = self.backend.search(
            "yoga", models.Meeting.objects.filter(start_time=time(8, 0))
        )
        self.assertEqual(len(results), 1)

    def test_search_with_time_lt_filter(self):
        results = self.backend.search(
            "yoga", models.Meeting.objects.filter(start_time__lt=time(12, 0))
        )
        self.assertEqual(len(results), 1)

    def test_search_with_time_range_filter(self):
        results = self.backend.search(
            "yoga",
            models.Meeting.objects.filter(start_time__range=(time(7, 0), time(9, 0))),
        )
        self.assertEqual(len(results), 1)

    def test_search_with_time_in_filter(self):
        results = self.backend.search(
            "yoga",
            models.Meeting.objects.filter(start_time__in=(time(7, 0), time(8, 0))),
        )
        self.assertEqual(len(results), 1)

    def test_child_model_with_id_filter(self):
        learning_python = models.ProgrammingGuide.objects.get(title="Learning Python")
        results = self.backend.search(
            "Python", models.ProgrammingGuide.objects.filter(id=learning_python.id)
        )
        self.assertEqual(set(results), {learning_python})

    def test_filter_on_related_fields_one_to_many(self):
        results = list(
            self.backend.search(
                "king", models.Novel.objects.filter(characters__name="Frodo Baggins")
            )
        )
        self.assertCountEqual(
            [r.title for r in results],
            ["The Return of the King"],
        )

        results = list(
            self.backend.search(
                "thrones", models.Novel.objects.filter(characters__name="Frodo Baggins")
            )
        )
        self.assertCountEqual(
            [r.title for r in results],
            [],
        )

    def test_filter_on_related_fields_foreign_key(self):
        results = list(
            self.backend.search(
                "thorin", models.Character.objects.filter(novel__setting="Middle Earth")
            )
        )
        self.assertCountEqual(
            [r.name for r in results],
            ["Thorin Oakenshield"],
        )

        results = list(
            self.backend.search(
                "thorin", models.Character.objects.filter(novel__setting="Westeros")
            )
        )
        self.assertCountEqual(
            [r.name for r in results],
            [],
        )

    def test_filter_on_related_fields_one_to_one(self):
        results = list(
            self.backend.search(
                "hobbit", models.Novel.objects.filter(protagonist__name="Bilbo Baggins")
            )
        )
        self.assertCountEqual(
            [r.title for r in results],
            ["The Hobbit"],
        )

        results = list(
            self.backend.search(
                "hobbit", models.Novel.objects.filter(protagonist__name="Frodo Baggins")
            )
        )
        self.assertCountEqual(
            [r.title for r in results],
            [],
        )

    def test_filter_on_related_fields_reverse_one_to_one(self):
        results = list(
            self.backend.search(
                "baggins",
                models.Character.objects.filter(
                    novel_as_protagonist__title="The Hobbit"
                ),
            )
        )
        self.assertCountEqual(
            [r.name for r in results],
            ["Bilbo Baggins"],
        )

    def test_filter_on_related_fields_forward_many_to_many(self):
        results = list(
            self.backend.search(
                "hobbit",
                models.Book.objects.filter(authors__date_of_birth=date(1892, 1, 3)),
            )
        )
        self.assertCountEqual(
            [r.title for r in results],
            ["The Hobbit"],
        )
        results = list(
            self.backend.search(
                "hobbit",
                models.Book.objects.filter(authors__date_of_birth=date(1920, 1, 2)),
            )
        )
        self.assertCountEqual(
            [r.title for r in results],
            [],
        )

    def test_filter_on_related_fields_reverse_many_to_many(self):
        results = list(
            self.backend.search(
                "tolkien",
                models.Author.objects.filter(books__publication_date=date(1954, 7, 29)),
            )
        )
        self.assertCountEqual(
            [r.name for r in results],
            ["J. R. R. Tolkien"],
        )
        results = list(
            self.backend.search(
                "tolkien",
                models.Author.objects.filter(books__publication_date=date(2000, 1, 1)),
            )
        )
        self.assertCountEqual(
            [r.name for r in results],
            [],
        )

    def test_multiple_filters_on_related_fields(self):
        results = list(
            self.backend.search(
                "tolkien",
                models.Author.objects.filter(
                    books__publication_date=date(1954, 7, 29),
                    books__number_of_pages__gt=400,
                ),
            )
        )
        self.assertCountEqual(
            [r.name for r in results],
            ["J. R. R. Tolkien"],
        )
        results = list(
            self.backend.search(
                "tolkien",
                models.Author.objects.filter(
                    # There is no single book that matches both of these criteria, so no results should be returned
                    # (even though there are matches for the individual criteria)
                    books__publication_date=date(1954, 7, 29),
                    books__number_of_pages__lt=400,
                ),
            )
        )
        self.assertCountEqual(
            [r.name for r in results],
            [],
        )

    def test_missing_filter_field(self):
        with self.assertRaisesMessage(
            FilterFieldError,
            'Cannot filter search results with field "name". Please add index.FilterField("name") to Author.search_fields.',
        ):
            list(
                self.backend.search(
                    MATCH_ALL, models.Author.objects.filter(name="Isaac Asimov")
                )
            )

    def test_missing_filter_field_in_related_fields(self):
        with self.assertRaisesMessage(
            FilterFieldError,
            'Cannot filter search results with field "publication_date". Please add index.FilterField("publication_date") to the RelatedFields("novel_as_protagonist") definition in Character.search_fields.',
        ):
            list(
                self.backend.search(
                    MATCH_ALL,
                    models.Character.objects.filter(
                        novel_as_protagonist__publication_date=date(1937, 9, 21)
                    ),
                )
            )

    def test_missing_related_fields(self):
        with self.assertRaisesMessage(
            FilterFieldError,
            'Cannot filter search results with field "name". Please add a suitable index.RelatedFields definition to Character.search_fields.',
        ):
            list(
                self.backend.search(
                    MATCH_ALL,
                    models.Character.objects.filter(
                        novel__authors__name="J. R. R. Tolkien"
                    ),
                )
            )

    # TREEBEARD FILTERING TESTS
    def test_get_ancestors_filter(self):
        for model in (models.MPAnimal, models.NSAnimal):
            with self.subTest(model=model):
                dog = model.objects.get(name="Dog")
                results = self.backend.search("mammal", dog.get_ancestors())
                self.assertCountEqual(
                    [r.name for r in results],
                    ["Mammal"],
                )
                results = self.backend.search("reptile", dog.get_ancestors())
                self.assertCountEqual(
                    [r.name for r in results],
                    [],
                )

    def test_get_children_filter(self):
        for model in (models.MPAnimal, models.NSAnimal):
            with self.subTest(model=model):
                animal = model.objects.get(name="Animal")
                results = self.backend.search("mammal", animal.get_children())
                self.assertCountEqual(
                    [r.name for r in results],
                    ["Mammal"],
                )
                results = self.backend.search("dog", animal.get_children())
                # dog is a grandchild, not a child, of animal, so shouldn't be returned
                self.assertCountEqual(
                    [r.name for r in results],
                    [],
                )

    def test_get_descendants_filter(self):
        for model in (models.MPAnimal, models.NSAnimal):
            with self.subTest(model=model):
                animal = model.objects.get(name="Animal")
                results = self.backend.search("dog", animal.get_descendants())
                self.assertCountEqual(
                    [r.name for r in results],
                    ["Dog"],
                )
                reptile = models.MPAnimal.objects.get(name="Reptile")
                results = self.backend.search("dog", reptile.get_descendants())
                self.assertCountEqual(
                    [r.name for r in results],
                    [],
                )

    def test_get_descendants_filter_after_move(self):
        for model in (models.MPAnimal, models.NSAnimal):
            with self.subTest(model=model):
                index = self.backend.get_index_for_model(model)

                # Create Labradoodle as a child of Animal
                animal = model.objects.get(name="Animal")
                labradoodle = animal.add_child(name="Labradoodle")

                # Move Labradoodle to be a child of Dog
                dog = model.objects.get(name="Dog")
                labradoodle.move(dog, pos="first-child")
                index.refresh()

                # If the move operation was successfully reflected in the search index,
                # Labradoodle should now be returned as a descendant of Dog
                results = self.backend.search("labradoodle", dog.get_descendants())
                self.assertCountEqual(
                    [r.name for r in results],
                    ["Labradoodle"],
                )

    def test_get_descendants_filter_after_move_to_root(self):
        for model in (models.MPAnimal, models.NSAnimal):
            with self.subTest(model=model):
                index = self.backend.get_index_for_model(model)

                # Create Mushroom as a child of Animal
                animal = model.objects.get(name="Animal")
                mushroom = animal.add_child(name="Mushroom")
                # Move Mushroom to be a root node ordered before Animal
                mushroom.move(animal, pos="first-sibling")
                index.refresh()

                # Under the NS_Node implementation, Animal now has a tree_id of 2 and a
                # tree_ids_incremented signal was sent. Searching descendants of Animal will filter
                # by tree_id=2, which will only succeed on non-database backends if the
                # tree_ids_incremented signal was appropriately handled.
                animal = model.objects.get(name="Animal")
                results = self.backend.search("dog", animal.get_descendants())
                self.assertCountEqual(
                    [r.name for r in results],
                    ["Dog"],
                )

    def test_get_siblings_filter(self):
        for model in (models.MPAnimal, models.NSAnimal):
            with self.subTest(model=model):
                dog = model.objects.get(name="Dog")
                results = self.backend.search("cat", dog.get_siblings())
                self.assertCountEqual(
                    [r.name for r in results],
                    ["Cat"],
                )
                results = self.backend.search("mammal", dog.get_siblings())
                self.assertCountEqual(
                    [r.name for r in results],
                    [],
                )

    def test_get_root_nodes_filter(self):
        for model in (models.MPAnimal, models.NSAnimal):
            with self.subTest(model=model):
                results = self.backend.search("animal", model.get_root_nodes())
                self.assertCountEqual(
                    [r.name for r in results],
                    ["Animal"],
                )
                results = self.backend.search("mammal", model.get_root_nodes())
                self.assertCountEqual(
                    [r.name for r in results],
                    [],
                )

    def test_search_after_delete_subtree(self):
        for model in (models.MPAnimal, models.NSAnimal):
            with self.subTest(model=model):
                index = self.backend.get_index_for_model(model)

                result_count = self.backend.search("dog", model).count()
                self.assertEqual(result_count, 1)

                # Delete the Mammal subtree
                mammal = model.objects.get(name="Mammal")
                mammal.delete()
                index.refresh()

                result_count = self.backend.search("dog", model).count()
                self.assertEqual(result_count, 0)

    # ORDER BY RELEVANCE

    def test_order_by_relevance_match_all(self):
        results = self.backend.search(
            MATCH_ALL,
            models.Novel.objects.order_by("number_of_pages"),
            order_by_relevance=False,
        )

        # Ordering should be set to "number_of_pages"
        self.assertEqual(
            [r.title for r in results],
            [
                "Foundation",
                "The Hobbit",
                "The Two Towers",
                "The Fellowship of the Ring",
                "The Return of the King",
                "A Game of Thrones",
                "A Clash of Kings",
                "A Storm of Swords",
            ],
        )

    def test_order_by_relevance_false_with_real_search(self):
        # MATCH_ALL searches will often short-circuit the actual search query logic, so
        # we should do a non-MATCH_ALL query to ensure full coverage
        results = self.backend.search(
            "javascript",
            models.Book.objects.order_by("number_of_pages"),
            order_by_relevance=False,
        )

        self.assertEqual(
            [r.title for r in results],
            [
                "JavaScript: The good parts",
                "JavaScript: The Definitive Guide",
            ],
        )

        results = self.backend.search(
            "javascript",
            models.Book.objects.order_by("-number_of_pages"),
            order_by_relevance=False,
        )

        self.assertEqual(
            [r.title for r in results],
            [
                "JavaScript: The Definitive Guide",
                "JavaScript: The good parts",
            ],
        )

    def test_order_by_relevance_sliced(self):
        results = self.backend.search(
            "javascript",
            models.Book.objects.order_by("number_of_pages"),
            order_by_relevance=False,
        )[:1]

        self.assertEqual(
            [r.title for r in results],
            [
                "JavaScript: The good parts",
            ],
        )

        results = self.backend.search(
            "javascript",
            models.Book.objects.order_by("-number_of_pages"),
            order_by_relevance=False,
        )[:1]

        self.assertEqual(
            [r.title for r in results],
            [
                "JavaScript: The Definitive Guide",
            ],
        )

    def test_order_by_relevance_false_with_no_ordering_set(self):
        # If no ordering is set on the queryset, order by PK descending
        results = self.backend.search(
            "javascript",
            models.Book.objects.order_by(),
            order_by_relevance=False,
        )

        self.assertEqual(
            [r.title for r in results],
            [
                "JavaScript: The Definitive Guide",
                "JavaScript: The good parts",
            ],
        )

    def test_order_by_time(self):
        results = self.backend.search(
            "yoga",
            models.Meeting.objects.order_by("start_time"),
            order_by_relevance=False,
        )

        self.assertEqual(
            [r.name for r in results],
            [
                "Breakfast yoga",
                "Evening yoga",
            ],
        )

    def test_order_by_non_filterable_field(self):
        with self.assertRaises(FieldError):
            list(
                self.backend.search(
                    MATCH_ALL,
                    models.Author.objects.order_by("name"),
                    order_by_relevance=False,
                )
            )

    def test_order_by_expression(self):
        results = self.backend.search(
            "javascript",
            models.Book.objects.order_by(F("title").asc(nulls_first=True)),
            order_by_relevance=False,
        )

        self.assertEqual(
            [r.title for r in results],
            [
                "JavaScript: The Definitive Guide",
                "JavaScript: The good parts",
            ],
        )

    # SLICING TESTS

    def test_single_result(self):
        results = self.backend.search(
            MATCH_ALL,
            models.Novel.objects.order_by("number_of_pages"),
            order_by_relevance=False,
        )

        self.assertEqual(results[0].title, "Foundation")
        self.assertEqual(results[1].title, "The Hobbit")

    def test_limit(self):
        # Note: we need consistent ordering for this test
        results = self.backend.search(
            MATCH_ALL,
            models.Novel.objects.order_by("number_of_pages"),
            order_by_relevance=False,
        )

        # Limit the results
        results = results[:3]

        self.assertListEqual(
            [r.title for r in results], ["Foundation", "The Hobbit", "The Two Towers"]
        )

    def test_offset(self):
        # Note: we need consistent ordering for this test
        results = self.backend.search(
            MATCH_ALL,
            models.Novel.objects.order_by("number_of_pages"),
            order_by_relevance=False,
        )

        # Offset the results
        results = results[3:]

        self.assertListEqual(
            [r.title for r in results],
            [
                "The Fellowship of the Ring",
                "The Return of the King",
                "A Game of Thrones",
                "A Clash of Kings",
                "A Storm of Swords",
            ],
        )

    def test_offset_and_limit(self):
        # Note: we need consistent ordering for this test
        results = self.backend.search(
            MATCH_ALL,
            models.Novel.objects.order_by("number_of_pages"),
            order_by_relevance=False,
        )

        # Offset the results
        results = results[3:6]

        self.assertListEqual(
            [r.title for r in results],
            [
                "The Fellowship of the Ring",
                "The Return of the King",
                "A Game of Thrones",
            ],
        )

    def test_filter_none(self):
        results = self.backend.search(MATCH_ALL, models.Book.objects.none())
        self.assertListEqual(list(results), [])

        results = self.backend.search("JavaScript", models.Book.objects.none())
        self.assertListEqual(list(results), [])

    # FACET TESTS

    def test_facet(self):
        results = self.backend.search(MATCH_ALL, models.ProgrammingGuide).facet(
            "programming_language"
        )

        # Not testing ordering here as two of the items have the same count, so the ordering is undefined.
        # See test_facet_tags for a test of the ordering
        self.assertDictEqual(dict(results), {"js": 2, "py": 2, "rs": 1})

    def test_facet_tags(self):
        # The test data doesn't contain any tags, add some
        FANTASY_BOOKS = [1, 2, 3, 4, 5, 6, 7]
        SCIFI_BOOKS = [10]
        for book in models.Book.objects.filter(id__in=FANTASY_BOOKS + SCIFI_BOOKS):
            book = book.get_indexed_instance()

            if book.id in FANTASY_BOOKS:
                book.tags.add("Fantasy")
            if book.id in SCIFI_BOOKS:
                book.tags.add("Science Fiction")

            # adding a related object doesn't trigger the post_save signal to reindex the book,
            # so we need to manually add it to the index
            self.backend.add(book)

        index = self.backend.get_index_for_model(models.Book)
        index.refresh()

        fantasy_tag = Tag.objects.get(name="Fantasy")
        scifi_tag = Tag.objects.get(name="Science Fiction")

        results = self.backend.search(MATCH_ALL, models.Book).facet("tags")

        self.assertEqual(
            results,
            OrderedDict(
                [
                    (fantasy_tag.id, 7),
                    (None, 6),
                    (scifi_tag.id, 1),
                ]
            ),
        )

    def test_facet_with_nonexistent_field(self):
        with self.assertRaises(FilterFieldError):
            self.backend.search(MATCH_ALL, models.ProgrammingGuide).facet("foo")

    # MISC TESTS

    def test_same_rank_pages(self):
        # Checks that results with a same ranking cannot be found multiple times
        # across pages (see https://github.com/wagtail/wagtail/issues/3729).
        same_rank_objects = set()

        for i in range(10):
            obj = models.Book.objects.create(
                title=f"Rank {i}",
                publication_date=date(2017, 10, 18),
                number_of_pages=100,
            )
            same_rank_objects.add(obj)

        index = self.backend.get_index_for_model(models.Book)
        index.refresh()

        results = self.backend.search("Rank", models.Book)
        results_across_pages = set()
        for i, _obj in enumerate(same_rank_objects):
            results_across_pages.add(results[i : i + 1][0])
        self.assertSetEqual(results_across_pages, same_rank_objects)

    def test_delete(self):
        foundation = models.Novel.objects.filter(title="Foundation").first()

        # Delete from the database
        foundation.delete()

        # Refresh the search index
        index = self.backend.get_index_for_model(models.Novel)
        index.refresh()

        # To test that the book was deleted from the index as well, we will perform the slicing check from an earlier
        # test where "Foundation" was the first result. We need to test it this way so we can pick up the case where
        # the object still exists in the index but not in the database (in that case, just two objects would be returned
        # instead of three).

        # Note: we need consistent ordering for this test
        results = self.backend.search(
            MATCH_ALL,
            models.Novel.objects.order_by("number_of_pages"),
            order_by_relevance=False,
        )

        # Limit the results
        results = results[:3]

        self.assertEqual(
            [r.title for r in results],
            [
                # "Foundation"
                "The Hobbit",
                "The Two Towers",
                "The Fellowship of the Ring",  # If this item doesn't appear, "Foundation" is still in the index
            ],
        )

    def test_plain_text_single_word(self):
        results = self.backend.search(
            PlainText("JavaScript"), models.Book.objects.all()
        )
        self.assertSetEqual(
            {r.title for r in results},
            {"JavaScript: The Definitive Guide", "JavaScript: The good parts"},
        )

    def test_incomplete_plain_text(self):
        results = self.backend.search(PlainText("pro"), models.Book.objects.all())

        self.assertSetEqual({r.title for r in results}, set())

    def test_plain_text_multiple_words_or(self):
        results = self.backend.search(
            PlainText("JavaScript Definitive", operator="or"), models.Book.objects.all()
        )
        self.assertSetEqual(
            {r.title for r in results},
            {"JavaScript: The Definitive Guide", "JavaScript: The good parts"},
        )

    def test_plain_text_multiple_words_and(self):
        results = self.backend.search(
            PlainText("JavaScript Definitive Guide", operator="and"),
            models.Book.objects.all(),
        )
        self.assertSetEqual(
            {r.title for r in results}, {"JavaScript: The Definitive Guide"}
        )

    def test_plain_text_operator_case(self):
        results = self.backend.search(
            PlainText("Guide", operator="AND"), models.Book.objects.all()
        )
        self.assertSetEqual(
            {r.title for r in results}, {"JavaScript: The Definitive Guide"}
        )

        results = self.backend.search(
            PlainText("Guide", operator="aNd"), models.Book.objects.all()
        )
        self.assertSetEqual(
            {r.title for r in results}, {"JavaScript: The Definitive Guide"}
        )

        results = self.backend.search(
            "Guide", models.Book.objects.all(), operator="AND"
        )
        self.assertSetEqual(
            {r.title for r in results}, {"JavaScript: The Definitive Guide"}
        )

        results = self.backend.search(
            "Guide", models.Book.objects.all(), operator="aNd"
        )
        self.assertSetEqual(
            {r.title for r in results}, {"JavaScript: The Definitive Guide"}
        )

    def test_plain_text_invalid_operator(self):
        with self.assertRaises(ValueError):
            self.backend.search(
                PlainText("Guide", operator="xor"), models.Book.objects.all()
            )

        with self.assertRaises(ValueError):
            self.backend.search("Guide", models.Book.objects.all(), operator="xor")

    def test_boost(self):
        results = self.backend.search(
            PlainText("JavaScript Definitive")
            | Boost(PlainText("Learning Python"), 2.0),
            models.Book.objects.all(),
        )

        # Both python and JavaScript should be returned with Python at the top
        self.assertEqual(
            [r.title for r in results],
            [
                "Learning Python",
                "JavaScript: The Definitive Guide",
            ],
        )

        results = self.backend.search(
            PlainText("JavaScript Definitive")
            | Boost(PlainText("Learning Python"), 0.5),
            models.Book.objects.all(),
        )

        # Now they should be swapped
        self.assertEqual(
            [r.title for r in results],
            [
                "JavaScript: The Definitive Guide",
                "Learning Python",
            ],
        )

    def test_match_all(self):
        results = self.backend.search(MATCH_ALL, models.Book.objects.all())
        self.assertEqual(len(results), 14)

    def test_search_none(self):
        """Passing None as a search term should be treated as MATCH_ALL, but with a deprecation warning."""
        with self.assertWarnsMessage(
            Warning,
            "Querying `None` is deprecated, use `MATCH_ALL` instead.",
        ):
            results = self.backend.search(None, models.Book.objects.all())
        self.assertEqual(len(results), 14)

    def test_and(self):
        results = self.backend.search(
            And([PlainText("javascript"), PlainText("definitive")]),
            models.Book.objects.all(),
        )
        self.assertSetEqual(
            {r.title for r in results}, {"JavaScript: The Definitive Guide"}
        )

        results = self.backend.search(
            PlainText("javascript") & PlainText("definitive"), models.Book.objects.all()
        )
        self.assertSetEqual(
            {r.title for r in results}, {"JavaScript: The Definitive Guide"}
        )

    def test_or(self):
        results = self.backend.search(
            Or([PlainText("hobbit"), PlainText("towers")]), models.Book.objects.all()
        )
        self.assertSetEqual(
            {r.title for r in results}, {"The Hobbit", "The Two Towers"}
        )

        results = self.backend.search(
            PlainText("hobbit") | PlainText("towers"), models.Book.objects.all()
        )
        self.assertSetEqual(
            {r.title for r in results}, {"The Hobbit", "The Two Towers"}
        )

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
        self.assertSetEqual({r.title for r in results}, all_other_titles)

    def test_operators_combination(self):
        results = self.backend.search(
            (
                (PlainText("javascript") & ~PlainText("definitive"))
                | PlainText("python")
                | PlainText("rust")
            )
            | PlainText("two"),
            models.Book.objects.all(),
        )
        self.assertSetEqual(
            {r.title for r in results},
            {
                "JavaScript: The good parts",
                "Learning Python",
                "The Two Towers",
                "The Rust Programming Language",
                "Two Scoops of Django 1.11",
                "Programming Rust",
            },
        )

    def test_negated_and(self):
        results = self.backend.search(
            (PlainText("rust") & ~(PlainText("programming") & PlainText("language"))),
            models.Book.objects.all(),
        )
        self.assertSetEqual(
            {r.title for r in results},
            {
                "Programming Rust",
            },
        )

    def test_negated_or(self):
        results = self.backend.search(
            (PlainText("rust") & ~(PlainText("language") | PlainText("crabs"))),
            models.Book.objects.all(),
        )
        self.assertSetEqual(
            {r.title for r in results},
            {
                "Programming Rust",
            },
        )

    def test_phrase(self):
        results = self.backend.search(
            Phrase("rust programming"), models.Book.objects.all()
        )
        self.assertSetEqual(
            {r.title for r in results}, {"The Rust Programming Language"}
        )

        results = self.backend.search(
            Phrase("programming rust"), models.Book.objects.all()
        )
        self.assertSetEqual({r.title for r in results}, {"Programming Rust"})

    def test_rebuild_modelsearch_index_no_verbosity(self):
        stdout = StringIO()
        management.call_command(
            "rebuild_modelsearch_index",
            verbosity=0,
            backend_name=self.backend_name,
            stdout=stdout,
        )
        self.assertFalse(stdout.getvalue())

    def test_refresh_all_indexes(self):
        """
        Backends should provide a refresh_indexes method that refreshes all indexes. We don't care
        what this does (and it will often be a no-op), beyond ensuring that searches return
        recently-updated data afterwards.
        """
        book = models.Book.objects.create(
            title="To Kill A Mockingbird",
            publication_date=date(2017, 10, 18),
            number_of_pages=100,
        )
        self.backend.add(book)
        author = models.Author.objects.create(name="Harper Lee")
        self.backend.add(author)
        self.backend.refresh_indexes()

        results = self.backend.search("mockingbird", models.Book)
        self.assertEqual(results.count(), 1)
        results = self.backend.search("harper", models.Author)
        self.assertEqual(results.count(), 1)

    def test_add_bulk(self):
        # Create book records using bulk_create, so that we don't trigger the post_save signal
        # (which would index them immediately and negate the need to call add_bulk)
        books = [
            models.Book(
                title="Fifty Shades of Grey",
                publication_date=date(2020, 1, 1),
                number_of_pages=100,
            ),
            models.Book(
                title="Fifty Shades Darker",
                publication_date=date(2020, 2, 1),
                number_of_pages=200,
            ),
            models.Book(
                title="Fifty Shades Freed",
                publication_date=date(2020, 3, 1),
                number_of_pages=300,
            ),
        ]
        models.Book.objects.bulk_create(books)

        self.backend.add_bulk(
            models.Book, models.Book.objects.filter(title__startswith="Fifty Shades")
        )
        self.backend.get_index_for_model(models.Book).refresh()

        results = self.backend.search("Fifty Shades", models.Book)
        self.assertEqual(results.count(), 3)

    def test_add_bulk_empty_list(self):
        self.backend.add_bulk(models.Book, [])


@override_settings(
    MODELSEARCH_BACKENDS={
        "default": {"BACKEND": "modelsearch.backends.database"},
    }
)
class TestBackendLoader(TestCase):
    @mock.patch("modelsearch.backends.database.connection")
    def test_import_by_name_unknown_db_vendor(self, connection):
        connection.vendor = "unknown"
        db = get_search_backend(backend="default")
        self.assertIsInstance(db, DatabaseSearchBackend)

    @mock.patch("modelsearch.backends.database.connection")
    def test_import_by_path_unknown_db_vendor(self, connection):
        connection.vendor = "unknown"
        db = get_search_backend(backend="modelsearch.backends.database")
        self.assertIsInstance(db, DatabaseSearchBackend)

    @mock.patch("modelsearch.backends.database.connection")
    def test_import_by_full_path_unknown_db_vendor(self, connection):
        connection.vendor = "unknown"
        db = get_search_backend(backend="modelsearch.backends.database.SearchBackend")
        self.assertIsInstance(db, DatabaseSearchBackend)

    @unittest.skipIf(
        connection.vendor != "postgresql",
        "Only applicable to PostgreSQL database systems",
    )
    def test_import_by_name_postgres_db_vendor(self):
        from modelsearch.backends.database.postgres.postgres import (
            PostgresSearchBackend,
        )

        db = get_search_backend(backend="default")
        self.assertIsInstance(db, PostgresSearchBackend)

    @unittest.skipIf(
        connection.vendor != "postgresql",
        "Only applicable to PostgreSQL database systems",
    )
    def test_import_by_path_postgres_db_vendor(self):
        from modelsearch.backends.database.postgres.postgres import (
            PostgresSearchBackend,
        )

        db = get_search_backend(backend="modelsearch.backends.database")
        self.assertIsInstance(db, PostgresSearchBackend)

    @unittest.skipIf(
        connection.vendor != "postgresql",
        "Only applicable to PostgreSQL database systems",
    )
    def test_import_by_full_path_postgres_db_vendor(self):
        from modelsearch.backends.database.postgres.postgres import (
            PostgresSearchBackend,
        )

        db = get_search_backend(backend="modelsearch.backends.database.SearchBackend")
        self.assertIsInstance(db, PostgresSearchBackend)

    @unittest.skipIf(
        connection.vendor != "mysql", "Only applicable to MySQL database systems"
    )
    def test_import_by_name_mysql_db_vendor(self):
        from modelsearch.backends.database.mysql.mysql import MySQLSearchBackend

        db = get_search_backend(backend="default")
        self.assertIsInstance(db, MySQLSearchBackend)

    @unittest.skipIf(
        connection.vendor != "mysql", "Only applicable to MySQL database systems"
    )
    def test_import_by_path_mysql_db_vendor(self):
        from modelsearch.backends.database.mysql.mysql import MySQLSearchBackend

        db = get_search_backend(backend="modelsearch.backends.database")
        self.assertIsInstance(db, MySQLSearchBackend)

    @unittest.skipIf(
        connection.vendor != "mysql", "Only applicable to MySQL database systems"
    )
    def test_import_by_full_path_mysql_db_vendor(self):
        from modelsearch.backends.database.mysql.mysql import MySQLSearchBackend

        db = get_search_backend(backend="modelsearch.backends.database.SearchBackend")
        self.assertIsInstance(db, MySQLSearchBackend)

    @unittest.skipIf(
        connection.vendor != "sqlite", "Only applicable to SQLite database systems"
    )
    def test_import_by_name_sqlite_db_vendor(self):
        # This should return the fallback backend, because the SQLite backend doesn't support versions less than 3.19.0
        if not fts5_available():  # pragma: no cover
            from modelsearch.backends.database.fallback import DatabaseSearchBackend

            db = get_search_backend(backend="default")
            self.assertIsInstance(db, DatabaseSearchBackend)
        else:
            from modelsearch.backends.database.sqlite.sqlite import (
                SQLiteSearchBackend,
            )

            db = get_search_backend(backend="default")
            self.assertIsInstance(db, SQLiteSearchBackend)

    @unittest.skipIf(
        connection.vendor != "sqlite", "Only applicable to SQLite database systems"
    )
    def test_import_by_path_sqlite_db_vendor(self):
        # Same as above
        if not fts5_available():  # pragma: no cover
            from modelsearch.backends.database.fallback import DatabaseSearchBackend

            db = get_search_backend(backend="modelsearch.backends.database")
            self.assertIsInstance(db, DatabaseSearchBackend)
        else:
            from modelsearch.backends.database.sqlite.sqlite import (
                SQLiteSearchBackend,
            )

            db = get_search_backend(backend="modelsearch.backends.database")
            self.assertIsInstance(db, SQLiteSearchBackend)

    @unittest.skipIf(
        connection.vendor != "sqlite", "Only applicable to SQLite database systems"
    )
    def test_import_by_full_path_sqlite_db_vendor(self):
        # Same as above
        if not fts5_available():
            from modelsearch.backends.database.fallback import DatabaseSearchBackend

            db = get_search_backend(
                backend="modelsearch.backends.database.SearchBackend"
            )
            self.assertIsInstance(db, DatabaseSearchBackend)
        else:
            from modelsearch.backends.database.sqlite.sqlite import (
                SQLiteSearchBackend,
            )

            db = get_search_backend(
                backend="modelsearch.backends.database.SearchBackend"
            )
            self.assertIsInstance(db, SQLiteSearchBackend)

    def test_nonexistent_backend_import(self):
        self.assertRaises(
            InvalidSearchBackendError,
            get_search_backend,
            backend="modelsearch.backends.doesntexist",
        )

    @override_settings(
        MODELSEARCH_BACKENDS={
            "default": {"BACKEND": "modelsearch.backends.database"},
            "nonexistent": {"BACKEND": "modelsearch.backends.doesnotexist"},
        }
    )
    def test_nonexistent_backend_import_from_config(self):
        self.assertRaises(
            InvalidSearchBackendError,
            get_search_backend,
            backend="nonexistent",
        )

    def test_invalid_backend_import(self):
        self.assertRaises(
            InvalidSearchBackendError, get_search_backend, backend="I'm not a backend!"
        )

    def test_get_search_backends(self):
        backends = list(get_search_backends())

        self.assertEqual(len(backends), 1)
        self.assertTrue(issubclass(type(backends[0]), BaseSearchBackend))

    @override_settings(MODELSEARCH_BACKENDS={})
    def test_get_search_backends_with_no_default_defined(self):
        backends = list(get_search_backends())

        self.assertEqual(len(backends), 1)
        self.assertTrue(issubclass(type(backends[0]), BaseSearchBackend))

    @override_settings(
        MODELSEARCH_BACKENDS={
            "default": {"BACKEND": "modelsearch.backends.database"},
            "another-backend": {"BACKEND": "modelsearch.backends.database"},
        }
    )
    def test_get_search_backends_multiple(self):
        backends = list(get_search_backends())

        self.assertEqual(len(backends), 2)

    def test_get_search_backends_with_auto_update(self):
        backends = list(get_search_backends(with_auto_update=True))

        # Auto update is the default
        self.assertEqual(len(backends), 1)

    @override_settings(
        MODELSEARCH_BACKENDS={
            "default": {
                "BACKEND": "modelsearch.backends.database",
                "AUTO_UPDATE": False,
            },
        }
    )
    def test_get_search_backends_with_auto_update_disabled(self):
        backends = list(get_search_backends(with_auto_update=True))

        self.assertEqual(len(backends), 0)

    @override_settings(
        MODELSEARCH_BACKENDS={
            "default": {
                "BACKEND": "modelsearch.backends.database",
                "AUTO_UPDATE": False,
            },
        }
    )
    def test_get_search_backends_without_auto_update_disabled(self):
        backends = list(get_search_backends())

        self.assertEqual(len(backends), 1)
