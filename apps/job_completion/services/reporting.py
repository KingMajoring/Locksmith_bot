"""Failure-rate reporting: per-locksmith and category breakdowns."""
from __future__ import annotations

from datetime import date, timedelta

from apps.locksmiths.models import Locksmith

from ..models import CompletedJob, FailureCategory
from .labels import display_loss_type

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
    # Failures specifically blamed on the locksmith — a subset of
    # `failed` (some failures are Office/Client/Supplier's fault, or
    # still sitting uncategorized in the Job Failures queue), so this
    # is always <= failure_rate_pct, never a second independent measure.
    wgtk_fault = jobs.filter(
        status=CompletedJob.Status.FAILED,
        failure_category__master_reason=FailureCategory.MasterReason.WGTK_LOCKSMITH,
    ).count()
    wgtk_fault_rate = round(wgtk_fault / total * 100, 1) if total else 0.0

    # QC: of the jobs office has actually assessed for note quality
    # (notes_sufficient set, alongside categorizing a failure — see
    # categorize_jobs), what fraction were judged not good enough.
    # None (not 0.0) when nothing's been assessed yet, so the dashboard
    # can tell "0% insufficient" apart from "no QC data yet".
    notes_assessed = jobs.filter(notes_sufficient__isnull=False).count()
    notes_insufficient = jobs.filter(notes_sufficient=False).count()
    insufficient_notes_rate = (
        round(notes_insufficient / notes_assessed * 100, 1) if notes_assessed else None
    )

    return {
        "locksmith": locksmith,
        "total_jobs": total,
        "failed_jobs": failed,
        "failure_rate_pct": failure_rate,
        "wgtk_fault_jobs": wgtk_fault,
        "wgtk_fault_rate_pct": wgtk_fault_rate,
        "notes_assessed": notes_assessed,
        "insufficient_notes_rate_pct": insufficient_notes_rate,
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

    # category_id is None for "Uncategorized" — the drill-down view
    # (failed_jobs_list) treats a missing category param as "uncategorized
    # only", same convention failure_category__isnull=True uses elsewhere.
    by_category: dict[str, list] = {}
    for job in failed:
        if job.failure_category:
            label, category_id = job.failure_category.name, job.failure_category_id
        else:
            label, category_id = "Uncategorized", None
        entry = by_category.setdefault(label, [category_id, 0])
        entry[1] += 1

    return sorted(
        (
            {"category": label, "category_id": category_id, "count": count}
            for label, (category_id, count) in by_category.items()
        ),
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
            {
                "master_reason": labels.get(k, "Uncategorized"),
                "master_reason_key": k,
                "count": v,
            }
            for k, v in by_reason.items()
        ),
        key=lambda e: e["count"],
        reverse=True,
    )


def failed_jobs_list(
    window_days: int = DEFAULT_WINDOW_DAYS,
    *,
    category_id: int | None = None,
    uncategorized: bool = False,
    master_reason: str | None = None,
    locksmith_id: int | None = None,
):
    """Failed jobs matching any combination of the Job Failures/Locksmith
    Report drill-downs — same 90-day window as the breakdowns above, so
    the counts you click through from match what you land on.
    category_id/uncategorized are mutually exclusive (uncategorized wins
    if both are somehow passed); master_reason="uncategorized" means the
    same as uncategorized=True, matching master_reason_breakdown's key."""
    since = date.today() - timedelta(days=window_days)
    jobs = CompletedJob.objects.filter(
        status=CompletedJob.Status.FAILED, job_date__gte=since
    ).select_related("locksmith", "failure_category")

    if uncategorized or master_reason == "uncategorized":
        jobs = jobs.filter(failure_category__isnull=True)
    elif category_id is not None:
        jobs = jobs.filter(failure_category_id=category_id)
    elif master_reason:
        jobs = jobs.filter(failure_category__master_reason=master_reason)

    if locksmith_id is not None:
        jobs = jobs.filter(locksmith_id=locksmith_id)

    return jobs.order_by("-job_date")


def loss_types_for_locksmith(locksmith: Locksmith, window_days: int = DEFAULT_WINDOW_DAYS) -> list[str]:
    """Distinct loss_type display labels (see services/labels.py) this
    locksmith has jobs for — the benchmark grouping used on their
    report, since loss_type ("AKL", "Gain access", "Lockout", ...) is
    the meaningful classification, unlike service_type/KeyType which
    is almost always just "Car"."""
    since = date.today() - timedelta(days=window_days)
    raw_values = (
        CompletedJob.objects.filter(locksmith=locksmith, job_date__gte=since)
        .exclude(loss_type="")
        # CompletedJob's default ordering (job_date, order_no) gets
        # pulled into the query even with values_list()+distinct(),
        # which makes DISTINCT operate on (loss_type, job_date,
        # order_no) instead of loss_type alone — order_by() with no
        # args clears it so this actually dedupes on loss_type.
        .order_by()
        .values_list("loss_type", flat=True)
        .distinct()
    )
    return sorted({display_loss_type(v) for v in raw_values})
