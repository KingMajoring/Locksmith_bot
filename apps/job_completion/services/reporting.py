"""Failure-rate reporting: per-locksmith and category breakdowns."""
from __future__ import annotations

from datetime import date, timedelta

from apps.locksmiths.models import Locksmith

from ..models import CompletedJob, FailureCategory

DEFAULT_WINDOW_DAYS = 90


def needs_categorization_queryset():
    # Excludes jobs with no matched locksmith (driver not yet mapped via
    # OptimoDriverId) — nothing useful to do with those here until the
    # mapping exists, so they'd just be noise in the queue.
    return (
        CompletedJob.objects.filter(
            status=CompletedJob.Status.FAILED,
            failure_category__isnull=True,
            locksmith__isnull=False,
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


def master_reason_breakdown(window_days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    """Failure counts grouped by FailureCategory.master_reason — who or
    what was actually at fault (WGTK Office, Client, Supplier, WGTK
    Locksmith, or None) — to help spot where training is needed, rather
    than just which specific category comes up most."""
    since = date.today() - timedelta(days=window_days)
    failed = CompletedJob.objects.filter(
        status=CompletedJob.Status.FAILED, job_date__gte=since
    ).select_related("failure_category")

    labels = dict(FailureCategory.MasterReason.choices)
    by_reason: dict[str, int] = {}
    for job in failed:
        if job.failure_category:
            key = job.failure_category.master_reason
        else:
            key = "uncategorized"
        by_reason[key] = by_reason.get(key, 0) + 1

    return sorted(
        (
            {"master_reason": labels.get(k, "Uncategorized"), "count": v}
            for k, v in by_reason.items()
        ),
        key=lambda e: e["count"],
        reverse=True,
    )


def service_types_for_locksmith(locksmith: Locksmith, window_days: int = DEFAULT_WINDOW_DAYS) -> list[str]:
    since = date.today() - timedelta(days=window_days)
    return sorted(
        CompletedJob.objects.filter(locksmith=locksmith, job_date__gte=since)
        .exclude(service_type="")
        # CompletedJob's default ordering (job_date, order_no) gets
        # pulled into the query even with values_list()+distinct(),
        # which makes DISTINCT operate on (service_type, job_date,
        # order_no) instead of service_type alone — order_by() with no
        # args clears it so this actually dedupes on service_type.
        .order_by()
        .values_list("service_type", flat=True)
        .distinct()
    )
