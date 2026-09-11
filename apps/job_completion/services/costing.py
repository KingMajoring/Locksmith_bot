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
from apps.locksmith_portal.models import PortalDisposal


def _skus_for(job) -> list[str]:
    if not job.disposed_skus:
        return []
    return [s.strip() for s in job.disposed_skus.split(",") if s.strip()]


def _client_supplied_skus_by_order(order_nos) -> dict[str, set[str]]:
    """order_no -> set of upper-cased part codes the client supplied
    themselves on that job (see PortalDisposal.client_supplied). Handl's
    own Inventory_Disposals carries no such distinction — a
    client-supplied part is written there exactly like a real one (see
    apps.integrations.handl.record_client_supplied_disposal) so it still
    shows up in job.disposed_skus — this is the only place that
    knowledge exists, and it's needed to keep those parts out of the
    job's cost."""
    result: dict[str, set[str]] = {}
    rows = PortalDisposal.objects.filter(
        order_no__in=order_nos, client_supplied=True
    ).values_list("order_no", "part_code")
    for order_no, part_code in rows:
        result.setdefault(order_no, set()).add(part_code.upper())
    return result


def parts_cost_for_jobs(jobs) -> dict[str, float]:
    """Total parts cost per CompletedJob.order_no, keyed by order_no.
    Jobs with no disposed_skus are omitted. A part the client supplied
    themselves never cost WGTK anything, so it's excluded from the sum
    even though it's still listed in disposed_skus (see
    _client_supplied_skus_by_order) — a job whose only disposed parts
    were all client-supplied still gets an entry here, correctly valued
    at £0, rather than being omitted as if nothing were disposed at all."""
    all_skus: set[str] = set()
    for job in jobs:
        all_skus.update(_skus_for(job))
    if not all_skus:
        return {}

    unit_costs = get_handl_client().get_part_costs(list(all_skus))
    excluded_by_order = _client_supplied_skus_by_order([job.order_no for job in jobs])

    result = {}
    for job in jobs:
        skus = _skus_for(job)
        if not skus:
            continue
        excluded = excluded_by_order.get(job.order_no, set())
        result[job.order_no] = round(
            sum(unit_costs.get(sku, 0) for sku in skus if sku.upper() not in excluded), 2
        )
    return result
