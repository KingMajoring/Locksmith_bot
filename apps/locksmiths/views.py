from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.integrations.handl import get_handl_client
from apps.integrations.optimo import get_optimo_client

from .models import Locksmith
from .services import (
    apply_soter_user_ids,
    commit_groups,
    commit_optimo_driver_matches,
    group_locksmiths,
    match_optimo_drivers,
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
