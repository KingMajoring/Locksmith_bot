"""Import active WGTK locksmiths from a Soter Lookup_Locksmiths export file.

Usage:
    python manage.py import_soter_locksmiths locksmiths.tsv
        (dry run — prints what it would do, changes nothing)
    python manage.py import_soter_locksmiths locksmiths.tsv --commit
        (actually creates/updates Locksmith + SoterLocksmithId records)

Input file: whatever you get from running, in Soter/Azure Data Studio
(or SSMS, or the portal's Query editor) and saving/exporting the
results — a plain 2-column export still works (email left blank):

    SELECT ID, LocksmithName, EmailAddress FROM Lookup_Locksmiths ORDER BY LocksmithName;

Tab- or comma-separated, 2 or 3 columns (ID, LocksmithName[, EmailAddress]),
header row optional — auto-detected.

Prefer the "Sync from Soter" page in /admin/ (Locksmiths) if the app
already has a working Soter connection — it does the same thing live,
no file export needed. This command is for offline review/import, or
if the app can't reach Soter for some reason.

Since a plain export like this has no WGTKLocksmith/isDeleted flag
columns, this command filters active-staff by parsing "WGTK -" vs
"XWGTK -" (ex-staff) out of the name text instead — see
apps/locksmiths/services.py for the full filtering/grouping rules.
"""
from __future__ import annotations

import csv
import sys

from django.core.management.base import BaseCommand, CommandError

from apps.locksmiths.services import commit_groups, group_locksmiths


def parse_rows(lines: list[str]) -> list[tuple[str, str, str]]:
    sample = "\n".join(lines[:5])
    dialect = csv.excel_tab if "\t" in sample else csv.excel
    reader = csv.reader(lines, dialect=dialect)
    rows = []
    for row in reader:
        if len(row) < 2:
            continue
        soter_id, name = row[0].strip(), row[1].strip()
        email = row[2].strip() if len(row) > 2 else ""
        if not soter_id or not soter_id[0].isdigit():
            continue  # header row or blank
        rows.append((soter_id, name, email))
    return rows


class Command(BaseCommand):
    help = "Import active WGTK locksmiths (from a Soter Lookup_Locksmiths export file) as Locksmith + SoterLocksmithId records."

    def add_arguments(self, parser):
        parser.add_argument(
            "file", nargs="?", help="Path to the exported ID/LocksmithName[/EmailAddress] file. Reads stdin if omitted."
        )
        parser.add_argument(
            "--commit", action="store_true", help="Actually write to the database. Default is dry-run."
        )
        parser.add_argument(
            "--exclude", default="", help="Comma-separated extra substrings to exclude (case-insensitive)."
        )

    def handle(self, *args, **options):
        if options["file"]:
            with open(options["file"], encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
        else:
            lines = sys.stdin.read().splitlines()

        rows = parse_rows(lines)
        if not rows:
            raise CommandError("No (ID, LocksmithName) rows found in the input.")

        extra_excludes = [s for s in options["exclude"].split(",") if s.strip()]
        groups, stats = group_locksmiths(rows, extra_excludes)

        self.stdout.write("")
        self.stdout.write(
            f"{len(groups)} locksmith(s) found, {stats['excluded']} non-person rows excluded, "
            f"{stats['xwgtk']} XWGTK (ex-staff) rows skipped."
        )
        self.stdout.write("")

        for base_upper, group in sorted(groups.items()):
            flag = " ⚠ unusual ID count, check this one" if len(group["ids"]) not in (1, 2) else ""
            email = group["email"] or "(no email)"
            self.stdout.write(f"{group['display']}: {', '.join(group['ids'])} — {email}{flag}")

        if not options["commit"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Dry run only — nothing written. Review the list above, then re-run with --commit."
            ))
            return

        created_locksmiths, created_ids, emails_updated = commit_groups(groups)
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Done. {created_locksmiths} new Locksmith record(s), {created_ids} new SoterLocksmithId row(s), "
            f"{emails_updated} existing email(s) updated."
        ))
