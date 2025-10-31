from django.test import TestCase

from modelsearch import format_version


class TestVersion(TestCase):
    def test_format_version(self):
        self.assertEqual(format_version((1, 2, 3, "final", 0)), "1.2.3")
        self.assertEqual(format_version((1, 2, 0, "final", 0)), "1.2")
        self.assertEqual(format_version((1, 0, 0, "alpha", 2)), "1.0a2")
