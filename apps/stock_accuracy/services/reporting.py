"""Variance / leakage reporting: per-locksmith and per-line trends."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.locksmiths.models import Locksmith

from ..models import StockCheckItem, VarianceThreshold


def flagged_items_queryset(weeks: int | None = None):
    qs = StockCheckItem.objects.filter(actual_qty__isnull=False).select_related(
        "weekly_check", "weekly_check__locksmith"
    )
    if weeks is not None:
        cutoff = timezone.now().date() - timedelta(weeks=weeks)
        qs = qs.filter(weekly_check__week_starting__gte=cutoff)
    thresholds = VarianceThreshold.current()
    return [item for item in qs if item.is_flagged(thresholds)]


def locksmith_summary(locksmith: Locksmith, weeks: int = 12) -> dict:
    thresholds = VarianceThreshold.current()
    cutoff = timezone.now().date() - timedelta(weeks=weeks)
    items = StockCheckItem.objects.filter(
        weekly_check__locksmith=locksmith,
        weekly_check__week_starting__gte=cutoff,
        actual_qty__isnull=False,
    ).select_related("weekly_check")

    flagged = [i for i in items if i.is_flagged(thresholds)]
    total_value_impact = sum((i.value_impact or 0) for i in items)

    return {
        "locksmith": locksmith,
        "lines_checked": items.count(),
        "lines_flagged": len(flagged),
        "total_value_impact": round(total_value_impact, 2),
        "is_repeat_offender": _is_repeat_offender(locksmith, thresholds),
    }


def _is_repeat_offender(locksmith: Locksmith, thresholds: VarianceThreshold) -> bool:
    cutoff = timezone.now().date() - timedelta(weeks=thresholds.repeat_offender_window_weeks)
    weekly_checks = locksmith.stock_checks.filter(week_starting__gte=cutoff)
    flagged_weeks = 0
    for wc in weekly_checks:
        if any(item.is_flagged(thresholds) for item in wc.items.filter(actual_qty__isnull=False)):
            flagged_weeks += 1
    return flagged_weeks >= thresholds.repeat_offender_occurrences


def line_summary(weeks: int = 12) -> list[dict]:
    """Which part lines are flagged most often across all locksmiths."""
    thresholds = VarianceThreshold.current()
    cutoff = timezone.now().date() - timedelta(weeks=weeks)
    items = StockCheckItem.objects.filter(
        weekly_check__week_starting__gte=cutoff, actual_qty__isnull=False
    )

    by_line: dict[str, dict] = {}
    for item in items:
        entry = by_line.setdefault(
            item.part_code,
            {"part_code": item.part_code, "part_name": item.part_name, "checked": 0, "flagged": 0},
        )
        entry["checked"] += 1
        if item.is_flagged(thresholds):
            entry["flagged"] += 1

    return sorted(by_line.values(), key=lambda e: e["flagged"], reverse=True)
