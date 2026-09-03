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


def _matching_locksmith(email: str):
    from apps.locksmiths.models import Locksmith

    if not email:
        return None
    return Locksmith.objects.filter(email__iexact=email, active=True).first()


def _link_locksmith_to_user(user) -> None:
    # Only claims a Locksmith row that isn't already linked to someone
    # else, so a later email collision can't silently reassign it.
    from apps.locksmiths.models import Locksmith

    Locksmith.objects.filter(
        email__iexact=user.email, active=True, user__isnull=True
    ).update(user=user)


class WGTKSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Restricts sign-in to WGTK Microsoft 365 email addresses.

    Two tiers: office/admin staff (the original single-tier model — full
    admin access) and locksmiths (self-service portal only, see
    apps/accounts/middleware.py), distinguished by whether the signing-in
    email matches an existing Locksmith record.
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
        _link_locksmith_to_user(existing_user)

    def populate_user(self, request, sociallogin, data):
        # Anyone signing in with an allowed WGTK domain account is
        # trusted office/admin staff UNLESS their email matches a known
        # Locksmith — locksmiths get the self-service portal only, never
        # office/admin access, however they authenticate.
        user = super().populate_user(request, sociallogin, data)
        if not _matching_locksmith(user.email):
            user.is_staff = True
            user.is_superuser = True
        return user

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        _link_locksmith_to_user(user)
        return user


class WGTKAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # No self-serve signup form; accounts are only created via Microsoft SSO.
        return False

    def get_login_redirect_url(self, request):
        if hasattr(request.user, "locksmith_profile"):
            return "/locksmith/"
        return super().get_login_redirect_url(request)
