"""automation_saas Django settings.

Local dev:
- SQLite (db.sqlite3)

Staging/production (e.g., Render):
- Set DATABASE_URL to a managed Postgres instance
- Set SECRET_KEY
- Set DEBUG=0
- (Optional) Set ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS via env
"""

import os
from pathlib import Path

import dj_database_url


BASE_DIR = Path(__file__).resolve().parent.parent

NOTIFICATION_PROVIDER = os.getenv("NOTIFICATION_PROVIDER", "mock")

# --- Stage 6: Weekly reports + alerts ---
WEEKLY_REPORT_TEMPLATE_NAME = os.getenv("WEEKLY_REPORT_TEMPLATE_NAME", "").strip() or None
WEEKLY_REPORT_SEND_EMAIL = os.getenv("WEEKLY_REPORT_SEND_EMAIL", "0") == "1"

# Alert thresholds (percentages)
WEEKLY_REPORT_NO_SHOW_ALERT_PCT = float(os.getenv("WEEKLY_REPORT_NO_SHOW_ALERT_PCT", "20"))
WEEKLY_REPORT_WEEK_EMPTY_PCT = float(os.getenv("WEEKLY_REPORT_WEEK_EMPTY_PCT", "40"))
WEEKLY_REPORT_WEEK_FULL_PCT = float(os.getenv("WEEKLY_REPORT_WEEK_FULL_PCT", "90"))
WEEKLY_REPORT_MIN_CAPACITY_SLOTS = int(os.getenv("WEEKLY_REPORT_MIN_CAPACITY_SLOTS", "10"))

# Email (only used if WEEKLY_REPORT_SEND_EMAIL=1)
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "no-reply@example.com")

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-key-change-me")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.getenv("DEBUG", "1") == "1"


# Hosts
ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    ".onrender.com",
]
# Add custom hosts via env var (comma separated)
extra_hosts = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]
ALLOWED_HOSTS += extra_hosts


# CSRF (for HTTPS)
CSRF_TRUSTED_ORIGINS = [
    "https://*.onrender.com",
]
# Add trusted origins via env var (comma separated, include scheme)
extra_origins = [o.strip() for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
CSRF_TRUSTED_ORIGINS += extra_origins


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core.apps.CoreConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "automation_saas.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "automation_saas.wsgi.application"


# Database
# - Uses DATABASE_URL automatically when provided
# - Falls back to local SQLite
#
# Note: Render Postgres generally expects SSL. If you ever run into SSL errors,
# keep DB_SSL_REQUIRE=1 (default). For local dev you can set DB_SSL_REQUIRE=0.
DB_SSL_REQUIRE = os.getenv("DB_SSL_REQUIRE", "1") == "1"

# Avoid passing ssl_require for SQLite (it breaks local manage.py/migrations).
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.config(
            default=DATABASE_URL,
            conn_max_age=int(os.getenv("DB_CONN_MAX_AGE", "600")),
            ssl_require=DB_SSL_REQUIRE,
        )
    }
else:
    DATABASES = {
        "default": dj_database_url.config(
            default=f"sqlite:///{(BASE_DIR / 'db.sqlite3').as_posix()}",
            conn_max_age=int(os.getenv("DB_CONN_MAX_AGE", "600")),
            ssl_require=False,
        )
    }


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


LANGUAGE_CODE = "he"
TIME_ZONE = os.getenv("TIME_ZONE", "Asia/Jerusalem")
USE_I18N = True
USE_TZ = True


LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"


STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# WhiteNoise: cache-busted static files
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Public base URL used in reminder links (set this in Render env vars)
# Example: https://automation-saas-1.onrender.com
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "http://127.0.0.1:8000")

# How long public confirm/cancel links remain valid (seconds)
APPOINTMENT_ACTION_LINK_MAX_AGE_SECONDS = int(os.getenv("APPOINTMENT_ACTION_LINK_MAX_AGE_SECONDS", str(14 * 24 * 60 * 60)))



# When running behind a proxy (Render), this helps Django detect HTTPS correctly
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Sensible production defaults when DEBUG=0
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Leave this False if you ever need HTTP for a local tunnel.
    SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "1") == "1"
