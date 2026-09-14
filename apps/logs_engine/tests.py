from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.integrations.google_maps import LocksmithDistance
from apps.integrations.handl import JobDetails
from apps.locksmiths.models import Locksmith


def _job_with_location(**overrides):
    fields = dict(
        report_id="501179", make="Ford", model="Focus", year="2020",
        reg="AB20 CDE", vin="VIN1", service_type="Car", loss_type="LOST",
        supplied_service="", net_cost=100.0,
        vehicle_latitude=52.6309, vehicle_longitude=1.2974,
    )
    fields.update(overrides)
    return JobDetails(**fields)


class LogsEngineLookupTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("logs_engine:lookup"))
        self.assertEqual(response.status_code, 302)

    def test_no_report_id_shows_blank_form_only(self):
        response = self.client.get(reverse("logs_engine:lookup"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["searched"])
        self.assertIsNone(response.context["job"])
        self.assertNotContains(response, "No job found")

    @patch("apps.logs_engine.views.get_handl_client")
    def test_found_report_id_shows_job_card(self, mock_get_handl):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(return_value={
            "501179": JobDetails(
                report_id="501179", make="Ford", model="Focus", year="2020",
                reg="AB20 CDE", vin="VIN1", service_type="Car", loss_type="LOST",
                supplied_service="Non-Destructive Entry", net_cost=100.0,
            ),
        }))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.status_code, 200)
        job = response.context["job"]
        self.assertEqual(job.reg, "AB20 CDE")
        self.assertEqual(job.make, "Ford")
        self.assertContains(response, "AB20 CDE")
        self.assertContains(response, "Ford Focus 2020")
        mock_get_handl.return_value.get_job_details.assert_called_once_with(["501179"])

    @patch("apps.logs_engine.views.get_handl_client")
    def test_unknown_report_id_shows_not_found(self, mock_get_handl):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(return_value={}))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "999999"})

        self.assertIsNone(response.context["job"])
        self.assertContains(response, "No job found for ReportID 999999")

    @patch("apps.logs_engine.views.get_handl_client")
    def test_handl_failure_shows_not_found_rather_than_erroring(self, mock_get_handl):
        mock_get_handl.return_value = MagicMock(
            get_job_details=MagicMock(side_effect=Exception("boom"))
        )

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["job"])
        self.assertContains(response, "No job found for ReportID 501179")

    @patch("apps.logs_engine.views.get_handl_client")
    def test_strips_whitespace_from_report_id(self, mock_get_handl):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(return_value={
            "501179": JobDetails(
                report_id="501179", make="Ford", model="Focus", year="2020",
                reg="AB20 CDE", vin="VIN1", service_type="Car", loss_type="LOST",
                supplied_service="", net_cost=100.0,
            ),
        }))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "  501179  "})

        mock_get_handl.return_value.get_job_details.assert_called_once_with(["501179"])
        self.assertIsNotNone(response.context["job"])


class LogsEngineNearestLocksmithsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_ranks_locksmiths_nearest_first(self, mock_get_handl, mock_get_maps):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(
            return_value={"501179": _job_with_location()}
        ))
        far = Locksmith.objects.create(name="WGTK - Far Away", home_postcode="IP1 2AB")
        near = Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="IP1 2AB", distance_metres=40000.0, duration_seconds=3000, status="OK"),
            LocksmithDistance(origin="NR14 8PL", distance_metres=8369.0, duration_seconds=720, status="OK"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        nearest = response.context["nearest_locksmiths"]
        self.assertEqual([locksmith.pk for locksmith, _ in nearest], [near.pk, far.pk])
        self.assertContains(response, "WGTK - Nearby")
        self.assertContains(response, "WGTK - Far Away")
        # order in the passed-in list matters — this is how results get
        # zipped back onto the right locksmith
        call_origins = mock_get_maps.return_value.get_distances.call_args[0][0]
        self.assertEqual(call_origins, ["IP1 2AB", "NR14 8PL"])  # ordered by name

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_excludes_locksmiths_without_a_postcode_and_counts_them(self, mock_get_handl, mock_get_maps):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(
            return_value={"501179": _job_with_location()}
        ))
        Locksmith.objects.create(name="WGTK - No Postcode", home_postcode="")
        Locksmith.objects.create(name="WGTK - Inactive", home_postcode="NR14 8PL", active=False)
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.context["nearest_locksmiths"], [])
        self.assertEqual(response.context["locksmiths_missing_postcode"], 1)
        self.assertContains(response, "1 active locksmith has no home postcode set")
        mock_get_maps.return_value.get_distances.assert_not_called()

    @patch("apps.logs_engine.views.get_handl_client")
    def test_no_vehicle_location_skips_distance_lookup(self, mock_get_handl):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(
            return_value={"501179": _job_with_location(vehicle_latitude=None, vehicle_longitude=None)}
        ))
        Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")

        with patch("apps.logs_engine.views.get_google_maps_client") as mock_get_maps:
            response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})
            mock_get_maps.assert_not_called()

        self.assertEqual(response.context["nearest_locksmiths"], [])
        self.assertContains(response, "no vehicle location recorded")

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_google_failure_shows_no_suggestions_rather_than_erroring(self, mock_get_handl, mock_get_maps):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(
            return_value={"501179": _job_with_location()}
        ))
        Locksmith.objects.create(name="WGTK - Nearby", home_postcode="NR14 8PL")
        mock_get_maps.return_value = MagicMock(
            get_distances=MagicMock(side_effect=Exception("boom"))
        )

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["nearest_locksmiths"], [])

    @patch("apps.logs_engine.views.get_google_maps_client")
    @patch("apps.logs_engine.views.get_handl_client")
    def test_unresolved_origin_excluded_from_ranked_list(self, mock_get_handl, mock_get_maps):
        mock_get_handl.return_value = MagicMock(get_job_details=MagicMock(
            return_value={"501179": _job_with_location()}
        ))
        Locksmith.objects.create(name="WGTK - Bad Postcode", home_postcode="NOTREAL")
        mock_get_maps.return_value = MagicMock(get_distances=MagicMock(return_value=[
            LocksmithDistance(origin="NOTREAL", distance_metres=None, duration_seconds=None, status="NOT_FOUND"),
        ]))

        response = self.client.get(reverse("logs_engine:lookup"), {"report_id": "501179"})

        self.assertEqual(response.context["nearest_locksmiths"], [])
