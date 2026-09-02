"""Parts cost for completed jobs — looked up from Handl via the same
unit-cost basis as Area 1's stock reports (Inventory_Stock's
PartValue/Quantity for the most recently priced batch), summed across
every SKU disposed on a job.

Selling price / margin isn't included here yet — that needs Handl's
invoiced/charged amount per job, which hasn't been confirmed against
the real schema.
"""
from __future__ import annotations

from apps.integrations.handl import get_handl_client


def _skus_for(job) -> list[str]:
    if not job.disposed_skus:
        return []
    return [s.strip() for s in job.disposed_skus.split(",") if s.strip()]


def parts_cost_for_jobs(jobs) -> dict[str, float]:
    """Total parts cost per CompletedJob.order_no, keyed by order_no.
    Jobs with no disposed_skus are omitted."""
    all_skus: set[str] = set()
    for job in jobs:
        all_skus.update(_skus_for(job))
    if not all_skus:
        return {}

    unit_costs = get_handl_client().get_part_costs(list(all_skus))

    result = {}
    for job in jobs:
        skus = _skus_for(job)
        if not skus:
            continue
        result[job.order_no] = round(sum(unit_costs.get(sku, 0) for sku in skus), 2)
    return result
