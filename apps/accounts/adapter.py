from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.contrib import messages
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

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        user.is_staff = True
        return user


class WGTKAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # No self-serve signup form; accounts are only created via Microsoft SSO.
        return False
