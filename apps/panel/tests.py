from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.integrations.handl import PanelDailyFigures

from .services.spend import month_bounds, panel_spend_for_month


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


class MonthBoundsTests(TestCase):
    def test_zero_months_ago_is_current_month(self):
        start, end = month_bounds(0, today=date(2026, 9, 15))
        self.assertEqual(start, date(2026, 9, 1))
        self.assertEqual(end, date(2026, 10, 1))

    def test_one_month_ago(self):
        start, end = month_bounds(1, today=date(2026, 9, 15))
        self.assertEqual(start, date(2026, 8, 1))
        self.assertEqual(end, date(2026, 9, 1))

    def test_crosses_year_boundary(self):
        start, end = month_bounds(2, today=date(2026, 1, 20))
        self.assertEqual(start, date(2025, 11, 1))
        self.assertEqual(end, date(2025, 12, 1))

    def test_december_end_rolls_into_next_year(self):
        start, end = month_bounds(0, today=date(2026, 12, 5))
        self.assertEqual(start, date(2026, 12, 1))
        self.assertEqual(end, date(2027, 1, 1))


class PanelSpendForMonthTests(TestCase):
    def _run(self, figures, months_ago=0, today=date(2026, 9, 15)):
        with patch(
            "apps.panel.services.spend.get_handl_client",
            return_value=FakePanelHandlClient(figures),
        ):
            return panel_spend_for_month(months_ago=months_ago, today=today)

    def test_aggregates_job_count_and_totals_per_locksmith(self):
        figures = [
            _figure("ABC Locksmiths", date(2026, 9, 3), 1, 50.0, 150.0),
            _figure("ABC Locksmiths", date(2026, 9, 5), 2, 70.0, 190.0),
        ]
        rows, totals, month_start = self._run(figures)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.locksmith_name, "ABC Locksmiths")
        self.assertEqual(row.job_count, 3)
        # quoted = net_cost - wgtk_fee per day: (150-50)+(190-70) = 100+120 = 220
        self.assertEqual(row.total_quoted_price, 220.0)
        # selling cost = quoted + fee per day = net_cost, summed: 150+190 = 340
        self.assertEqual(row.selling_cost, 340.0)
        self.assertEqual(month_start, date(2026, 9, 1))

    def test_selling_cost_is_quoted_price_plus_wgtk_fee(self):
        figures = [_figure("ABC Locksmiths", date(2026, 9, 3), 1, 50.0, 150.0)]
        rows, _totals, _month_start = self._run(figures)
        self.assertEqual(rows[0].total_quoted_price, 100.0)
        self.assertEqual(rows[0].selling_cost, 150.0)

    def test_day_missing_net_cost_still_counted_in_jobs_but_excluded_from_money(self):
        figures = [_figure("ABC Locksmiths", date(2026, 9, 3), 1, 50.0, None)]
        rows, _totals, _month_start = self._run(figures)
        self.assertEqual(rows[0].job_count, 1)
        self.assertIsNone(rows[0].total_quoted_price)
        self.assertIsNone(rows[0].selling_cost)

    def test_day_missing_wgtk_fee_still_counted_in_jobs_but_excluded_from_money(self):
        figures = [_figure("ABC Locksmiths", date(2026, 9, 3), 1, None, 150.0)]
        rows, _totals, _month_start = self._run(figures)
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
        rows, _totals, _month_start = self._run(figures)
        self.assertEqual(rows[0].job_count, 2)
        self.assertEqual(rows[0].total_quoted_price, 100.0)
        self.assertEqual(rows[0].selling_cost, 150.0)

    def test_sorted_by_total_quoted_price_descending(self):
        figures = [
            _figure("Small Spend Locksmiths", date(2026, 9, 3), 1, 10.0, 60.0),
            _figure("Big Spend Locksmiths", date(2026, 9, 3), 1, 100.0, 600.0),
        ]
        rows, _totals, _month_start = self._run(figures)
        self.assertEqual([r.locksmith_name for r in rows], ["Big Spend Locksmiths", "Small Spend Locksmiths"])

    def test_only_includes_the_requested_calendar_month(self):
        figures = [
            _figure("ABC Locksmiths", date(2026, 8, 31), 1, 50.0, 150.0),  # last month
            _figure("ABC Locksmiths", date(2026, 9, 1), 1, 50.0, 150.0),  # 1st of month
            _figure("ABC Locksmiths", date(2026, 9, 30), 1, 50.0, 150.0),  # last day of month
            _figure("ABC Locksmiths", date(2026, 10, 1), 1, 50.0, 150.0),  # next month
        ]
        rows, _totals, _month_start = self._run(figures, months_ago=0, today=date(2026, 9, 15))
        self.assertEqual(rows[0].job_count, 2)

    def test_can_navigate_to_a_previous_month(self):
        figures = [
            _figure("ABC Locksmiths", date(2026, 8, 15), 1, 50.0, 150.0),
            _figure("ABC Locksmiths", date(2026, 9, 15), 1, 50.0, 150.0),
        ]
        rows, _totals, month_start = self._run(figures, months_ago=1, today=date(2026, 9, 15))
        self.assertEqual(month_start, date(2026, 8, 1))
        self.assertEqual(rows[0].job_count, 1)

    def test_no_panel_jobs_returns_empty_list_and_zeroed_totals(self):
        rows, totals, _month_start = self._run([])
        self.assertEqual(rows, [])
        self.assertEqual(totals.job_count, 0)
        self.assertIsNone(totals.total_quoted_price)
        self.assertIsNone(totals.selling_cost)

    def test_totals_sum_across_every_locksmith(self):
        figures = [
            _figure("ABC Locksmiths", date(2026, 9, 3), 2, 50.0, 150.0),
            _figure("XYZ Locksmiths", date(2026, 9, 3), 1, 20.0, 80.0),
        ]
        _rows, totals, _month_start = self._run(figures)
        self.assertEqual(totals.job_count, 3)
        self.assertEqual(totals.total_quoted_price, 160.0)  # (150-50) + (80-20)
        self.assertEqual(totals.selling_cost, 230.0)  # 150 + 80

    def test_totals_skip_locksmiths_with_no_money_figures_but_still_count_jobs(self):
        figures = [
            _figure("ABC Locksmiths", date(2026, 9, 3), 2, None, None),
            _figure("XYZ Locksmiths", date(2026, 9, 3), 1, 20.0, 80.0),
        ]
        _rows, totals, _month_start = self._run(figures)
        self.assertEqual(totals.job_count, 3)
        self.assertEqual(totals.total_quoted_price, 60.0)
        self.assertEqual(totals.selling_cost, 80.0)


class PanelSpendViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)

    def test_spend_renders_locksmith_rows_and_totals(self):
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
        self.assertContains(response, "Jobs panelled")

    def test_months_ago_query_param_selects_a_different_month(self):
        today = date.today()
        last_month_date = today.replace(day=1) - timedelta(days=1)
        figures = [_figure("Last Month Locksmiths", last_month_date, 1, 50.0, 150.0)]
        with patch(
            "apps.panel.services.spend.get_handl_client",
            return_value=FakePanelHandlClient(figures),
        ):
            response = self.client.get(reverse("panel:spend"), {"months_ago": 1})
        self.assertContains(response, "Last Month Locksmiths")

    def test_next_month_link_hidden_on_current_month(self):
        with patch(
            "apps.panel.services.spend.get_handl_client",
            return_value=FakePanelHandlClient([]),
        ):
            response = self.client.get(reverse("panel:spend"))
        self.assertNotContains(response, "Next month")

    def test_next_month_link_shown_when_viewing_a_past_month(self):
        with patch(
            "apps.panel.services.spend.get_handl_client",
            return_value=FakePanelHandlClient([]),
        ):
            response = self.client.get(reverse("panel:spend"), {"months_ago": 1})
        self.assertContains(response, "Next month")

    def test_negative_months_ago_is_clamped_to_current_month(self):
        with patch(
            "apps.panel.services.spend.get_handl_client",
            return_value=FakePanelHandlClient([]),
        ):
            response = self.client.get(reverse("panel:spend"), {"months_ago": -5})
        self.assertNotContains(response, "Next month")

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("panel:spend"))
        self.assertEqual(response.status_code, 302)
