"""Panel Spend report (Area 4): how much WGTK is spending with each
non-WGTK (panel/subcontractor) locksmith — one calendar month at a
time, or year to date.

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

from apps.integrations.handl import PanelDailyFigures, get_handl_client


@dataclass(frozen=True)
class PanelLocksmithSpend:
    locksmith_name: str
    job_count: int
    total_quoted_price: float | None  # what WGTK pays the panel locksmith
    selling_cost: float | None  # quoted price + WGTK's fee: what the client was charged


@dataclass(frozen=True)
class PanelSpendTotals:
    job_count: int
    total_quoted_price: float | None
    selling_cost: float | None


def month_bounds(months_ago: int = 0, today: date | None = None) -> tuple[date, date]:
    """[start, end) for the calendar month `months_ago` months before
    today's month — 0 is the current month, 1 is last month, and so on."""
    today = today or date.today()
    year, month = today.year, today.month
    for _ in range(months_ago):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _aggregate_by_locksmith(
    figures: list[PanelDailyFigures],
) -> tuple[list[PanelLocksmithSpend], PanelSpendTotals]:
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
    rows.sort(key=lambda r: r.total_quoted_price or 0, reverse=True)

    quoted_totals = [r.total_quoted_price for r in rows if r.total_quoted_price is not None]
    selling_totals = [r.selling_cost for r in rows if r.selling_cost is not None]
    totals = PanelSpendTotals(
        job_count=sum(r.job_count for r in rows),
        total_quoted_price=round(sum(quoted_totals), 2) if quoted_totals else None,
        selling_cost=round(sum(selling_totals), 2) if selling_totals else None,
    )
    return rows, totals


def panel_spend_for_month(
    months_ago: int = 0, today: date | None = None
) -> tuple[list[PanelLocksmithSpend], PanelSpendTotals, date]:
    """Per-locksmith rows (sorted by total quoted price descending —
    biggest spend first) and the month's totals, for the given month
    (months_ago=0 is the current month, still in progress; no separate
    "month to date" cap is needed since a day that hasn't happened yet
    simply has no Tableau_PanelFigures row). Also returns the month's
    start date, for the page heading."""
    start, end = month_bounds(months_ago, today)
    figures = get_handl_client().get_panel_daily_figures(start, end)
    rows, totals = _aggregate_by_locksmith(figures)
    return rows, totals, start


def panel_spend_year_to_date(
    today: date | None = None,
) -> tuple[list[PanelLocksmithSpend], PanelSpendTotals, date]:
    """Same shape as panel_spend_for_month, but 1 January through today
    of the current year. Also returns 1 January, for the page heading."""
    today = today or date.today()
    start = date(today.year, 1, 1)
    end = today + timedelta(days=1)
    figures = get_handl_client().get_panel_daily_figures(start, end)
    rows, totals = _aggregate_by_locksmith(figures)
    return rows, totals, start
