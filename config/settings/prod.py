import os

from .base import *  # noqa: F401,F403

DEBUG = False

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# Azure App Service terminates TLS at the load balancer and forwards over
# HTTP, setting this header so Django knows the original request was HTTPS.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# App Service exposes its own hostname via WEBSITE_HOSTNAME; trust it
# automatically so DJANGO_ALLOWED_HOSTS doesn't need updating on redeploy.
_website_hostname = os.environ.get("WEBSITE_HOSTNAME")
if _website_hostname and _website_hostname not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(_website_hostname)
    CSRF_TRUSTED_ORIGINS = [f"https://{_website_hostname}"]
