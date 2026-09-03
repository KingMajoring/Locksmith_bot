"""Margin (avg earning/parts cost) and timing (avg on-site duration)
reports, drilled down by Make -> normalized model family -> Year.

Both are really the same underlying question ("what does a job on this
vehicle typically involve") looked at from two angles, so they share
one grouping/summarizing path — the views/templates just choose which
of the summary's fields to show. Scoped to successful jobs only (a
failed job's earning/duration figures are often partial or misleading)
across the full history on file (not a rolling window) — this is meant
as a stable reference, not a recent snapshot.
"""
from __future__ import annotations

from .costing import parts_cost_for_jobs
from .model_normalization import normalize_model
from ..models import CompletedJob


def _base_queryset():
    return (
        CompletedJob.objects.filter(status=CompletedJob.Status.SUCCESS)
        .exclude(make="")
        .exclude(model="")
    )


def _avg(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _summarize(jobs: list, parts_costs: dict[str, float]) -> dict:
    net_costs = [job.net_cost for job in jobs if job.net_cost is not None]
    margins = [
        job.net_cost - parts_costs.get(job.order_no, 0.0)
        for job in jobs
        if job.net_cost is not None
    ]
    avg_earning = _avg(net_costs)
    avg_margin = _avg(margins)
    return {
        "job_count": len(jobs),
        "avg_earning": avg_earning,
        "avg_cost": _avg([parts_costs.get(job.order_no, 0.0) for job in jobs]),
        "avg_margin": avg_margin,
        "avg_margin_pct": (
            round(avg_margin / avg_earning * 100, 1) if avg_earning else None
        ),
        "avg_duration_minutes": _avg([job.duration_minutes for job in jobs]),
    }


def makes_summary() -> list[dict]:
    """One row per make, across every successful job on file for it
    regardless of model/year."""
    jobs = list(_base_queryset())
    parts_costs = parts_cost_for_jobs(jobs)

    by_make: dict[str, list] = {}
    for job in jobs:
        by_make.setdefault(job.make, []).append(job)

    rows = [{"make": make, **_summarize(make_jobs, parts_costs)} for make, make_jobs in by_make.items()]
    return sorted(rows, key=lambda r: r["make"])


def models_summary(make: str) -> list[dict]:
    """One row per normalized model family within a make."""
    jobs = list(_base_queryset().filter(make=make))
    parts_costs = parts_cost_for_jobs(jobs)

    by_family: dict[str, list] = {}
    for job in jobs:
        family = normalize_model(job.make, job.model)
        by_family.setdefault(family, []).append(job)

    rows = [
        {"model_family": family, **_summarize(family_jobs, parts_costs)}
        for family, family_jobs in by_family.items()
    ]
    return sorted(rows, key=lambda r: r["model_family"])


def years_summary(make: str, model_family: str) -> list[dict]:
    """One row per year of manufacture within a make + model family."""
    jobs = [
        job
        for job in _base_queryset().filter(make=make)
        if normalize_model(job.make, job.model) == model_family
    ]
    parts_costs = parts_cost_for_jobs(jobs)

    by_year: dict[str, list] = {}
    for job in jobs:
        by_year.setdefault(job.year or "Unknown", []).append(job)

    rows = [{"year": year, **_summarize(year_jobs, parts_costs)} for year, year_jobs in by_year.items()]
    return sorted(rows, key=lambda r: r["year"], reverse=True)
