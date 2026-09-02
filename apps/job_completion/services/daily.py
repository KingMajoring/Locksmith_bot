"""Jobs completed on a given day, with a rolling pill selector — the
last RECENT_DAYS days by default, paging PAGE_DAYS further into the
past each time "show earlier days" is clicked, rather than one huge
flat job history.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..models import CompletedJob

RECENT_DAYS = 7
PAGE_DAYS = 10


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
