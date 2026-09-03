"""Shared logic for turning a Soter Lookup_Locksmiths row list into
Locksmith + SoterLocksmithId records — used by both the management
command (file-based import) and the in-app "sync from Soter" page.

The live path (HandlClient.list_locksmiths()) already filters to active
WGTK-owned rows in SQL (WGTKLocksmith=1, isDeleted=0) — pass
already_filtered=True there. The file-based command can't rely on that
(a plain ID/Name export has no flag columns), so it falls back to
parsing "WGTK -" vs "XWGTK -" (ex-staff) out of the name text.

Either way, a locksmith usually has two rows in Soter — a "(V)" and
"(A)" suffix for the same person — which get grouped into one Locksmith
with two SoterLocksmithId rows under it. A curated list of non-person
"WGTK -" accounts (logistics, spare vans, test data etc.) is always
excluded, flag-filtered or not, since WGTKLocksmith=1 just means "ours",
not "an actual person."
"""
from __future__ import annotations

import re

from .models import Locksmith, OptimoDriverId, SoterLocksmithId

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

_SUFFIX_RE = re.compile(r"\s*\(\s*([AV])\s*\)\s*$", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_base_name(name: str) -> str:
    name = name.strip()
    name = _SUFFIX_RE.sub("", name)
    return _WHITESPACE_RE.sub(" ", name).strip()


def _extract_location(name: str) -> str:
    """"WGTK - Blain H (V)" -> "V", "WGTK - Blain H (A)" -> "A",
    no suffix -> ""."""
    match = _SUFFIX_RE.search(name.strip())
    return match.group(1).upper() if match else ""


def group_locksmiths(
    rows: list[tuple[str, str, str]],
    extra_excludes: list[str] = (),
    already_filtered: bool = False,
) -> tuple[dict[str, dict], dict[str, int]]:
    """rows: (soter_id, name, email) triples, e.g. from
    HandlClient.list_locksmiths() or a file export (email may be "").
    already_filtered: skip the WGTK-/XWGTK- name check, because rows
    are already scoped to active WGTK locksmiths (e.g. the live SQL
    query already did WHERE WGTKLocksmith = 1 AND isDeleted = 0).

    Returns (groups, stats):
    - groups: {normalized_upper_name: {"display": str, "ids": [str], "locations": {id: "V"|"A"|""}, "email": str}}
    - stats: {"excluded": int, "xwgtk": int}
    """
    exclude_substrings = [s.strip().upper() for s in extra_excludes if s.strip()]
    exclude_substrings += DEFAULT_EXCLUDE_SUBSTRINGS

    groups: dict[str, dict] = {}
    stats = {"excluded": 0, "xwgtk": 0}

    for soter_id, name, *rest in rows:
        email = rest[0] if rest else ""

        if not already_filtered:
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

        group = groups.setdefault(
            base_upper, {"display": base, "ids": [], "locations": {}, "email": ""}
        )
        group["ids"].append(soter_id)
        group["locations"][soter_id] = _extract_location(name)
        if email and not group["email"]:
            group["email"] = email

    return groups, stats


def commit_groups(groups: dict[str, dict]) -> tuple[int, int, int]:
    """Creates/updates Locksmith + SoterLocksmithId records from
    group_locksmiths()'s output. Returns
    (new_locksmiths, new_soter_ids, emails_updated)."""
    created_locksmiths = 0
    created_ids = 0
    emails_updated = 0
    for group in groups.values():
        locksmith, was_created = Locksmith.objects.get_or_create(
            name=group["display"], defaults={"active": True, "email": group["email"]}
        )
        created_locksmiths += int(was_created)
        if not was_created and group["email"] and locksmith.email != group["email"]:
            locksmith.email = group["email"]
            locksmith.save(update_fields=["email"])
            emails_updated += 1
        for soter_id in group["ids"]:
            location = group.get("locations", {}).get(soter_id, "")
            obj, id_created = SoterLocksmithId.objects.get_or_create(
                locksmith=locksmith, soter_locksmith_id=soter_id,
                defaults={"location": location},
            )
            created_ids += int(id_created)
            # Backfills location on rows synced before this field existed.
            if not id_created and location and obj.location != location:
                obj.location = location
                obj.save(update_fields=["location"])
    return created_locksmiths, created_ids, emails_updated


def _normalize_person_name(name: str) -> str:
    """"WGTK - John Mason" / "John Mason" / "JohnMason" all normalize to
    "JOHN MASON" (roughly) so Optimo driver names/serials can be matched
    against Locksmith.name despite the different formatting each system
    uses."""
    name = name.strip()
    # Split "JohnMason"-style camel case (Optimo's driverSerial, used as
    # a name fallback) into separate words — must happen before
    # uppercasing, since that's what the lower-to-upper boundary this
    # regex looks for depends on.
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    name = name.upper()
    name = re.sub(r"^WGTK\s*-\s*", "", name)
    return _WHITESPACE_RE.sub(" ", name).strip()


def match_optimo_drivers(driver_infos: list) -> tuple[list[dict], list[dict]]:
    """Matches Optimo drivers (OptimoDriverInfo, from
    OptimoClient.list_recent_drivers()) to existing Locksmith records —
    by email first (driver_external_id vs Locksmith.email, the more
    reliable signal since it's an exact match), falling back to a
    normalized name comparison.

    Drivers that already have an OptimoDriverId row are skipped
    entirely (nothing to do). Returns (matches, unmatched):
    - matches: [{"driver": OptimoDriverInfo, "locksmith": Locksmith, "reason": "email"|"name"}]
    - unmatched: [{"driver": OptimoDriverInfo}]
    """
    already_mapped = set(
        OptimoDriverId.objects.values_list("optimo_driver_serial", flat=True)
    )
    email_map = {
        locksmith.email.strip().lower(): locksmith
        for locksmith in Locksmith.objects.exclude(email="")
    }
    name_map = {_normalize_person_name(locksmith.name): locksmith for locksmith in Locksmith.objects.all()}

    matches = []
    unmatched = []
    for driver in driver_infos:
        if driver.driver_serial in already_mapped:
            continue

        locksmith = None
        reason = None
        if driver.driver_external_id:
            locksmith = email_map.get(driver.driver_external_id.strip().lower())
            if locksmith:
                reason = "email"
        if not locksmith:
            key = _normalize_person_name(driver.driver_name or driver.driver_serial)
            locksmith = name_map.get(key)
            if locksmith:
                reason = "name"

        if locksmith:
            matches.append({"driver": driver, "locksmith": locksmith, "reason": reason})
        else:
            unmatched.append({"driver": driver})

    return matches, unmatched


def commit_optimo_driver_matches(matches: list[dict]) -> int:
    """Creates OptimoDriverId rows from match_optimo_drivers()'s output.
    Returns the number created."""
    created = 0
    for match in matches:
        _, was_created = OptimoDriverId.objects.get_or_create(
            locksmith=match["locksmith"],
            optimo_driver_serial=match["driver"].driver_serial,
        )
        created += int(was_created)
    return created
