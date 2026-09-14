from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.integrations.handl import get_handl_client
from apps.integrations.optimo import get_optimo_client

from .models import Locksmith
from .services import (
    apply_soter_user_ids,
    commit_employee_location_matches,
    commit_groups,
    commit_optimo_driver_matches,
    group_locksmiths,
    match_employee_locations,
    match_optimo_drivers,
    parse_employee_location_rows,
)


def _preview_rows(groups):
    """Annotate each group with what would change: a new locksmith,
    added Soter ID(s), an updated email, or already up to date."""
    rows = []
    for base_upper, group in sorted(groups.items()):
        existing = Locksmith.objects.filter(name=group["display"]).first()
        changes = []
        if existing is None:
            changes.append("new")
        else:
            if not set(existing.soter_id_list) >= set(group["ids"]):
                changes.append("adds ID(s)")
            if group["email"] and existing.email != group["email"]:
                changes.append("updates email")
            if not changes:
                changes.append("up to date")
        rows.append(
            {
                "display": group["display"],
                "ids": group["ids"],
                "email": group["email"] or "(none)",
                "status": " + ".join(changes),
                "unusual_count": len(group["ids"]) not in (1, 2),
            }
        )
    return rows


@login_required
def sync_from_soter(request):
    extra_excludes = [
        s for s in request.POST.get("extra_excludes", request.GET.get("extra_excludes", "")).split(",")
        if s.strip()
    ]

    handl = get_handl_client()
    rows = handl.list_locksmiths()
    groups, stats = group_locksmiths(rows, extra_excludes, already_filtered=True)

    if request.method == "POST":
        created_locksmiths, created_ids, emails_updated = commit_groups(groups)
        user_ids_updated = apply_soter_user_ids(handl.list_locksmith_user_ids())
        messages.success(
            request,
            f"Synced from Soter: {created_locksmiths} new locksmith(s), "
            f"{created_ids} new Soter ID row(s), {emails_updated} email(s) updated, "
            f"{user_ids_updated} Soter user ID(s) updated.",
        )
        return redirect("admin:locksmiths_locksmith_changelist")

    return render(
        request,
        "locksmiths/sync_from_soter.html",
        {
            "preview_rows": _preview_rows(groups),
            "stats": stats,
            "extra_excludes": ", ".join(extra_excludes),
        },
    )


@login_required
def sync_from_optimo(request):
    optimo = get_optimo_client()
    driver_infos = optimo.list_recent_drivers()
    matches, unmatched = match_optimo_drivers(driver_infos)

    if request.method == "POST":
        created = commit_optimo_driver_matches(matches)
        messages.success(request, f"Synced from Optimo: {created} new driver mapping(s).")
        return redirect("admin:locksmiths_locksmith_changelist")

    return render(
        request,
        "locksmiths/sync_from_optimo.html",
        {"matches": matches, "unmatched": unmatched},
    )


_SESSION_KEY = "pending_employee_location_import"


@login_required
def sync_from_employee_locations(request):
    # Optimo has no live API for a driver's starting location (unlike
    # list_recent_drivers above), so this is a one-off file upload
    # rather than a live sync — a two-step preview/commit like the
    # pages above, but since the same file can't survive a redirect,
    # the parsed (already-matched) rows are stashed in the session
    # between the upload and the confirm click instead.
    if request.method == "POST" and request.POST.get("confirm"):
        pending = request.session.pop(_SESSION_KEY, None)
        if not pending:
            messages.error(request, "That import has expired — upload the file again.")
            return redirect("locksmiths:sync_from_employee_locations")
        locksmiths_by_id = Locksmith.objects.in_bulk([row["locksmith_id"] for row in pending])
        matches = [
            {"row": {"lat": row["lat"], "lng": row["lng"]}, "locksmith": locksmiths_by_id[row["locksmith_id"]]}
            for row in pending
            if row["locksmith_id"] in locksmiths_by_id
        ]
        updated = commit_employee_location_matches(matches)
        messages.success(request, f"Updated home location for {updated} locksmith(s).")
        return redirect("admin:locksmiths_locksmith_changelist")

    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "Choose a file to upload.")
            return redirect("locksmiths:sync_from_employee_locations")
        try:
            rows = parse_employee_location_rows(upload, upload.name)
        except Exception as exc:
            messages.error(request, f"Couldn't read that file: {exc}")
            return redirect("locksmiths:sync_from_employee_locations")

        matches, unmatched, skipped_no_coords = match_employee_locations(rows)
        request.session[_SESSION_KEY] = [
            {"locksmith_id": m["locksmith"].pk, "lat": m["row"]["lat"], "lng": m["row"]["lng"]}
            for m in matches
        ]
        return render(
            request,
            "locksmiths/sync_from_employee_locations.html",
            {
                "uploaded": True,
                "matches": matches,
                "unmatched": unmatched,
                "skipped_no_coords": skipped_no_coords,
            },
        )

    return render(request, "locksmiths/sync_from_employee_locations.html", {"uploaded": False})
