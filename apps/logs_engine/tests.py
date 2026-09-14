from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.integrations.handl import JobDetails


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
