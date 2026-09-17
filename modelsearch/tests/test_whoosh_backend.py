import tempfile

from django.test import TestCase
from django.test.utils import override_settings

from .test_backends import BackendTests


@override_settings(
    MODELSEARCH_BACKENDS={
        "default": {
            "BACKEND": "modelsearch.backends.whoosh",
            "PATH": tempfile.mkdtemp(prefix="modelsearch_whoosh_test_"),
        }
    }
)
class TestWhooshBackend(BackendTests, TestCase):
    backend_path = "modelsearch.backends.whoosh"
