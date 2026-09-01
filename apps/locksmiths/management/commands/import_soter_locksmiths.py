"""Import active WGTK locksmiths from a Soter Lookup_Locksmiths export.

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

Only rows whose name starts with "WGTK" (case-insensitive) and NOT
"XWGTK" (which marks ex-staff) are considered current active staff;
everything else in Lookup_Locksmiths is a panel/subcontractor firm.
A locksmith usually has two rows in Soter — a "(V)" and "(A)" suffix
for the same person — which get grouped into one Locksmith with two
SoterLocksmithId rows under it.

A curated list of non-person "WGTK -" accounts (logistics, spare vans,
test data etc.) is excluded by default — extend it with --exclude if
Soter's roster has grown new ones since. Always review the dry-run
output before passing --commit; group names flagged "⚠" are the ones
worth double-checking.
"""
from __future__ import annotations

import csv
import re
import sys

from django.core.management.base import BaseCommand, CommandError

from apps.locksmiths.models import Locksmith, SoterLocksmithId

DEFAULT_EXCLUDE_SUBSTRINGS = [
    "BCA",
    "EBAY/PARTS",
    "LOGISTICS TEAM",
    "MANHEIM",
    "OFFICE",
    "CANCELLATION",
    "TRAINING SCHOOL",
    "SPARE VAN",
    "PRE CODED/CLOSED FILES",
    "TRADE TEAM",
    "TESTING VLKS",
    "CLIENT",
]

_SUFFIX_RE = re.compile(r"\s*\(\s*[AV]\s*\)\s*$", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_base_name(name: str) -> str:
    name = name.strip()
    name = _SUFFIX_RE.sub("", name)
    return _WHITESPACE_RE.sub(" ", name).strip()


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
    help = "Import active WGTK locksmiths (from a Soter Lookup_Locksmiths export) as Locksmith + SoterLocksmithId records."

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

        exclude_substrings = [s.strip().upper() for s in options["exclude"].split(",") if s.strip()]
        exclude_substrings += DEFAULT_EXCLUDE_SUBSTRINGS

        groups: dict[str, dict] = {}  # normalized upper name -> {"display": str, "ids": [str]}
        excluded_count = 0
        xwgtk_count = 0

        for soter_id, name in rows:
            upper = name.strip().upper()
            if upper.startswith("XWGTK"):
                xwgtk_count += 1
                continue
            if not upper.startswith("WGTK"):
                continue  # panel/subcontractor firm, not ours

            base = normalize_base_name(name)
            base_upper = base.upper()
            if any(term in base_upper for term in exclude_substrings):
                excluded_count += 1
                self.stdout.write(f"  excluded (non-person): {soter_id}\t{name}")
                continue

            group = groups.setdefault(base_upper, {"display": base, "ids": []})
            group["ids"].append(soter_id)

        self.stdout.write("")
        self.stdout.write(f"{len(groups)} locksmith(s) found, {excluded_count} non-person rows excluded, "
                           f"{xwgtk_count} XWGTK (ex-staff) rows skipped.")
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

        created_locksmiths = 0
        created_ids = 0
        for group in groups.values():
            locksmith, was_created = Locksmith.objects.get_or_create(
                name=group["display"], defaults={"active": True}
            )
            created_locksmiths += int(was_created)
            for soter_id in group["ids"]:
                _, id_created = SoterLocksmithId.objects.get_or_create(
                    locksmith=locksmith, soter_locksmith_id=soter_id
                )
                created_ids += int(id_created)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Done. {created_locksmiths} new Locksmith record(s), {created_ids} new SoterLocksmithId row(s). "
            "Existing locksmiths with matching names were left as-is and just got any missing Soter IDs added."
        ))
