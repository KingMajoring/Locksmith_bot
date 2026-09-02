"""Duration benchmarking: SLA target vs company average vs a locksmith's
own average, for a given loss type.

Averages are built from successfully completed jobs only — a failed
job's duration doesn't represent genuine service time (e.g. cut short
because the customer wasn't there), so including it would distort what
"normal" looks like for that loss type.

Grouped by loss_type (e.g. "AKL", "Gain access", "Lockout") rather than
service_type, since service_type is Handl's KeyType and is almost
always just "Car" — not a meaningful grouping for duration benchmarks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Q

from apps.locksmiths.models import Locksmith

from ..models import CompletedJob, SLATarget
from .labels import raw_values_for_display_label

DEFAULT_WINDOW_DAYS = 90


@dataclass(frozen=True)
class DurationBenchmark:
    loss_type: str
    sla_target_minutes: int | None
    company_avg_minutes: float | None
    locksmith_avg_minutes: float | None
    company_sample_size: int
    locksmith_sample_size: int


def _average(values: list[int]) -> float | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def duration_benchmark(
    locksmith: Locksmith, loss_type: str, window_days: int = DEFAULT_WINDOW_DAYS
) -> DurationBenchmark:
    since = date.today() - timedelta(days=window_days)

    raw_values = raw_values_for_display_label(loss_type)
    raw_filter = Q()
    for value in raw_values:
        raw_filter |= Q(loss_type__iexact=value)

    successful = CompletedJob.objects.filter(
        raw_filter,
        status=CompletedJob.Status.SUCCESS,
        job_date__gte=since,
        start_time__isnull=False,
        end_time__isnull=False,
    )

    company_durations = [job.duration_minutes for job in successful]
    locksmith_durations = [
        job.duration_minutes for job in successful.filter(locksmith=locksmith)
    ]

    sla = SLATarget.objects.filter(loss_type=loss_type, active=True).first()

    return DurationBenchmark(
        loss_type=loss_type,
        sla_target_minutes=sla.target_minutes if sla else None,
        company_avg_minutes=_average(company_durations),
        locksmith_avg_minutes=_average(locksmith_durations),
        company_sample_size=len(company_durations),
        locksmith_sample_size=len(locksmith_durations),
    )
