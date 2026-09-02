"""Which locksmith is failing which vehicle model family — surfaces
patterns a flat per-locksmith failure rate can't show (e.g. someone
struggling specifically with BMWs, or a particular van model).
"""
from __future__ import annotations

from datetime import date, timedelta

from ..models import CompletedJob
from .model_normalization import normalize_model

DEFAULT_WINDOW_DAYS = 90


def locksmith_model_failure_breakdown(window_days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    since = date.today() - timedelta(days=window_days)
    jobs = (
        CompletedJob.objects.filter(job_date__gte=since, locksmith__isnull=False)
        .exclude(model="")
        .select_related("locksmith")
    )

    # Keyed by (locksmith, make, family) rather than family alone —
    # different manufacturers could coincidentally share a first-word
    # model name, and make/family are already bound together for the
    # BMW/Mercedes special cases in normalize_model().
    stats: dict[tuple, dict] = {}
    for job in jobs:
        family = normalize_model(job.make, job.model)
        key = (job.locksmith_id, job.make, family)
        entry = stats.setdefault(
            key,
            {
                "locksmith": job.locksmith,
                "make": job.make,
                "model_family": family,
                "total": 0,
                "failed": 0,
            },
        )
        entry["total"] += 1
        if job.status == CompletedJob.Status.FAILED:
            entry["failed"] += 1

    results = []
    for entry in stats.values():
        if entry["failed"] == 0:
            continue
        entry["failure_rate_pct"] = round(entry["failed"] / entry["total"] * 100, 1)
        results.append(entry)

    return sorted(results, key=lambda e: (e["failed"], e["failure_rate_pct"]), reverse=True)
