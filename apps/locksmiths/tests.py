import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from .management.commands.import_soter_locksmiths import parse_rows
from .models import Locksmith
from .services import group_locksmiths, normalize_base_name


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
        ("111", ""),
        ("1204", "WGTK - Andrew S"),
        ("1200", "XWGTK - Andrew S (A)"),
        ("887", "WGTK - Dean S (A)"),
        ("885", "WGTK - Dean S (V)"),
        ("999", "WGTK - BCA"),
        ("197", "Acorn Security Locksmiths Ltd T/A Keyhole Kates"),
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


class ParseRowsTests(TestCase):
    def test_parses_tab_separated_with_header(self):
        lines = ["ID\tLocksmithName", "111\tSome Locksmith", "112\tAnother One"]
        rows = parse_rows(lines)
        self.assertEqual(rows, [("111", "Some Locksmith"), ("112", "Another One")])

    def test_skips_blank_name_rows(self):
        lines = ["ID\tLocksmithName", "111", "112\tHas A Name"]
        rows = parse_rows(lines)
        self.assertEqual(rows, [("112", "Has A Name")])


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

    def test_extra_excludes_querystring_filters_preview(self):
        response = self.client.get(
            reverse("locksmiths:sync_from_soter"), {"extra_excludes": "andrew"}
        )
        self.assertNotContains(response, "WGTK - Andrew S")
        self.assertContains(response, "WGTK - Dean S")
