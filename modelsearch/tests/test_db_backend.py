import unittest

from unittest.mock import patch

from django.test import TestCase
from django.test.utils import override_settings

from modelsearch import index
from modelsearch.test.testapp import models

from .test_backends import BackendTests


@override_settings(
    MODELSEARCH_BACKENDS={
        "default": {
            "BACKEND": "modelsearch.backends.database.fallback",
        }
    }
)
class TestDBBackend(BackendTests, TestCase):
    backend_path = "modelsearch.backends.database.fallback"

    # Doesn't support ranking
    @unittest.expectedFailure
    def test_ranking(self):
        super().test_ranking()

    # Doesn't support ranking
    @unittest.expectedFailure
    def test_annotate_score(self):
        super().test_annotate_score()

    # Doesn't support ranking
    @unittest.expectedFailure
    def test_annotate_score_with_slice(self):
        super().test_annotate_score_with_slice()

    # Doesn't support ranking
    @unittest.expectedFailure
    def test_search_boosting_on_related_fields(self):
        super().test_search_boosting_on_related_fields()

    # Doesn't support searching specific fields
    @unittest.expectedFailure
    def test_search_child_class_field_from_parent(self):
        super().test_search_child_class_field_from_parent()

    # Doesn't support searching related fields
    @unittest.expectedFailure
    def test_search_on_related_fields(self):
        super().test_search_on_related_fields()

    # Doesn't support searching related fields
    @unittest.expectedFailure
    def test_search_on_related_fields_reverse_one_to_one(self):
        super().test_search_on_related_fields_reverse_one_to_one()

    # Doesn't support searching nested related fields
    @unittest.expectedFailure
    def test_search_on_nested_related_fields(self):
        super().test_search_on_nested_related_fields()

    # Doesn't support searching callable fields
    @unittest.expectedFailure
    def test_search_callable_field(self):
        super().test_search_callable_field()

    # Database backend always uses `icontains`, so always autocomplete
    @unittest.expectedFailure
    def test_incomplete_plain_text(self):
        super().test_incomplete_plain_text()

    # Database backend doesn't support Boost() query class
    @unittest.expectedFailure
    def test_boost(self):
        super().test_boost()

    def test_reset_indexes(self):
        """
        After running backend.reset_indexes(), search should still return results (because there's
        no actual index to reset)
        """
        self.backend.reset_indexes()
        results = self.backend.search("JavaScript", models.Book)
        self.assertEqual(results.count(), 2)

    def test_filter_on_many_to_many_through_attname(self):
        tolkien = models.Author.objects.get(name="J. R. R. Tolkien")
        search_fields = [
            index.SearchField("title"),
            index.FilterField("author_id"),
        ]
        with patch.object(models.Book, "search_fields", search_fields):
            results = list(
                self.backend.search(
                    "hobbit",
                    models.Book.objects.filter(authors=tolkien),
                )
            )
            self.assertCountEqual(
                [r.title for r in results],
                ["The Hobbit"],
            )
