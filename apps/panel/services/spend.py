"""Panel Spend report (Area 4): how much WGTK is spending with each
non-WGTK (panel/subcontractor) locksmith this month.

Live-queried from Handl on every page load — panel jobs never go
through Optimo (they're not WGTK's own locksmiths), so there's no
local pull/storage to build this from, unlike Area 2's CompletedJob.

Built on Tableau_PanelFigures (see HandlClient.get_panel_daily_figures)
rather than reconstructed from the raw claim tables — Policy_Financial
only gets a row once the office invoices a job, days behind it being
logged, which made a naive join show wildly wrong (often deeply
negative) margins for anything logged in the last few days.
Tableau_PanelFigures already carries WGTK's fee without that lag.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from apps.integrations.handl import get_handl_client


@dataclass(frozen=True)
class PanelLocksmithSpend:
    locksmith_name: str
    job_count: int
    total_quoted_price: float | None  # what WGTK pays the panel locksmith
    selling_cost: float | None  # quoted price + WGTK's fee: what the client was charged


def mtd_spend_by_locksmith(today: date | None = None) -> list[PanelLocksmithSpend]:
    """One row per panel locksmith with a job logged so far this month
    (1st of the month through today inclusive), sorted by total quoted
    price descending — biggest spend first."""
    today = today or date.today()
    start = today.replace(day=1)
    end = today + timedelta(days=1)  # exclusive upper bound: through today
    figures = get_handl_client().get_panel_daily_figures(start, end)

    by_locksmith: dict[str, list] = {}
    for figure in figures:
        by_locksmith.setdefault(figure.panel_name, []).append(figure)

    rows = []
    for name, daily_figures in by_locksmith.items():
        job_count = sum(f.job_count for f in daily_figures)
        # Quoted price and selling cost are only derivable for a day
        # where both net_cost and wgtk_fee are present — summing
        # net_cost alone while wgtk_fee (or vice versa) is missing for
        # a different subset of days would silently compare mismatched
        # sets of days, the same trap that produced bogus margins before.
        quoted_values = []
        selling_values = []
        for f in daily_figures:
            if f.net_cost is None or f.wgtk_fee is None:
                continue
            quoted = f.net_cost - f.wgtk_fee
            quoted_values.append(quoted)
            selling_values.append(quoted + f.wgtk_fee)
        rows.append(
            PanelLocksmithSpend(
                locksmith_name=name,
                job_count=job_count,
                total_quoted_price=round(sum(quoted_values), 2) if quoted_values else None,
                selling_cost=round(sum(selling_values), 2) if selling_values else None,
            )
        )
    return sorted(rows, key=lambda r: r.total_quoted_price or 0, reverse=True)
