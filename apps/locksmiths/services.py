"""Shared logic for turning a Soter Lookup_Locksmiths row list into
Locksmith + SoterLocksmithId records — used by both the management
command (file-based import) and the in-app "sync from Soter" page.

Only rows whose name starts with "WGTK" (case-insensitive) and NOT
"XWGTK" (which marks ex-staff) are considered current active staff;
everything else in Lookup_Locksmiths is a panel/subcontractor firm —
also how Panelled Jobs (Area 4) will identify a job that went to panel.
A locksmith usually has two rows in Soter — a "(V)" and "(A)" suffix
for the same person — which get grouped into one Locksmith with two
SoterLocksmithId rows under it.
"""
from __future__ import annotations

import re

from .models import Locksmith, SoterLocksmithId

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


def group_locksmiths(
    rows: list[tuple[str, str]], extra_excludes: list[str] = ()
) -> tuple[dict[str, dict], dict[str, int]]:
    """rows: (soter_id, name) pairs, e.g. from HandlClient.list_locksmiths()
    or a file export. Returns (groups, stats):
    - groups: {normalized_upper_name: {"display": str, "ids": [str]}}
    - stats: {"excluded": int, "xwgtk": int}, plus per-row exclusion log
      available by re-deriving from the caller if needed.
    """
    exclude_substrings = [s.strip().upper() for s in extra_excludes if s.strip()]
    exclude_substrings += DEFAULT_EXCLUDE_SUBSTRINGS

    groups: dict[str, dict] = {}
    stats = {"excluded": 0, "xwgtk": 0}

    for soter_id, name in rows:
        upper = name.strip().upper()
        if upper.startswith("XWGTK"):
            stats["xwgtk"] += 1
            continue
        if not upper.startswith("WGTK"):
            continue  # panel/subcontractor firm, not ours

        base = normalize_base_name(name)
        base_upper = base.upper()
        if any(term in base_upper for term in exclude_substrings):
            stats["excluded"] += 1
            continue

        group = groups.setdefault(base_upper, {"display": base, "ids": []})
        group["ids"].append(soter_id)

    return groups, stats


def commit_groups(groups: dict[str, dict]) -> tuple[int, int]:
    """Creates/updates Locksmith + SoterLocksmithId records from
    group_locksmiths()'s output. Returns (new_locksmiths, new_soter_ids)."""
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
    return created_locksmiths, created_ids
