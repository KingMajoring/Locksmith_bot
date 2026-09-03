from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .services.spend import panel_spend_for_month, panel_spend_year_to_date


@login_required
def spend(request):
    if request.GET.get("view") == "ytd":
        rows, totals, period_start = panel_spend_year_to_date()
        return render(
            request,
            "panel/spend.html",
            {
                "rows": rows,
                "totals": totals,
                "period_label": f"Year to date ({period_start.year})",
                "is_ytd": True,
            },
        )

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
            "period_label": month_start.strftime("%B %Y"),
            "is_ytd": False,
            "prev_months_ago": months_ago + 1,
            "next_months_ago": months_ago - 1 if months_ago > 0 else None,
        },
    )
