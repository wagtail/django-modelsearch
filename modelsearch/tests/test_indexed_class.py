from contextlib import contextmanager

from django.core import checks
from django.test import TestCase

from modelsearch import index
from modelsearch.test.testapp import models


@contextmanager
def patch_search_fields(model, new_search_fields):
    """
    A context manager to allow testing of different search_fields configurations
    without permanently changing the models' search_fields.
    """
    old_search_fields = model.search_fields
    model.search_fields = new_search_fields
    yield
    model.search_fields = old_search_fields


class TestContentTypeNames(TestCase):
    def test_base_content_type_name(self):
        name = models.Novel.indexed_get_toplevel_content_type()
        self.assertEqual(name, "searchtests_book")

    def test_qualified_content_type_name(self):
        name = models.Novel.indexed_get_content_type()
        self.assertEqual(name, "searchtests_book_searchtests_novel")


class TestSearchFields(TestCase):
    def make_dummy_type(self, search_fields):
        return type("DummyType", (index.Indexed,), {"search_fields": search_fields})

    def get_checks_result(warning_id=None):
        """Run Django checks on any with the 'search' tag used when registering the check"""
        checks_result = checks.run_checks()
        if warning_id:
            return [warning for warning in checks_result if warning.id == warning_id]
        return checks_result

    def test_basic(self):
        cls = self.make_dummy_type(
            [
                index.SearchField("test", boost=100),
                index.FilterField("filter_test"),
            ]
        )

        self.assertEqual(len(cls.get_search_fields()), 2)
        self.assertEqual(len(cls.get_searchable_search_fields()), 1)
        self.assertEqual(len(cls.get_filterable_search_fields()), 1)

    def test_overriding(self):
        # If there are two fields with the same type and name
        # the last one should override all the previous ones. This ensures that the
        # standard convention of:
        #
        #     class SpecificPageType(Page):
        #         search_fields = Page.search_fields + [some_other_definitions]
        #
        # ...causes the definitions in some_other_definitions to override Page.search_fields
        # as intended.
        cls = self.make_dummy_type(
            [
                index.SearchField("test", boost=100),
                index.SearchField("test"),
            ]
        )

        self.assertEqual(len(cls.get_search_fields()), 1)
        self.assertEqual(len(cls.get_searchable_search_fields()), 1)
        self.assertEqual(len(cls.get_filterable_search_fields()), 0)

        field = cls.get_search_fields()[0]
        self.assertIsInstance(field, index.SearchField)

        # Boost should be reset to the default if it's not specified by the override
        self.assertIsNone(field.boost)

    def test_different_field_types_dont_override(self):
        # A search and filter field with the same name should be able to coexist
        cls = self.make_dummy_type(
            [
                index.SearchField("test", boost=100),
                index.FilterField("test"),
            ]
        )

        self.assertEqual(len(cls.get_search_fields()), 2)
        self.assertEqual(len(cls.get_searchable_search_fields()), 1)
        self.assertEqual(len(cls.get_filterable_search_fields()), 1)

    def test_checking_search_fields(self):
        with patch_search_fields(
            models.Book, models.Book.search_fields + [index.SearchField("foo")]
        ):
            expected_errors = [
                checks.Warning(
                    "Book.search_fields contains non-existent field 'foo'",
                    obj=models.Book,
                    id="modelsearch.W004",
                )
            ]
            errors = models.Book.check()
            self.assertEqual(errors, expected_errors)

    def test_checking_missing_title_search_field(self):
        with patch_search_fields(models.Book, [index.SearchField("summary")]):
            errors = models.Book.check()
            w005_errors = [e for e in errors if e.id == "modelsearch.W005"]
            self.assertEqual(len(w005_errors), 1)
            self.assertIn("title", w005_errors[0].msg)

    def test_no_warning_when_title_search_field_present(self):
        with patch_search_fields(
            models.Book,
            [index.SearchField("title"), index.SearchField("summary")],
        ):
            errors = models.Book.check()
            w005_errors = [e for e in errors if e.id == "modelsearch.W005"]
            self.assertEqual(w005_errors, [])
