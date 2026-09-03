from django.shortcuts import redirect

_ALLOWED_PREFIXES = ("/locksmith/", "/accounts/", "/static/")


class RestrictLocksmithsToPortalMiddleware:
    """Locksmith-linked users (see apps/accounts/adapter.py) only ever get
    is_staff=False, so they can't reach Django admin — but every other
    view in this project only checks @login_required, not staff status,
    which would otherwise let a locksmith account view office/admin
    pages (margins, other locksmiths' performance, etc.) just by typing
    the URL. Enforced here at the request level rather than annotating
    every view.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and hasattr(user, "locksmith_profile")
            and not request.path.startswith(_ALLOWED_PREFIXES)
        ):
            return redirect("/locksmith/")
        return self.get_response(request)
