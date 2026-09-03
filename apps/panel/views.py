from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .services.spend import panel_spend_for_month


@login_required
def spend(request):
    try:
        months_ago = max(int(request.GET.get("months_ago", 0)), 0)
    except (TypeError, ValueError):
        months_ago = 0

    rows, totals, month_start = panel_spend_for_month(months_ago)
    return render(
        request,
        "panel/spend.html",
        {
            "rows": rows,
            "totals": totals,
            "month_start": month_start,
            "prev_months_ago": months_ago + 1,
            "next_months_ago": months_ago - 1 if months_ago > 0 else None,
        },
    )
