from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from apps.locksmiths.models import Locksmith

from .adapter import WGTKAccountAdapter, WGTKSocialAccountAdapter, _link_locksmith_to_user
from .middleware import RestrictLocksmithsToPortalMiddleware

User = get_user_model()


def _sociallogin_for(email):
    # DefaultSocialAccountAdapter.populate_user only ever touches
    # sociallogin.user, so a bare namespace is enough — no need to build
    # a real allauth SocialLogin.
    return SimpleNamespace(user=User(email=email))


class PopulateUserTests(TestCase):
    def test_office_email_gets_staff_and_superuser(self):
        adapter = WGTKSocialAccountAdapter()
        user = adapter.populate_user(
            None, _sociallogin_for("office@wgtk.co.uk"), {"email": "office@wgtk.co.uk"}
        )
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)

    def test_locksmith_email_does_not_get_staff_or_superuser(self):
        Locksmith.objects.create(name="Dean S", email="dean@wgtk.co.uk", active=True)
        adapter = WGTKSocialAccountAdapter()
        user = adapter.populate_user(
            None, _sociallogin_for("dean@wgtk.co.uk"), {"email": "dean@wgtk.co.uk"}
        )
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_locksmith_match_is_case_insensitive(self):
        Locksmith.objects.create(name="Dean S", email="Dean@WGTK.co.uk", active=True)
        adapter = WGTKSocialAccountAdapter()
        user = adapter.populate_user(
            None, _sociallogin_for("dean@wgtk.co.uk"), {"email": "dean@wgtk.co.uk"}
        )
        self.assertFalse(user.is_staff)

    def test_inactive_locksmith_email_still_gets_office_access(self):
        Locksmith.objects.create(name="Ex Staff", email="ex@wgtk.co.uk", active=False)
        adapter = WGTKSocialAccountAdapter()
        user = adapter.populate_user(
            None, _sociallogin_for("ex@wgtk.co.uk"), {"email": "ex@wgtk.co.uk"}
        )
        self.assertTrue(user.is_staff)


class LinkLocksmithToUserTests(TestCase):
    def test_links_matching_active_locksmith(self):
        locksmith = Locksmith.objects.create(name="Dean S", email="dean@wgtk.co.uk", active=True)
        user = User.objects.create(email="dean@wgtk.co.uk", username="dean@wgtk.co.uk")

        _link_locksmith_to_user(user)

        locksmith.refresh_from_db()
        self.assertEqual(locksmith.user_id, user.id)

    def test_does_not_steal_locksmith_already_linked_to_someone_else(self):
        other_user = User.objects.create(email="other@wgtk.co.uk", username="other")
        locksmith = Locksmith.objects.create(
            name="Dean S", email="dean@wgtk.co.uk", active=True, user=other_user
        )
        user = User.objects.create(email="dean@wgtk.co.uk", username="dean@wgtk.co.uk")

        _link_locksmith_to_user(user)

        locksmith.refresh_from_db()
        self.assertEqual(locksmith.user_id, other_user.id)


class LoginRedirectTests(TestCase):
    def test_locksmith_linked_user_redirects_to_portal(self):
        locksmith = Locksmith.objects.create(name="Dean S", email="dean@wgtk.co.uk", active=True)
        user = User.objects.create(email="dean@wgtk.co.uk", username="dean@wgtk.co.uk")
        locksmith.user = user
        locksmith.save(update_fields=["user"])

        request = RequestFactory().get("/")
        request.user = user
        self.assertEqual(WGTKAccountAdapter().get_login_redirect_url(request), "/locksmith/")

    def test_office_user_gets_default_redirect(self):
        user = User.objects.create(
            email="office@wgtk.co.uk", username="office@wgtk.co.uk", is_staff=True
        )
        request = RequestFactory().get("/")
        request.user = user
        self.assertEqual(WGTKAccountAdapter().get_login_redirect_url(request), "/")


class RestrictLocksmithsToPortalMiddlewareTests(TestCase):
    def _middleware(self):
        return RestrictLocksmithsToPortalMiddleware(lambda request: HttpResponse("ok"))

    def _locksmith_user(self):
        locksmith = Locksmith.objects.create(name="Dean S", email="dean@wgtk.co.uk", active=True)
        user = User.objects.create(email="dean@wgtk.co.uk", username="dean@wgtk.co.uk")
        locksmith.user = user
        locksmith.save(update_fields=["user"])
        return user

    def test_locksmith_user_redirected_away_from_office_pages(self):
        request = RequestFactory().get("/stock-accuracy/")
        request.user = self._locksmith_user()
        response = self._middleware()(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/locksmith/")

    def test_locksmith_user_allowed_on_portal_paths(self):
        request = RequestFactory().get("/locksmith/")
        request.user = self._locksmith_user()
        response = self._middleware()(request)
        self.assertEqual(response.status_code, 200)

    def test_office_user_not_redirected(self):
        user = User.objects.create(
            email="office@wgtk.co.uk", username="office@wgtk.co.uk", is_staff=True
        )
        request = RequestFactory().get("/stock-accuracy/")
        request.user = user
        response = self._middleware()(request)
        self.assertEqual(response.status_code, 200)

    def test_anonymous_user_not_redirected(self):
        request = RequestFactory().get("/stock-accuracy/")
        request.user = AnonymousUser()
        response = self._middleware()(request)
        self.assertEqual(response.status_code, 200)
