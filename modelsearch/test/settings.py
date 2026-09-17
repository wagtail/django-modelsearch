import os

from pathlib import Path

import dj_database_url


# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/1.10/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = "c6u0-9c!7nilj_ysatsda0(f@e_2mws2f!6m0n^o*4#*q#kzp)"  # NOQA: S105

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ["localhost", "testserver"]


# Application definition

INSTALLED_APPS = [
    "modelsearch.test.testapp",
    "modelsearch",
    "taggit",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sitemaps",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "modelsearch.test.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]


# Using DatabaseCache to make sure that the cache is cleared between tests.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "cache",
    }
}


# don't use the intentionally slow default password hasher
PASSWORD_HASHERS = ("django.contrib.auth.hashers.MD5PasswordHasher",)


# Database
# https://docs.djangoproject.com/en/1.10/ref/settings/#databases

DATABASES = {
    "default": dj_database_url.config(default="sqlite:///test_modelsearch.sqlite")
}

if DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
    INSTALLED_APPS.append("django.contrib.postgres")


# Search backend

SEARCH_BACKEND = os.getenv("SEARCH_BACKEND", "db")

MODELSEARCH_BACKENDS = {
    "default": {
        "BACKEND": {
            "db": "modelsearch.backends.database",
            "elasticsearch7": "modelsearch.backends.elasticsearch7",
            "elasticsearch8": "modelsearch.backends.elasticsearch8",
            "elasticsearch9": "modelsearch.backends.elasticsearch9",
            "opensearch2": "modelsearch.backends.opensearch2",
            "opensearch3": "modelsearch.backends.opensearch3",
            "whoosh": "modelsearch.backends.whoosh",
        }[SEARCH_BACKEND]
    }
}

if SEARCH_BACKEND in ["elasticsearch7", "elasticsearch8", "elasticsearch9"]:
    ELASTICSEARCH_URL = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
    MODELSEARCH_BACKENDS["default"]["URLS"] = [ELASTICSEARCH_URL]
    MODELSEARCH_BACKENDS["default"]["INDEX_PREFIX"] = "modelsearchtest_"
    MODELSEARCH_BACKENDS["default"]["OPTIONS"] = {
        "ca_certs": os.environ.get("ELASTICSEARCH_CA_CERTS"),
    }

if SEARCH_BACKEND in ["opensearch2", "opensearch3"]:
    OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
    MODELSEARCH_BACKENDS["default"]["URLS"] = [OPENSEARCH_URL]
    MODELSEARCH_BACKENDS["default"]["INDEX_PREFIX"] = "modelsearchtest_"
    MODELSEARCH_BACKENDS["default"]["OPTIONS"] = {
        "ca_certs": os.environ.get("OPENSEARCH_CA_CERTS"),
    }


# Password validation
# https://docs.djangoproject.com/en/1.10/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization
# https://docs.djangoproject.com/en/1.10/topics/i18n/

LANGUAGE_CODE = "en-gb"

TIME_ZONE = "UTC"

USE_I18N = True

USE_L10N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/1.8/howto/static-files/

STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

STATIC_ROOT = BASE_DIR / "test-static"
STATIC_URL = "/static/"

MEDIA_ROOT = BASE_DIR / "test-media"
