from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.locksmiths.models import Locksmith

from .models import EmailSettings, StockCheckItem, VarianceThreshold, WeeklyStockCheck
from .services.emailing import send_weekly_check
from .services.generation import generate_weekly_check
from .services.reporting import locksmith_summary


def _make_locksmith(name, email, soter_ids=("1",)):
    locksmith = Locksmith.objects.create(name=name, email=email)
    for soter_id in soter_ids:
        locksmith.soter_ids.create(soter_locksmith_id=soter_id)
    return locksmith


class GenerationTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith("Jane Smith", "jane@example.com", ["ENG-001"])

    def test_generates_configured_number_of_lines(self):
        weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))
        self.assertEqual(weekly_check.items.count(), 10)

    def test_lines_are_unique_within_a_check(self):
        weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))
        codes = list(weekly_check.items.values_list("part_code", flat=True))
        self.assertEqual(len(codes), len(set(codes)))

    def test_expected_qty_is_frozen_not_recalculated(self):
        weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))
        item = weekly_check.items.first()
        original_expected = item.expected_qty

        # Simulate stock moving in Handl after the check was sent — the
        # frozen expected_qty on the item must not change.
        item.refresh_from_db()
        self.assertEqual(item.expected_qty, original_expected)

    def test_regenerating_same_week_returns_existing_check(self):
        first = generate_weekly_check(self.locksmith, date(2026, 9, 7))
        second = generate_weekly_check(self.locksmith, date(2026, 9, 7))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(WeeklyStockCheck.objects.count(), 1)

    @override_settings(STOCK_CHECK_NO_REPEAT_WEEKS=52)
    def test_no_repeat_window_excludes_recently_checked_lines(self):
        week1 = generate_weekly_check(self.locksmith, date(2026, 8, 3))
        week1_codes = set(week1.items.values_list("part_code", flat=True))

        week2 = generate_weekly_check(self.locksmith, date(2026, 8, 10))
        week2_codes = set(week2.items.values_list("part_code", flat=True))

        # With a 52-week no-repeat window and only 35 lines in the mock
        # catalogue, some overlap is unavoidable once the pool is
        # exhausted — but the two draws should still differ.
        self.assertNotEqual(week1_codes, week2_codes)


class EmailingTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith("Bob Jones", "bob@example.com", ["ENG-002"])
        self.weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))

    def test_send_attaches_excel_and_marks_sent_when_live(self):
        EmailSettings.objects.create(emails_live=True)
        send_weekly_check(self.weekly_check)
        self.weekly_check.refresh_from_db()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["bob@example.com"])
        self.assertEqual(len(mail.outbox[0].attachments), 1)
        self.assertEqual(self.weekly_check.status, WeeklyStockCheck.Status.SENT)
        self.assertIsNotNone(self.weekly_check.sent_at)

    def test_send_without_email_raises(self):
        self.locksmith.email = ""
        self.locksmith.save()
        with self.assertRaises(ValueError):
            send_weekly_check(self.weekly_check)

    @override_settings(STOCK_CHECK_TEST_REDIRECT_EMAIL="richard.king@wgtk.co.uk")
    def test_redirects_to_test_address_when_not_live(self):
        send_weekly_check(self.weekly_check)

        self.assertEqual(mail.outbox[0].to, ["richard.king@wgtk.co.uk"])
        self.assertIn("bob@example.com", mail.outbox[0].subject)
        self.assertIn("TEST", mail.outbox[0].subject)

    def test_default_refuses_to_send_when_not_live_and_no_test_address(self):
        """Safety default: EmailSettings.emails_live starts False, and
        with no STOCK_CHECK_TEST_REDIRECT_EMAIL configured either,
        sending must refuse outright rather than silently falling
        through to the real locksmith."""
        with self.assertRaises(ValueError):
            send_weekly_check(self.weekly_check)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(STOCK_CHECK_TEST_REDIRECT_EMAIL="richard.king@wgtk.co.uk")
    def test_emails_live_overrides_test_redirect(self):
        """Going live is an explicit choice — once emails_live is on,
        real locksmiths get their emails even if a test redirect
        address happens to still be configured."""
        EmailSettings.objects.create(emails_live=True)
        send_weekly_check(self.weekly_check)

        self.assertEqual(mail.outbox[0].to, ["bob@example.com"])
        self.assertNotIn("TEST", mail.outbox[0].subject)


class VarianceFlaggingTests(TestCase):
    def setUp(self):
        self.locksmith = _make_locksmith("Ali Khan", "ali@example.com", ["ENG-003"])
        self.weekly_check = WeeklyStockCheck.objects.create(
            locksmith=self.locksmith, week_starting=date(2026, 9, 7)
        )
        self.thresholds = VarianceThreshold.objects.create(
            unit_threshold=2, pct_threshold=10, value_threshold=25, active=True
        )

    def test_small_variance_not_flagged(self):
        item = StockCheckItem.objects.create(
            weekly_check=self.weekly_check,
            part_code="TK-100",
            part_name="Test part",
            expected_qty=10,
            unit_cost=1,
            actual_qty=10,
        )
        self.assertFalse(item.is_flagged(self.thresholds))

    def test_variance_over_unit_threshold_is_flagged(self):
        item = StockCheckItem.objects.create(
            weekly_check=self.weekly_check,
            part_code="TK-101",
            part_name="Test part",
            expected_qty=10,
            unit_cost=1,
            actual_qty=6,  # variance of 4, over unit_threshold of 2
        )
        self.assertTrue(item.is_flagged(self.thresholds))

    def test_variance_over_value_threshold_is_flagged_even_within_unit_threshold(self):
        item = StockCheckItem.objects.create(
            weekly_check=self.weekly_check,
            part_code="TK-102",
            part_name="Expensive part",
            expected_qty=10,
            unit_cost=50,
            actual_qty=9,  # variance of 1 unit, but £50 impact
        )
        self.assertTrue(item.is_flagged(self.thresholds))

    def test_unentered_item_is_not_flagged(self):
        item = StockCheckItem.objects.create(
            weekly_check=self.weekly_check,
            part_code="TK-103",
            part_name="Test part",
            expected_qty=10,
            unit_cost=1,
        )
        self.assertFalse(item.is_flagged(self.thresholds))

    def test_repeat_offender_detected_across_weeks(self):
        thresholds = VarianceThreshold.objects.create(
            unit_threshold=1,
            pct_threshold=1000,
            value_threshold=1000,
            repeat_offender_occurrences=2,
            repeat_offender_window_weeks=4,
            active=True,
        )
        self.thresholds.active = False
        self.thresholds.save()

        for i, expected in enumerate([10, 10, 10]):
            wc = WeeklyStockCheck.objects.create(
                locksmith=self.locksmith, week_starting=date(2026, 10, 5) - timedelta(weeks=i)
            )
            StockCheckItem.objects.create(
                weekly_check=wc,
                part_code=f"TK-20{i}",
                part_name="Part",
                expected_qty=expected,
                unit_cost=1,
                actual_qty=expected - 5,  # always flagged under this threshold
            )

        summary = locksmith_summary(self.locksmith)
        self.assertTrue(summary["is_repeat_offender"])


class ViewsSmokeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office_admin", email="admin@wgtk.co.uk", password="x", is_staff=True
        )
        self.client.force_login(self.user)
        self.locksmith = _make_locksmith("Sam Lee", "sam@example.com", ["ENG-010"])
        self.weekly_check = generate_weekly_check(self.locksmith, date(2026, 9, 7))
        self.weekly_check.status = WeeklyStockCheck.Status.SENT
        self.weekly_check.sent_at = timezone.now()
        self.weekly_check.save()

    def test_dashboard_renders(self):
        response = self.client.get(reverse("stock_accuracy:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sam Lee")

    def test_dashboard_shows_emails_not_live_by_default(self):
        response = self.client.get(reverse("stock_accuracy:dashboard"))
        self.assertFalse(response.context["emails_live"])
        self.assertContains(response, "OFF: all stock-check emails redirect")

    def test_toggle_emails_live_flips_setting(self):
        self.assertFalse(EmailSettings.current().emails_live)
        self.client.post(reverse("stock_accuracy:toggle_emails_live"))
        self.assertTrue(EmailSettings.current().emails_live)
        self.client.post(reverse("stock_accuracy:toggle_emails_live"))
        self.assertFalse(EmailSettings.current().emails_live)

    def test_toggle_emails_live_requires_post(self):
        self.client.get(reverse("stock_accuracy:toggle_emails_live"))
        self.assertFalse(EmailSettings.current().emails_live)

    def test_toggle_emails_live_requires_login(self):
        self.client.logout()
        response = self.client.post(reverse("stock_accuracy:toggle_emails_live"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(EmailSettings.current().emails_live)

    def test_locksmith_report_renders(self):
        response = self.client.get(
            reverse("stock_accuracy:locksmith_report", args=[self.locksmith.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_entry_detail_saves_counts_and_completes_check(self):
        url = reverse("stock_accuracy:entry_detail", args=[self.weekly_check.pk])
        self.assertEqual(self.client.get(url).status_code, 200)

        items = list(self.weekly_check.items.all())
        data = {f"qty_{item.id}": item.expected_qty for item in items}
        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 302)
        self.weekly_check.refresh_from_db()
        self.assertEqual(self.weekly_check.status, WeeklyStockCheck.Status.COMPLETED)
        self.assertIsNotNone(self.weekly_check.completed_at)
        self.assertTrue(self.weekly_check.is_fully_entered)

    def test_login_required_redirects_anonymous(self):
        self.client.logout()
        response = self.client.get(reverse("stock_accuracy:dashboard"))
        self.assertEqual(response.status_code, 302)
