"""Failure-rate reporting: per-locksmith and category breakdowns."""
from __future__ import annotations

from datetime import date, timedelta

from apps.locksmiths.models import Locksmith

from ..models import CompletedJob

DEFAULT_WINDOW_DAYS = 90


def needs_categorization_queryset():
    return (
        CompletedJob.objects.filter(
            status=CompletedJob.Status.FAILED, failure_category__isnull=True
        )
        .select_related("locksmith")
        .order_by("-job_date")
    )


def locksmith_summary(locksmith: Locksmith, window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    since = date.today() - timedelta(days=window_days)
    jobs = CompletedJob.objects.filter(locksmith=locksmith, job_date__gte=since)
    total = jobs.count()
    failed = jobs.filter(status=CompletedJob.Status.FAILED).count()
    failure_rate = round(failed / total * 100, 1) if total else 0.0

    return {
        "locksmith": locksmith,
        "total_jobs": total,
        "failed_jobs": failed,
        "failure_rate_pct": failure_rate,
    }


def all_locksmith_summaries(window_days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    return [
        locksmith_summary(locksmith, window_days)
        for locksmith in Locksmith.objects.filter(active=True)
    ]


def failure_category_breakdown(window_days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    since = date.today() - timedelta(days=window_days)
    failed = CompletedJob.objects.filter(
        status=CompletedJob.Status.FAILED, job_date__gte=since
    ).select_related("failure_category")

    by_category: dict[str, int] = {}
    for job in failed:
        label = job.failure_category.name if job.failure_category else "Uncategorized"
        by_category[label] = by_category.get(label, 0) + 1

    return sorted(
        ({"category": k, "count": v} for k, v in by_category.items()),
        key=lambda e: e["count"],
        reverse=True,
    )


def service_types_for_locksmith(locksmith: Locksmith, window_days: int = DEFAULT_WINDOW_DAYS) -> list[str]:
    since = date.today() - timedelta(days=window_days)
    return sorted(
        CompletedJob.objects.filter(locksmith=locksmith, job_date__gte=since)
        .exclude(service_type="")
        .values_list("service_type", flat=True)
        .distinct()
    )
