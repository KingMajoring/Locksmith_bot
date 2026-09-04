"""Month-by-month failure trends — is the failure rate improving or
getting worse over time, sliced three ways:

- Overall: total jobs / failures / failure rate per month.
- By locksmith, restricted to failures the locksmith themselves caused
  (FailureCategory.master_reason == WGTK_LOCKSMITH) — client/supplier/
  office-caused failures would just be noise on an individual
  performance trend.
- By vehicle make/model family (same normalize_model grouping as
  services/model_analysis.py), so a model getting worse over time
  stands out rather than being buried in a single last-90-days total.
"""
from __future__ import annotations

from datetime import date

from ..models import CompletedJob, FailureCategory
from .model_normalization import normalize_model

DEFAULT_MONTHS = 12


def _month_key(d: date) -> date:
    return date(d.year, d.month, 1)


def _month_label(d: date) -> str:
    return d.strftime("%b %Y")


def _month_starts(months: int, today: date | None = None) -> list[date]:
    """The first-of-month date for each of the last `months` months,
    oldest first, ending with the current (partial) month."""
    today = today or date.today()
    year, month = today.year, today.month
    starts = []
    for _ in range(months):
        starts.append(date(year, month, 1))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(starts))


def monthly_failure_trend(months: int = DEFAULT_MONTHS) -> list[dict]:
    """Total jobs / failed jobs / failure rate for each of the last
    `months` calendar months, oldest first."""
    starts = _month_starts(months)
    totals = {start: 0 for start in starts}
    failed = {start: 0 for start in starts}

    jobs = CompletedJob.objects.filter(job_date__gte=starts[0]).only("job_date", "status")
    for job in jobs:
        key = _month_key(job.job_date)
        if key not in totals:
            continue
        totals[key] += 1
        if job.status == CompletedJob.Status.FAILED:
            failed[key] += 1

    rows = []
    for start in starts:
        total = totals[start]
        f = failed[start]
        rows.append(
            {
                "label": _month_label(start),
                "total": total,
                "failed": f,
                "failure_rate_pct": round(f / total * 100, 1) if total else 0.0,
            }
        )
    return rows


def locksmith_wgtk_fault_trend(months: int = DEFAULT_MONTHS) -> dict:
    """Month-by-month count of failures attributed to the locksmith
    themselves, per locksmith — the trend that actually reflects
    individual performance/training need, isolated from failures
    caused by the client, a supplier, or WGTK's own office."""
    starts = _month_starts(months)
    jobs = (
        CompletedJob.objects.filter(
            job_date__gte=starts[0],
            status=CompletedJob.Status.FAILED,
            failure_category__master_reason=FailureCategory.MasterReason.WGTK_LOCKSMITH,
            locksmith__isnull=False,
        )
        .select_related("locksmith")
        .only("job_date", "locksmith_id", "locksmith__name")
    )

    per_locksmith: dict[int, dict[date, int]] = {}
    names: dict[int, str] = {}
    for job in jobs:
        key = _month_key(job.job_date)
        if key not in starts:
            continue
        counts = per_locksmith.setdefault(job.locksmith_id, {})
        counts[key] = counts.get(key, 0) + 1
        names[job.locksmith_id] = job.locksmith.name

    rows = []
    for locksmith_id, counts in per_locksmith.items():
        month_counts = [counts.get(start, 0) for start in starts]
        rows.append(
            {
                "locksmith_name": names[locksmith_id],
                "month_counts": month_counts,
                "total": sum(month_counts),
            }
        )
    rows.sort(key=lambda r: r["total"], reverse=True)

    return {"months": [_month_label(s) for s in starts], "rows": rows}


def make_model_failure_trend(months: int = DEFAULT_MONTHS) -> dict:
    """Month-by-month failure counts per vehicle make/model family."""
    starts = _month_starts(months)
    jobs = (
        CompletedJob.objects.filter(job_date__gte=starts[0], status=CompletedJob.Status.FAILED)
        .exclude(model="")
        .only("job_date", "make", "model")
    )

    per_family: dict[tuple[str, str], dict[date, int]] = {}
    for job in jobs:
        key = _month_key(job.job_date)
        if key not in starts:
            continue
        family = normalize_model(job.make, job.model)
        counts = per_family.setdefault((job.make, family), {})
        counts[key] = counts.get(key, 0) + 1

    rows = []
    for (make, family), counts in per_family.items():
        month_counts = [counts.get(start, 0) for start in starts]
        rows.append(
            {
                "make": make,
                "model_family": family,
                "month_counts": month_counts,
                "total": sum(month_counts),
            }
        )
    rows.sort(key=lambda r: r["total"], reverse=True)

    return {"months": [_month_label(s) for s in starts], "rows": rows}
