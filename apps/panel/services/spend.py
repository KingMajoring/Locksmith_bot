"""Panel Spend report (Area 4): how much WGTK is spending with each
non-WGTK (panel/subcontractor) locksmith this month, and what margin
WGTK is making on those jobs (what the client was charged minus what
the panel locksmith quoted).

Live-queried from Handl on every page load — panel jobs never go
through Optimo (they're not WGTK's own locksmiths), so there's no
local pull/storage to build this from, unlike Area 2's CompletedJob.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from apps.integrations.handl import get_handl_client


@dataclass(frozen=True)
class PanelLocksmithSpend:
    locksmith_name: str
    job_count: int
    total_quoted_price: float | None
    total_net_cost: float | None
    margin: float | None  # total_net_cost - total_quoted_price


def mtd_spend_by_locksmith(today: date | None = None) -> list[PanelLocksmithSpend]:
    """One row per panel locksmith with a job logged so far this month
    (1st of the month through today inclusive), sorted by total quoted
    price descending — biggest spend first."""
    today = today or date.today()
    start = today.replace(day=1)
    end = today + timedelta(days=1)  # exclusive upper bound: through today
    jobs = get_handl_client().get_panel_jobs(start, end)

    by_locksmith: dict[str, list] = {}
    for job in jobs:
        by_locksmith.setdefault(job.locksmith_name, []).append(job)

    rows = []
    for name, locksmith_jobs in by_locksmith.items():
        quoted = [j.quoted_price for j in locksmith_jobs if j.quoted_price is not None]
        net = [j.net_cost for j in locksmith_jobs if j.net_cost is not None]
        total_quoted = round(sum(quoted), 2) if quoted else None
        total_net = round(sum(net), 2) if net else None
        margin = (
            round(total_net - total_quoted, 2)
            if total_net is not None and total_quoted is not None
            else None
        )
        rows.append(
            PanelLocksmithSpend(
                locksmith_name=name,
                job_count=len(locksmith_jobs),
                total_quoted_price=total_quoted,
                total_net_cost=total_net,
                margin=margin,
            )
        )
    return sorted(rows, key=lambda r: r.total_quoted_price or 0, reverse=True)
