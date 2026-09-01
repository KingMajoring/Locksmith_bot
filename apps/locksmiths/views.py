from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.integrations.handl import get_handl_client

from .models import Locksmith
from .services import commit_groups, group_locksmiths


def _preview_rows(groups):
    """Annotate each group with whether it's new, would add IDs to an
    existing locksmith, or is already fully up to date."""
    rows = []
    for base_upper, group in sorted(groups.items()):
        existing = Locksmith.objects.filter(name=group["display"]).first()
        if existing is None:
            status = "new"
        elif set(existing.soter_id_list) >= set(group["ids"]):
            status = "up to date"
        else:
            status = "adds ID(s)"
        rows.append(
            {
                "display": group["display"],
                "ids": group["ids"],
                "status": status,
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
    groups, stats = group_locksmiths(rows, extra_excludes)

    if request.method == "POST":
        created_locksmiths, created_ids = commit_groups(groups)
        messages.success(
            request,
            f"Synced from Soter: {created_locksmiths} new locksmith(s), "
            f"{created_ids} new Soter ID row(s) added.",
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
