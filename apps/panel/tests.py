from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.integrations.handl import PanelDailyFigures

from .services.spend import mtd_spend_by_locksmith


class FakePanelHandlClient:
    def __init__(self, figures):
        self._figures = figures

    def get_panel_daily_figures(self, start_date, end_date):
        return [f for f in self._figures if start_date <= f.figure_date < end_date]


def _figure(locksmith_name, figure_date, job_count, wgtk_fee, net_cost):
    return PanelDailyFigures(
        panel_name=locksmith_name, figure_date=figure_date, job_count=job_count,
        wgtk_fee=wgtk_fee, net_cost=net_cost,
    )


class MtdSpendByLocksmithTests(TestCase):
    def _run(self, figures, today=date(2026, 9, 15)):
        with patch(
            "apps.panel.services.spend.get_handl_client",
            return_value=FakePanelHandlClient(figures),
        ):
            return mtd_spend_by_locksmith(today=today)

    def test_aggregates_job_count_and_totals_per_locksmith(self):
        figures = [
            _figure("ABC Locksmiths", date(2026, 9, 3), 1, 50.0, 150.0),
            _figure("ABC Locksmiths", date(2026, 9, 5), 2, 70.0, 190.0),
        ]
        rows = self._run(figures)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.locksmith_name, "ABC Locksmiths")
        self.assertEqual(row.job_count, 3)
        # quoted = net_cost - wgtk_fee per day: (150-50)+(190-70) = 100+120 = 220
        self.assertEqual(row.total_quoted_price, 220.0)
        # selling cost = quoted + fee per day = net_cost, summed: 150+190 = 340
        self.assertEqual(row.selling_cost, 340.0)

    def test_selling_cost_is_quoted_price_plus_wgtk_fee(self):
        figures = [_figure("ABC Locksmiths", date(2026, 9, 3), 1, 50.0, 150.0)]
        rows = self._run(figures)
        self.assertEqual(rows[0].total_quoted_price, 100.0)
        self.assertEqual(rows[0].selling_cost, 150.0)

    def test_day_missing_net_cost_still_counted_in_jobs_but_excluded_from_money(self):
        figures = [_figure("ABC Locksmiths", date(2026, 9, 3), 1, 50.0, None)]
        rows = self._run(figures)
        self.assertEqual(rows[0].job_count, 1)
        self.assertIsNone(rows[0].total_quoted_price)
        self.assertIsNone(rows[0].selling_cost)

    def test_day_missing_wgtk_fee_still_counted_in_jobs_but_excluded_from_money(self):
        figures = [_figure("ABC Locksmiths", date(2026, 9, 3), 1, None, 150.0)]
        rows = self._run(figures)
        self.assertEqual(rows[0].job_count, 1)
        self.assertIsNone(rows[0].total_quoted_price)
        self.assertIsNone(rows[0].selling_cost)

    def test_partial_days_only_sum_the_days_with_both_figures(self):
        """One day has both figures, another is missing wgtk_fee — the
        money totals should reflect only the complete day, not silently
        mix net_cost from one day with fee from another."""
        figures = [
            _figure("ABC Locksmiths", date(2026, 9, 3), 1, 50.0, 150.0),
            _figure("ABC Locksmiths", date(2026, 9, 4), 1, None, 200.0),
        ]
        rows = self._run(figures)
        self.assertEqual(rows[0].job_count, 2)
        self.assertEqual(rows[0].total_quoted_price, 100.0)
        self.assertEqual(rows[0].selling_cost, 150.0)

    def test_sorted_by_total_quoted_price_descending(self):
        figures = [
            _figure("Small Spend Locksmiths", date(2026, 9, 3), 1, 10.0, 60.0),
            _figure("Big Spend Locksmiths", date(2026, 9, 3), 1, 100.0, 600.0),
        ]
        rows = self._run(figures)
        self.assertEqual([r.locksmith_name for r in rows], ["Big Spend Locksmiths", "Small Spend Locksmiths"])

    def test_only_includes_this_month_up_to_today(self):
        figures = [
            _figure("ABC Locksmiths", date(2026, 8, 31), 1, 50.0, 150.0),  # last month
            _figure("ABC Locksmiths", date(2026, 9, 1), 1, 50.0, 150.0),  # 1st of month
            _figure("ABC Locksmiths", date(2026, 9, 15), 1, 50.0, 150.0),  # today
            _figure("ABC Locksmiths", date(2026, 9, 16), 1, 50.0, 150.0),  # tomorrow
        ]
        rows = self._run(figures, today=date(2026, 9, 15))
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
        figures = [_figure("ABC Locksmiths", date.today(), 1, 50.0, 150.0)]
        with patch(
            "apps.panel.services.spend.get_handl_client",
            return_value=FakePanelHandlClient(figures),
        ):
            response = self.client.get(reverse("panel:spend"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ABC Locksmiths")
        self.assertContains(response, "£100.0")
        self.assertContains(response, "£150.0")
        self.assertNotContains(response, "WGTK margin")

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("panel:spend"))
        self.assertEqual(response.status_code, 302)
