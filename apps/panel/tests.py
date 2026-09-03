from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.integrations.handl import PanelJobSpend

from .services.spend import mtd_spend_by_locksmith


class FakePanelHandlClient:
    def __init__(self, jobs):
        self._jobs = jobs

    def get_panel_jobs(self, start_date, end_date):
        return [j for j in self._jobs if start_date <= j.logged_date < end_date]


def _job(report_id, locksmith_name, logged_date, quoted_price, net_cost):
    return PanelJobSpend(
        report_id=report_id, locksmith_name=locksmith_name, logged_date=logged_date,
        quoted_price=quoted_price, net_cost=net_cost,
    )


class MtdSpendByLocksmithTests(TestCase):
    def _run(self, jobs, today=date(2026, 9, 15)):
        with patch(
            "apps.panel.services.spend.get_handl_client",
            return_value=FakePanelHandlClient(jobs),
        ):
            return mtd_spend_by_locksmith(today=today)

    def test_aggregates_job_count_and_totals_per_locksmith(self):
        jobs = [
            _job("1", "ABC Locksmiths", date(2026, 9, 3), 100.0, 150.0),
            _job("2", "ABC Locksmiths", date(2026, 9, 5), 120.0, 170.0),
        ]
        rows = self._run(jobs)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.locksmith_name, "ABC Locksmiths")
        self.assertEqual(row.job_count, 2)
        self.assertEqual(row.total_quoted_price, 220.0)
        self.assertEqual(row.total_net_cost, 320.0)
        self.assertEqual(row.margin, 100.0)

    def test_margin_is_net_cost_minus_quoted_price(self):
        jobs = [_job("1", "ABC Locksmiths", date(2026, 9, 3), 100.0, 150.0)]
        rows = self._run(jobs)
        self.assertEqual(rows[0].margin, 50.0)

    def test_job_missing_net_cost_still_counted_but_margin_is_none(self):
        jobs = [_job("1", "ABC Locksmiths", date(2026, 9, 3), 100.0, None)]
        rows = self._run(jobs)
        self.assertEqual(rows[0].job_count, 1)
        self.assertEqual(rows[0].total_quoted_price, 100.0)
        self.assertIsNone(rows[0].total_net_cost)
        self.assertIsNone(rows[0].margin)

    def test_job_missing_quoted_price_still_counted_but_margin_is_none(self):
        jobs = [_job("1", "ABC Locksmiths", date(2026, 9, 3), None, 150.0)]
        rows = self._run(jobs)
        self.assertEqual(rows[0].job_count, 1)
        self.assertIsNone(rows[0].total_quoted_price)
        self.assertEqual(rows[0].total_net_cost, 150.0)
        self.assertIsNone(rows[0].margin)

    def test_sorted_by_total_quoted_price_descending(self):
        jobs = [
            _job("1", "Small Spend Locksmiths", date(2026, 9, 3), 50.0, 60.0),
            _job("2", "Big Spend Locksmiths", date(2026, 9, 3), 500.0, 600.0),
        ]
        rows = self._run(jobs)
        self.assertEqual([r.locksmith_name for r in rows], ["Big Spend Locksmiths", "Small Spend Locksmiths"])

    def test_only_includes_this_month_up_to_today(self):
        jobs = [
            _job("1", "ABC Locksmiths", date(2026, 8, 31), 100.0, 150.0),  # last month
            _job("2", "ABC Locksmiths", date(2026, 9, 1), 100.0, 150.0),  # 1st of month
            _job("3", "ABC Locksmiths", date(2026, 9, 15), 100.0, 150.0),  # today
            _job("4", "ABC Locksmiths", date(2026, 9, 16), 100.0, 150.0),  # tomorrow
        ]
        rows = self._run(jobs, today=date(2026, 9, 15))
        self.assertEqual(rows[0].job_count, 2)

    def test_no_panel_jobs_returns_empty_list(self):
        self.assertEqual(self._run([]), [])


class PanelSpendViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)

    def test_spend_renders_locksmith_rows(self):
        jobs = [_job("1", "ABC Locksmiths", date.today(), 100.0, 150.0)]
        with patch(
            "apps.panel.services.spend.get_handl_client",
            return_value=FakePanelHandlClient(jobs),
        ):
            response = self.client.get(reverse("panel:spend"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ABC Locksmiths")
        self.assertContains(response, "£100.0")
        self.assertContains(response, "£50.0")

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("panel:spend"))
        self.assertEqual(response.status_code, 302)
