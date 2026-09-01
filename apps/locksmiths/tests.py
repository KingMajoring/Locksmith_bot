import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.integrations.optimo import OptimoDriverInfo

from .management.commands.import_soter_locksmiths import parse_rows
from .models import Locksmith, OptimoDriverId
from .services import (
    commit_optimo_driver_matches,
    group_locksmiths,
    match_optimo_drivers,
    normalize_base_name,
)


class NormalizeBaseNameTests(TestCase):
    def test_strips_v_and_a_suffixes(self):
        self.assertEqual(normalize_base_name("WGTK - Dean S (A)"), "WGTK - Dean S")
        self.assertEqual(normalize_base_name("WGTK - Dean S (V)"), "WGTK - Dean S")

    def test_collapses_double_spaces(self):
        self.assertEqual(normalize_base_name("WGTK -  Ricky Colley (A)"), "WGTK - Ricky Colley")

    def test_leaves_plain_names_untouched(self):
        self.assertEqual(normalize_base_name("WGTK - Andrew S"), "WGTK - Andrew S")


class GroupLocksmithsTests(TestCase):
    ROWS = [
        ("111", "", ""),
        ("1204", "WGTK - Andrew S", "andrew.s@wgtk.co.uk"),
        ("1200", "XWGTK - Andrew S (A)", ""),
        ("887", "WGTK - Dean S (A)", "dean.s@wgtk.co.uk"),
        ("885", "WGTK - Dean S (V)", ""),
        ("999", "WGTK - BCA", ""),
        ("197", "Acorn Security Locksmiths Ltd T/A Keyhole Kates", ""),
    ]

    def test_groups_and_counts_correctly(self):
        groups, stats = group_locksmiths(self.ROWS)
        self.assertEqual(stats, {"excluded": 1, "xwgtk": 1})
        self.assertEqual(set(groups.keys()), {"WGTK - ANDREW S", "WGTK - DEAN S"})
        self.assertEqual(sorted(groups["WGTK - DEAN S"]["ids"]), ["885", "887"])

    def test_extra_excludes_are_case_insensitive(self):
        groups, stats = group_locksmiths(self.ROWS, extra_excludes=["andrew"])
        self.assertNotIn("WGTK - ANDREW S", groups)
        self.assertEqual(stats["excluded"], 2)

    def test_captures_first_non_blank_email_in_group(self):
        groups, _ = group_locksmiths(self.ROWS)
        self.assertEqual(groups["WGTK - ANDREW S"]["email"], "andrew.s@wgtk.co.uk")
        # (A) row has the email, (V) row doesn't — either order should pick it up.
        self.assertEqual(groups["WGTK - DEAN S"]["email"], "dean.s@wgtk.co.uk")

    def test_already_filtered_skips_wgtk_xwgtk_check(self):
        rows = [
            ("500", "Some Panel Firm", ""),
            ("501", "XWGTK - Ex Staff", ""),
        ]
        groups, stats = group_locksmiths(rows, already_filtered=True)
        # Neither is excluded by name-prefix logic when already_filtered —
        # the caller (e.g. the live SQL query) is trusted to have scoped
        # rows to active WGTK staff already.
        self.assertEqual(set(groups.keys()), {"SOME PANEL FIRM", "XWGTK - EX STAFF"})
        self.assertEqual(stats, {"excluded": 0, "xwgtk": 0})


class ParseRowsTests(TestCase):
    def test_parses_tab_separated_with_header(self):
        lines = ["ID\tLocksmithName", "111\tSome Locksmith", "112\tAnother One"]
        rows = parse_rows(lines)
        self.assertEqual(rows, [("111", "Some Locksmith", ""), ("112", "Another One", "")])

    def test_skips_blank_name_rows(self):
        lines = ["ID\tLocksmithName", "111", "112\tHas A Name"]
        rows = parse_rows(lines)
        self.assertEqual(rows, [("112", "Has A Name", "")])

    def test_parses_optional_email_column(self):
        lines = ["ID\tLocksmithName\tEmailAddress", "111\tSome Locksmith\tsome@wgtk.co.uk"]
        rows = parse_rows(lines)
        self.assertEqual(rows, [("111", "Some Locksmith", "some@wgtk.co.uk")])


class ImportCommandTests(TestCase):
    SAMPLE = """ID\tLocksmithName
111
1204\tWGTK - Andrew S
1200\tXWGTK - Andrew S (A)
1201\tXWGTK - Andrew S (V)
887\tWGTK - Dean S (A)
885\tWGTK - Dean S (V)
999\tWGTK - BCA
1055\tWGTK - LOGISTICS TEAM
197\t Acorn Security Locksmiths Ltd T/A Keyhole Kates
"""

    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.sample_path = Path(tmpdir.name) / "locksmiths.tsv"
        self.sample_path.write_text(self.SAMPLE)

    def test_dry_run_makes_no_changes(self):
        call_command("import_soter_locksmiths", str(self.sample_path), stdout=StringIO())
        self.assertEqual(Locksmith.objects.count(), 0)

    def test_commit_creates_grouped_locksmiths(self):
        call_command("import_soter_locksmiths", str(self.sample_path), "--commit", stdout=StringIO())
        self.assertEqual(Locksmith.objects.count(), 2)

        andrew = Locksmith.objects.get(name="WGTK - Andrew S")
        self.assertEqual(andrew.soter_id_list, ["1204"])

        dean = Locksmith.objects.get(name="WGTK - Dean S")
        self.assertEqual(sorted(dean.soter_id_list), ["885", "887"])

    def test_excludes_non_person_and_panel_and_xwgtk_rows(self):
        call_command("import_soter_locksmiths", str(self.sample_path), "--commit", stdout=StringIO())
        names = set(Locksmith.objects.values_list("name", flat=True))
        self.assertNotIn("WGTK - BCA", names)
        self.assertNotIn("WGTK - LOGISTICS TEAM", names)
        self.assertFalse(any("Acorn" in n for n in names))
        self.assertFalse(any("Andrew S" in n and "XWGTK" in n for n in names))

    def test_rerunning_commit_is_idempotent(self):
        call_command("import_soter_locksmiths", str(self.sample_path), "--commit", stdout=StringIO())
        call_command("import_soter_locksmiths", str(self.sample_path), "--commit", stdout=StringIO())
        self.assertEqual(Locksmith.objects.count(), 2)
        dean = Locksmith.objects.get(name="WGTK - Dean S")
        self.assertEqual(dean.soter_ids.count(), 2)


class SyncFromSoterViewTests(TestCase):
    """Uses MockHandlClient's fixed list_locksmiths() fixture (no
    HANDL_SQL_SERVER set in tests) to exercise the live-sync page."""

    def setUp(self):
        # Mirrors the real access model: every WGTK SSO login is a full
        # superuser (apps/accounts/adapter.py), not just is_staff.
        self.user = get_user_model().objects.create_user(
            username="office_admin",
            email="admin@wgtk.co.uk",
            password="x",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_login(self.user)

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("locksmiths:sync_from_soter"))
        self.assertEqual(response.status_code, 302)

    def test_get_shows_preview_without_writing(self):
        response = self.client.get(reverse("locksmiths:sync_from_soter"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "WGTK - Andrew S")
        self.assertContains(response, "WGTK - Dean S")
        self.assertNotContains(response, "WGTK - BCA")
        self.assertEqual(Locksmith.objects.count(), 0)

    def test_post_commits_and_redirects(self):
        response = self.client.post(reverse("locksmiths:sync_from_soter"))
        self.assertRedirects(response, reverse("admin:locksmiths_locksmith_changelist"))
        self.assertEqual(Locksmith.objects.count(), 2)
        dean = Locksmith.objects.get(name="WGTK - Dean S")
        self.assertEqual(sorted(dean.soter_id_list), ["885", "887"])
        self.assertEqual(dean.email, "dean.s@wgtk.co.uk")

    def test_post_updates_email_on_existing_locksmith(self):
        Locksmith.objects.create(name="WGTK - Dean S", email="stale@example.com")
        self.client.post(reverse("locksmiths:sync_from_soter"))
        dean = Locksmith.objects.get(name="WGTK - Dean S")
        self.assertEqual(dean.email, "dean.s@wgtk.co.uk")

    def test_extra_excludes_querystring_filters_preview(self):
        response = self.client.get(
            reverse("locksmiths:sync_from_soter"), {"extra_excludes": "andrew"}
        )
        self.assertNotContains(response, "WGTK - Andrew S")
        self.assertContains(response, "WGTK - Dean S")


class AssignScheduleActionTests(TestCase):
    def setUp(self):
        from apps.stock_accuracy.models import StockCheckSchedule

        self.StockCheckSchedule = StockCheckSchedule
        self.user = get_user_model().objects.create_user(
            username="office_admin",
            email="admin@wgtk.co.uk",
            password="x",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_login(self.user)

    def _run_action(self, locksmiths):
        return self.client.post(
            reverse("admin:locksmiths_locksmith_changelist"),
            {
                "action": "assign_stock_check_schedule",
                "_selected_action": [str(l.pk) for l in locksmiths],
            },
            follow=True,
        )

    def test_creates_schedules_for_locksmiths_without_one(self):
        a = Locksmith.objects.create(name="WGTK - A")
        b = Locksmith.objects.create(name="WGTK - B")

        self._run_action([a, b])

        self.assertTrue(self.StockCheckSchedule.objects.filter(locksmith=a).exists())
        self.assertTrue(self.StockCheckSchedule.objects.filter(locksmith=b).exists())

    def test_skips_locksmiths_that_already_have_a_schedule(self):
        c = Locksmith.objects.create(name="WGTK - C")
        self.StockCheckSchedule.objects.create(locksmith=c, weekday=2, enabled=True)

        self._run_action([c])

        # Still exactly one schedule, untouched (still Wednesday).
        self.assertEqual(self.StockCheckSchedule.objects.filter(locksmith=c).count(), 1)
        self.assertEqual(self.StockCheckSchedule.objects.get(locksmith=c).weekday, 2)

    def test_spreads_new_schedules_across_all_five_weekdays(self):
        locksmiths = [Locksmith.objects.create(name=f"WGTK - Person {i}") for i in range(5)]

        self._run_action(locksmiths)

        weekdays = set(
            self.StockCheckSchedule.objects.filter(locksmith__in=locksmiths).values_list(
                "weekday", flat=True
            )
        )
        self.assertEqual(weekdays, {0, 1, 2, 3, 4})

    def test_balances_against_existing_schedules_not_just_selection(self):
        # Four locksmiths already on Monday; a fifth (new) selection
        # should go anywhere but Monday, since Monday's already busiest.
        for i in range(4):
            existing = Locksmith.objects.create(name=f"WGTK - Existing {i}")
            self.StockCheckSchedule.objects.create(locksmith=existing, weekday=0, enabled=True)
        newcomer = Locksmith.objects.create(name="WGTK - Newcomer")

        self._run_action([newcomer])

        self.assertNotEqual(
            self.StockCheckSchedule.objects.get(locksmith=newcomer).weekday, 0
        )


class MatchOptimoDriversTests(TestCase):
    def test_matches_by_email_first(self):
        locksmith = Locksmith.objects.create(name="WGTK - Chris Webster", email="chris.webster@wgtk.co.uk")
        driver = OptimoDriverInfo(
            driver_serial="ChrisWebster",
            driver_name="Some Other Name",  # deliberately wrong, to prove email wins
            driver_external_id="chris.webster@wgtk.co.uk",
        )
        matches, unmatched = match_optimo_drivers([driver])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["locksmith"], locksmith)
        self.assertEqual(matches[0]["reason"], "email")
        self.assertEqual(unmatched, [])

    def test_falls_back_to_name_match(self):
        locksmith = Locksmith.objects.create(name="WGTK - John Mason")
        driver = OptimoDriverInfo(
            driver_serial="JohnMason", driver_name="John Mason", driver_external_id=""
        )
        matches, unmatched = match_optimo_drivers([driver])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["locksmith"], locksmith)
        self.assertEqual(matches[0]["reason"], "name")

    def test_name_match_falls_back_to_driver_serial_when_name_blank(self):
        locksmith = Locksmith.objects.create(name="WGTK - John Mason")
        driver = OptimoDriverInfo(driver_serial="JohnMason", driver_name="", driver_external_id="")
        matches, unmatched = match_optimo_drivers([driver])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["locksmith"], locksmith)

    def test_no_match_goes_to_unmatched(self):
        driver = OptimoDriverInfo(
            driver_serial="Nobody", driver_name="Nobody Real", driver_external_id="nobody@example.com"
        )
        matches, unmatched = match_optimo_drivers([driver])
        self.assertEqual(matches, [])
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0]["driver"], driver)

    def test_already_mapped_driver_is_skipped_entirely(self):
        locksmith = Locksmith.objects.create(name="WGTK - John Mason", email="john@wgtk.co.uk")
        OptimoDriverId.objects.create(locksmith=locksmith, optimo_driver_serial="JohnMason")
        driver = OptimoDriverInfo(
            driver_serial="JohnMason", driver_name="John Mason", driver_external_id="john@wgtk.co.uk"
        )
        matches, unmatched = match_optimo_drivers([driver])
        self.assertEqual(matches, [])
        self.assertEqual(unmatched, [])


class CommitOptimoDriverMatchesTests(TestCase):
    def test_creates_optimo_driver_id_rows(self):
        locksmith = Locksmith.objects.create(name="WGTK - John Mason")
        driver = OptimoDriverInfo(driver_serial="JohnMason", driver_name="John Mason", driver_external_id="")
        created = commit_optimo_driver_matches(
            [{"driver": driver, "locksmith": locksmith, "reason": "name"}]
        )
        self.assertEqual(created, 1)
        self.assertEqual(
            OptimoDriverId.objects.get(locksmith=locksmith).optimo_driver_serial, "JohnMason"
        )

    def test_idempotent_on_rerun(self):
        locksmith = Locksmith.objects.create(name="WGTK - John Mason")
        driver = OptimoDriverInfo(driver_serial="JohnMason", driver_name="John Mason", driver_external_id="")
        match = [{"driver": driver, "locksmith": locksmith, "reason": "name"}]
        commit_optimo_driver_matches(match)
        created_again = commit_optimo_driver_matches(match)
        self.assertEqual(created_again, 0)
        self.assertEqual(OptimoDriverId.objects.count(), 1)


class SyncFromOptimoViewTests(TestCase):
    """Uses MockOptimoClient's fixed list_recent_drivers() fixture (no
    OptimoSettings key set in tests) to exercise the live-sync page."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="office_admin",
            email="admin@wgtk.co.uk",
            password="x",
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_login(self.user)

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("locksmiths:sync_from_optimo"))
        self.assertEqual(response.status_code, 302)

    def test_get_shows_preview_without_writing(self):
        response = self.client.get(reverse("locksmiths:sync_from_optimo"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(OptimoDriverId.objects.count(), 0)

    def test_post_commits_matches_and_redirects(self):
        # MockOptimoClient's fixed drivers use emails like
        # "driver011@example.com" — match a locksmith to one of those
        # to prove the commit path writes an OptimoDriverId row.
        locksmith = Locksmith.objects.create(name="WGTK - Test Driver", email="driver011@example.com")
        response = self.client.post(reverse("locksmiths:sync_from_optimo"))
        self.assertRedirects(response, reverse("admin:locksmiths_locksmith_changelist"))
        self.assertEqual(
            OptimoDriverId.objects.get(locksmith=locksmith).optimo_driver_serial, "011"
        )
