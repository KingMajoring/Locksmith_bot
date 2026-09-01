"""Import active WGTK locksmiths from a Soter Lookup_Locksmiths export file.

Usage:
    python manage.py import_soter_locksmiths locksmiths.tsv
        (dry run — prints what it would do, changes nothing)
    python manage.py import_soter_locksmiths locksmiths.tsv --commit
        (actually creates/updates Locksmith + SoterLocksmithId records)

Input file: whatever you get from running, in Soter/Azure Data Studio
(or SSMS, or the portal's Query editor) and saving/exporting the
results:

    SELECT ID, LocksmithName FROM Lookup_Locksmiths ORDER BY LocksmithName;

Tab- or comma-separated, two columns (ID, LocksmithName), header row
optional — auto-detected.

Prefer the "Sync from Soter" page in /admin/ (Locksmiths) if the app
already has a working Soter connection — it does the same thing live,
no file export needed. This command is for offline review/import, or
if the app can't reach Soter for some reason.

See apps/locksmiths/services.py for the filtering/grouping rules
(active-staff detection, non-person account exclusions, (V)/(A) row
grouping).
"""
from __future__ import annotations

import csv
import sys

from django.core.management.base import BaseCommand, CommandError

from apps.locksmiths.services import commit_groups, group_locksmiths


def parse_rows(lines: list[str]) -> list[tuple[str, str]]:
    sample = "\n".join(lines[:5])
    dialect = csv.excel_tab if "\t" in sample else csv.excel
    reader = csv.reader(lines, dialect=dialect)
    rows = []
    for row in reader:
        if len(row) < 2:
            continue
        soter_id, name = row[0].strip(), row[1].strip()
        if not soter_id or not soter_id[0].isdigit():
            continue  # header row or blank
        rows.append((soter_id, name))
    return rows


class Command(BaseCommand):
    help = "Import active WGTK locksmiths (from a Soter Lookup_Locksmiths export file) as Locksmith + SoterLocksmithId records."

    def add_arguments(self, parser):
        parser.add_argument(
            "file", nargs="?", help="Path to the exported ID/LocksmithName file. Reads stdin if omitted."
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
            self.stdout.write(f"{group['display']}: {', '.join(group['ids'])}{flag}")

        if not options["commit"]:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Dry run only — nothing written. Review the list above, then re-run with --commit."
            ))
            return

        created_locksmiths, created_ids = commit_groups(groups)
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Done. {created_locksmiths} new Locksmith record(s), {created_ids} new SoterLocksmithId row(s). "
            "Existing locksmiths with matching names were left as-is and just got any missing Soter IDs added."
        ))
