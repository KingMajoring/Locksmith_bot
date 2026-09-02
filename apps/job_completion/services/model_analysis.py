"""Which locksmith is failing which vehicle model family — surfaces
patterns a flat per-locksmith failure rate can't show (e.g. someone
struggling specifically with BMWs, or a particular van model). Also
available company-wide (grouped by model family only, ignoring who did
the job) to see which vehicles are inherently the harder ones. Each
row's failures are further broken down by master reason, so it's clear
whether a bad rate is down to the locksmith, the client, a supplier, or
WGTK's own office process.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..models import CompletedJob, FailureCategory
from .model_normalization import normalize_model

DEFAULT_WINDOW_DAYS = 90


def _master_reason_breakdown(reason_counts: dict[str, int]) -> list[dict]:
    labels = dict(FailureCategory.MasterReason.choices)
    total_failed = sum(reason_counts.values())
    if not total_failed:
        return []
    breakdown = [
        {
            "label": labels.get(key, "Uncategorized"),
            "count": count,
            "pct": round(count / total_failed * 100, 1),
        }
        for key, count in reason_counts.items()
    ]
    return sorted(breakdown, key=lambda e: e["count"], reverse=True)


def _model_failure_stats(jobs, group_by_locksmith: bool) -> list[dict]:
    # Keyed by (locksmith, make, family) — or just (make, family) for
    # the company-wide view — rather than family alone: different
    # manufacturers could coincidentally share a first-word model name,
    # and make/family are already bound together for the BMW/Mercedes
    # special cases in normalize_model().
    stats: dict[tuple, dict] = {}
    for job in jobs:
        family = normalize_model(job.make, job.model)
        key = (job.locksmith_id, job.make, family) if group_by_locksmith else (job.make, family)
        entry = stats.setdefault(
            key,
            {
                **({"locksmith": job.locksmith} if group_by_locksmith else {}),
                "make": job.make,
                "model_family": family,
                "total": 0,
                "failed": 0,
                "_reason_counts": {},
            },
        )
        entry["total"] += 1
        if job.status == CompletedJob.Status.FAILED:
            entry["failed"] += 1
            reason_key = job.failure_category.master_reason if job.failure_category else "uncategorized"
            entry["_reason_counts"][reason_key] = entry["_reason_counts"].get(reason_key, 0) + 1

    results = []
    for entry in stats.values():
        if entry["failed"] == 0:
            continue
        entry["failure_rate_pct"] = round(entry["failed"] / entry["total"] * 100, 1)
        entry["master_reasons"] = _master_reason_breakdown(entry.pop("_reason_counts"))
        results.append(entry)

    return sorted(results, key=lambda e: (e["failed"], e["failure_rate_pct"]), reverse=True)


def locksmith_model_failure_breakdown(window_days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    since = date.today() - timedelta(days=window_days)
    jobs = (
        CompletedJob.objects.filter(job_date__gte=since, locksmith__isnull=False)
        .exclude(model="")
        .select_related("locksmith", "failure_category")
    )
    return _model_failure_stats(jobs, group_by_locksmith=True)


def company_model_failure_breakdown(window_days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    """Same breakdown as locksmith_model_failure_breakdown, but
    aggregated across every locksmith — which vehicle models fail most
    company-wide, regardless of who did the job."""
    since = date.today() - timedelta(days=window_days)
    jobs = (
        CompletedJob.objects.filter(job_date__gte=since)
        .exclude(model="")
        .select_related("failure_category")
    )
    return _model_failure_stats(jobs, group_by_locksmith=False)
