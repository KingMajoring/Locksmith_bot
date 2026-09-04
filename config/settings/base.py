"""
Base settings shared by all environments.

WGTK Ops Tool covers four areas: Stock Accuracy, Job Completion, Job Costing
and Panelled Jobs. Build starts with Stock Accuracy (see apps/stock_accuracy).
"""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-secret-key-change-me")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.microsoft",
    "apps.accounts",
    "apps.locksmiths",
    "apps.integrations",
    "apps.stock_accuracy",
    "apps.job_completion",
    "apps.locksmith_portal",
    "apps.panel",
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
    "allauth.account.middleware.AccountMiddleware",
    "apps.accounts.middleware.RestrictLocksmithsToPortalMiddleware",
]

ROOT_URLCONF = "config.urls"

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
                "apps.job_completion.context_processors.needs_categorization_count",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": env.db("DATABASE_URL", default="sqlite:///" + str(BASE_DIR / "db.sqlite3")),
}

# Handl/Soter is a separate, external Azure SQL database, read via
# pymssql (see apps/integrations/handl.py) rather than Django's ORM.
# Left unset by default so the app falls back to the mock Handl client.
# In production these are Key Vault references, not plain values.
HANDL_SQL_SERVER = env("HANDL_SQL_SERVER", default="")
HANDL_SQL_PORT = env.int("HANDL_SQL_PORT", default=1433)
HANDL_SQL_DATABASE = env("HANDL_SQL_DATABASE", default="")
HANDL_SQL_USER = env("HANDL_SQL_USER", default="")
HANDL_SQL_PASSWORD = env("HANDL_SQL_PASSWORD", default="")

# Separate write-capable credential (locksmith portal disposals only —
# apps/integrations/handl.py's record_disposal). The main HANDL_SQL_USER
# above is read-only by design; this is a second, write-capable account
# ("n8n"-style service user) on the same server/port/database.
HANDL_SQL_WRITE_USER = env("HANDL_SQL_WRITE_USER", default="")
HANDL_SQL_WRITE_PASSWORD = env("HANDL_SQL_WRITE_PASSWORD", default="")

# Fallback only: real portal disposals attribute to the specific
# locksmith's own Soter login id (Locksmith.soter_user_id, synced from
# wiki.LocksmithLogin — confirmed live that's how Soter already
# attributes real disposals, not a shared account). This is only used
# if a locksmith hasn't been through a Soter sync since soter_user_id
# was added, so their own id isn't known yet.
HANDL_PORTAL_CREATED_BY_USER_ID = env.int("HANDL_PORTAL_CREATED_BY_USER_ID", default=0)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SITE_ID = 1
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_ADAPTER = "apps.accounts.adapter.WGTKSocialAccountAdapter"
ACCOUNT_ADAPTER = "apps.accounts.adapter.WGTKAccountAdapter"

# Only these email domains may sign in. Configure via env; the deploy admin
# should set this to WGTK's real Microsoft 365 domain(s), comma separated.
ALLOWED_EMAIL_DOMAINS = env.list("ALLOWED_EMAIL_DOMAINS", default=["wgtk.co.uk"])

SOCIALACCOUNT_PROVIDERS = {
    "microsoft": {
        "APPS": [
            {
                "client_id": env("MICROSOFT_CLIENT_ID", default=""),
                "secret": env("MICROSOFT_CLIENT_SECRET", default=""),
                "settings": {
                    "tenant": env("MICROSOFT_TENANT_ID", default="common"),
                },
            }
        ],
    }
}

# --- Email (weekly stock check to locksmiths, Area 1) ----------------------
# Default: console backend (prints instead of sending). Two real options:
# - SMTP: EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend +
#   EMAIL_HOST_USER/PASSWORD (needs SMTP AUTH enabled on the mailbox).
# - Microsoft Graph (preferred — avoids SMTP AUTH entirely):
#   EMAIL_BACKEND=apps.integrations.graph_email_backend.MicrosoftGraphEmailBackend
#   + the MS_GRAPH_MAIL_* settings below.
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="smtp.office365.com")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="stock-checks@wgtk.co.uk")

# Microsoft Graph sendMail (see apps/integrations/graph_email_backend.py).
# MS_GRAPH_MAIL_SENDER is whose mailbox the app-only Graph call acts as
# (e.g. admin@wgtk.co.uk); MS_GRAPH_MAIL_FROM is the message's From
# address (e.g. parts@wgtk.co.uk, a shared mailbox the sender has Send
# As rights on) — leave MS_GRAPH_MAIL_FROM blank to just send as the
# sender mailbox directly.
MS_GRAPH_MAIL_CLIENT_ID = env("MS_GRAPH_MAIL_CLIENT_ID", default="")
MS_GRAPH_MAIL_CLIENT_SECRET = env("MS_GRAPH_MAIL_CLIENT_SECRET", default="")
MS_GRAPH_MAIL_TENANT_ID = env("MS_GRAPH_MAIL_TENANT_ID", default="")
MS_GRAPH_MAIL_SENDER = env("MS_GRAPH_MAIL_SENDER", default="")
MS_GRAPH_MAIL_FROM = env("MS_GRAPH_MAIL_FROM", default="")

# --- Stock Accuracy (Area 1) config defaults --------------------------------
STOCK_CHECK_LINES_PER_WEEK = env.int("STOCK_CHECK_LINES_PER_WEEK", default=10)
STOCK_CHECK_POOL_SIZE = env.int("STOCK_CHECK_POOL_SIZE", default=30)
STOCK_CHECK_USAGE_WINDOW_DAYS = env.int("STOCK_CHECK_USAGE_WINDOW_DAYS", default=90)
STOCK_CHECK_NO_REPEAT_WEEKS = env.int("STOCK_CHECK_NO_REPEAT_WEEKS", default=4)

# Pre-go-live safety net: while set, every stock-check email is
# redirected here instead of the real locksmith (subject line still
# says who it would really have gone to). Leave unset once confident
# in real SMTP delivery and ready for locksmiths to receive them.
STOCK_CHECK_TEST_REDIRECT_EMAIL = env("STOCK_CHECK_TEST_REDIRECT_EMAIL", default="")

# --- Optimo API (Area 2+, wired up in a later phase) ------------------------
OPTIMO_API_BASE_URL = env("OPTIMO_API_BASE_URL", default="")
OPTIMO_API_KEY = env("OPTIMO_API_KEY", default="")

# --- Scheduled jobs over HTTP (replaces Azure WebJobs) -----------------------
# Confirmed live: Azure's WebJobs feature (App_Data/jobs/triggered/...)
# never actually runs here — Kudu's WebJobs discovery scans the
# persistent /home/site/wwwroot, but this app's real code (App_Data
# included) only ever exists in a per-instance temp extraction of the
# Oryx build artifact, so a WebJob placed there is never picked up (no
# cron, no webjob process, and Kudu itself reports zero registered
# jobs). See job_completion.views.run_scheduled_job — a GitHub Actions
# scheduled workflow calls it instead, authenticated by this shared
# secret rather than Django login (GitHub Actions can't go through
# SSO). Left unset by default so the endpoint refuses everything until
# deliberately configured.
SCHEDULED_JOB_TOKEN = env("SCHEDULED_JOB_TOKEN", default="")
