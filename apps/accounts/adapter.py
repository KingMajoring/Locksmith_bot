from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import redirect


def _email_domain_allowed(email: str) -> bool:
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    return domain in {d.lower() for d in settings.ALLOWED_EMAIL_DOMAINS}


class WGTKSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Restricts sign-in to WGTK Microsoft 365 email addresses.

    Only office/admin staff use this tool (locksmiths only interact via
    the emailed stock-check sheet), so login is a simple allow-by-domain
    check rather than a full role system.
    """

    def is_open_for_signup(self, request, sociallogin):
        # Unlike the account adapter below (which closes the local
        # password-based signup form), first-time Microsoft SSO sign-ins
        # from an allowed WGTK domain should be auto-provisioned.
        return True

    def pre_social_login(self, request, sociallogin):
        email = sociallogin.account.extra_data.get("mail") or sociallogin.account.extra_data.get(
            "userPrincipalName", ""
        )
        if not _email_domain_allowed(email):
            messages.error(
                request,
                "That Microsoft account isn't a recognised WGTK address. "
                "Sign in with your WGTK email.",
            )
            raise ImmediateHttpResponse(redirect("account_login"))

        # If this exact Microsoft account has never signed in before but a
        # local account already exists with the same email (e.g. because
        # the Azure AD app registration used for login was swapped, giving
        # a new provider account id), connect this sign-in to that
        # existing account instead of hitting the "confirm signup" form
        # and colliding on the unique email constraint.
        if sociallogin.is_existing:
            return
        try:
            existing_user = get_user_model().objects.get(email__iexact=email)
        except get_user_model().DoesNotExist:
            return
        sociallogin.connect(request, existing_user)

    def populate_user(self, request, sociallogin, data):
        # Single-tier access model (see README): anyone signing in with an
        # allowed WGTK domain account is trusted office/admin staff, so
        # they get full admin access rather than a permission-less
        # is_staff account that can log into /admin/ but do nothing there.
        user = super().populate_user(request, sociallogin, data)
        user.is_staff = True
        user.is_superuser = True
        return user


class WGTKAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # No self-serve signup form; accounts are only created via Microsoft SSO.
        return False
