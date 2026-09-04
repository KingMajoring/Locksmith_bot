"""Jobs completed on a given day, with a rolling pill selector — the
last RECENT_DAYS days by default, paging PAGE_DAYS further into the
past each time "show earlier days" is clicked, rather than one huge
flat job history.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..models import CompletedJob
from .labels import is_gain_access_loss_type

RECENT_DAYS = 7
PAGE_DAYS = 10

# Worth a second look on the By Day table: a completed job with no
# parts disposed, or one finished suspiciously fast for the work it
# claims. Gain access (lock-out/break-in) jobs are exempt from both —
# genuinely opening a lock can use no parts and take well under a
# minute, unlike most other services. Only meaningful for jobs that
# actually finished, so failed jobs are never flagged.
MIN_MINUTES_GAIN_ACCESS = 3
MIN_MINUTES_OTHER = 15


def review_flags(job) -> list[str]:
    if job.status != CompletedJob.Status.SUCCESS:
        return []

    gain_access = is_gain_access_loss_type(job.loss_type)
    flags = []

    if not job.disposed_skus and not gain_access:
        flags.append("No parts disposed")

    duration = job.duration_minutes
    if duration is not None:
        threshold = MIN_MINUTES_GAIN_ACCESS if gain_access else MIN_MINUTES_OTHER
        if duration < threshold:
            flags.append(f"Completed in {duration} min")

    return flags


def day_pills(offset: int = 0) -> list[date]:
    """Dates to show as pill buttons for a given paging offset.
    offset=0 is the most recent RECENT_DAYS days (today back to
    today - (RECENT_DAYS - 1)); offset>0 is a PAGE_DAYS-wide window
    further into the past, starting `offset` days back."""
    today = date.today()
    count = RECENT_DAYS if offset == 0 else PAGE_DAYS
    return [today - timedelta(days=offset + i) for i in range(count)]


def next_offset(offset: int) -> int:
    return RECENT_DAYS if offset == 0 else offset + PAGE_DAYS


def prev_offset(offset: int) -> int:
    """Paging backwards towards today — the inverse of next_offset."""
    if offset <= RECENT_DAYS:
        return 0
    return offset - PAGE_DAYS


def jobs_for_day(for_date: date):
    return (
        CompletedJob.objects.filter(job_date=for_date)
        .select_related("locksmith", "failure_category")
        .order_by("locksmith__name", "order_no")
    )


def summarize_day(jobs: list) -> dict:
    """Totals for a day's jobs, for the summary box next to the table.
    Expects `parts_cost`/`margin` already annotated onto each job (see
    views.jobs_by_day) — this only sums what's present, so a job
    missing a Handl figure just doesn't contribute to that total rather
    than breaking the whole sum.
    """
    total_miles = sum(j.distance_miles for j in jobs if j.distance_miles is not None)
    total_income = sum(j.net_cost for j in jobs if j.net_cost is not None)
    total_cost = sum(j.parts_cost for j in jobs if getattr(j, "parts_cost", None) is not None)
    total_margin = sum(j.margin for j in jobs if getattr(j, "margin", None) is not None)
    locksmith_count = len({j.locksmith_id for j in jobs if j.locksmith_id})

    return {
        "total_miles": round(total_miles, 1),
        "total_income": round(total_income, 2),
        "total_cost": round(total_cost, 2),
        "total_margin": round(total_margin, 2),
        "job_count": len(jobs),
        "locksmith_count": locksmith_count,
    }
