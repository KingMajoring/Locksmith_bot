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

from django.db.models import Q

from .costing import parts_cost_for_jobs
from .labels import display_loss_type, is_gain_access_loss_type, raw_values_for_display_label
from .model_normalization import normalize_model
from ..models import CompletedJob


def _base_queryset():
    return (
        CompletedJob.objects.filter(status=CompletedJob.Status.SUCCESS)
        .exclude(make="")
        .exclude(model="")
    )


def _filter_by_service(queryset, service: str | None):
    """service is a display label from services/labels.py (e.g. "AKL",
    "Gain access") — filters to the raw loss_type value(s) it maps
    from. No-op when service is falsy."""
    if not service:
        return queryset
    raw_filter = Q()
    for value in raw_values_for_display_label(service):
        raw_filter |= Q(loss_type__iexact=value)
    return queryset.filter(raw_filter)


def available_services() -> list[str]:
    """Every distinct service (loss_type display label) present across
    all-time successful jobs, for the filter tags shown on each page."""
    raw_values = _base_queryset().exclude(loss_type="").values_list("loss_type", flat=True).distinct()
    return sorted({display_loss_type(v) for v in raw_values})


# Locksmiths sometimes start the job in Optimo and immediately end it
# again instead of starting it on arrival, leaving a near-zero duration
# that isn't a real "time to complete" — confirmed by the office as a
# workflow habit, not a genuinely fast job. Excluded from the timing
# average (but not from job_count/earning, which aren't affected by a
# bad clock). Gain access jobs are exempt: a lock-out can genuinely be
# opened in under 5 minutes, so a short duration there is real.
MIN_TIMED_DURATION_MINUTES = 5


def _timed_durations(jobs: list) -> list[int]:
    durations = []
    for job in jobs:
        duration = job.duration_minutes
        if duration is None:
            continue
        if duration < MIN_TIMED_DURATION_MINUTES and not is_gain_access_loss_type(job.loss_type):
            continue
        durations.append(duration)
    return durations


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
        "avg_duration_minutes": _avg(_timed_durations(jobs)),
    }


def makes_summary(service: str | None = None) -> list[dict]:
    """One row per make, across every successful job on file for it
    regardless of model/year. service optionally scopes this to one
    loss_type display label (see services/labels.py)."""
    jobs = list(_filter_by_service(_base_queryset(), service))
    parts_costs = parts_cost_for_jobs(jobs)

    by_make: dict[str, list] = {}
    for job in jobs:
        by_make.setdefault(job.make, []).append(job)

    rows = [{"make": make, **_summarize(make_jobs, parts_costs)} for make, make_jobs in by_make.items()]
    return sorted(rows, key=lambda r: r["make"])


def models_summary(make: str, service: str | None = None) -> list[dict]:
    """One row per normalized model family within a make."""
    jobs = list(_filter_by_service(_base_queryset().filter(make=make), service))
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


def years_summary(make: str, model_family: str, service: str | None = None) -> list[dict]:
    """One row per year of manufacture within a make + model family."""
    jobs = [
        job
        for job in _filter_by_service(_base_queryset().filter(make=make), service)
        if normalize_model(job.make, job.model) == model_family
    ]
    parts_costs = parts_cost_for_jobs(jobs)

    by_year: dict[str, list] = {}
    for job in jobs:
        by_year.setdefault(job.year or "Unknown", []).append(job)

    rows = [{"year": year, **_summarize(year_jobs, parts_costs)} for year, year_jobs in by_year.items()]
    return sorted(rows, key=lambda r: r["year"], reverse=True)
