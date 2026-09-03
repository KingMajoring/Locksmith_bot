"""Daily pull of completed Optimo jobs into CompletedJob rows.

Combines two Optimo calls (order summary + completion details), resolves
the driver to a WGTK locksmith via OptimoDriverId, and resolves vehicle/
service details via Handl using the ReportID parsed out of the Optimo
orderNo (format "<ReportID>_<date>").

Only "success" and "failed" jobs are stored — anything still "scheduled"
or otherwise not yet completed on the pull date is skipped; it'll be
picked up on completion once the pull runs for the day it actually
finishes on. Re-running for a date already pulled is safe: existing
CompletedJob rows are refreshed by order_no, but failure_category (a
manual office decision) is never overwritten by a repull.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from apps.integrations.handl import get_handl_client
from apps.integrations.optimo import get_optimo_client
from apps.locksmiths.models import OptimoDriverId

from ..models import CompletedJob

_COMPLETED_STATUSES = {CompletedJob.Status.SUCCESS, CompletedJob.Status.FAILED}


@dataclass(frozen=True)
class PullSummary:
    created: int
    updated: int
    skipped_not_completed: int
    skipped_admin: int = 0


# A leading digit run alone isn't a reliable enough signal that an
# orderNo is a real Handl claim — plenty of admin notes start with
# digits too ("20 MIN FLEET & STOCK CHECK", "17 Little Venice...").
# Requiring a well-formed "20YY" year after the ID isn't reliable
# either — real jobs' dates are hand-typed into Optimo and come with
# all kinds of typos (confirmed live: "_202-08-25", "_1016-08-18",
# "_18/0/2026", "_202+-04-08", "-026-01-09" — missing/extra/wrong
# digits, reordered fields). The one thing that reliably distinguishes
# a real "<ReportID><separator><date>" order from an admin note is that
# nothing *after* the leading ID is ever a letter — a mistyped date is
# still only digits and separators, whereas a note is prose.
_REPORT_ID_PATTERN = re.compile(r"^\s*(\d+)[\d\-_/\\~ +]*$")


def _report_id_from_order_no(order_no: str) -> str | None:
    """The numeric Handl ReportID this Optimo orderNo corresponds to, or
    None if it's an internal/admin entry with no real ReportID at all
    (confirmed live: hundreds of entries like "SEND KEY TO CHARLEY" and
    "**HALF DAY TODAY** UP TO 20 MIN VAN & STOCK CHECK" — free-text
    notes drivers add to their Optimo route, not Handl claims)."""
    match = _REPORT_ID_PATTERN.match(order_no)
    return match.group(1) if match else None


def pull_completed_jobs_for_date(for_date: date) -> PullSummary:
    optimo = get_optimo_client()
    handl = get_handl_client()

    order_summaries = optimo.list_orders_for_date(for_date)
    order_nos = [summary.order_no for summary in order_summaries]
    completions = optimo.get_completion_details(order_nos)

    driver_map = {
        driver_id.optimo_driver_serial: driver_id.locksmith
        for driver_id in OptimoDriverId.objects.select_related("locksmith")
    }

    completed_summaries = [
        summary
        for summary in order_summaries
        if (completion := completions.get(summary.order_no))
        and completion.status in _COMPLETED_STATUSES
    ]

    # Not every completed Optimo order is a real locksmith job — some are
    # internal/admin housekeeping entries with no real ReportID at all,
    # so they're not Handl claims and must never be stored as a
    # CompletedJob — not just skipped for the Handl lookup, which used
    # to leave them in with blank make/model/etc, polluting job counts
    # and cost/margin totals.
    job_entries = [
        (s, rid)
        for s in completed_summaries
        if (rid := _report_id_from_order_no(s.order_no)) is not None
    ]
    skipped_admin = len(completed_summaries) - len(job_entries)

    report_ids = [rid for _s, rid in job_entries]
    job_details = handl.get_job_details(report_ids)
    disposed_skus = handl.get_disposed_skus(report_ids)

    created = 0
    updated = 0
    for summary, report_id in job_entries:
        completion = completions[summary.order_no]
        details = job_details.get(report_id)

        defaults = {
            "report_id": report_id,
            "job_date": for_date,
            "locksmith": driver_map.get(summary.driver_serial),
            "driver_serial": summary.driver_serial,
            "status": completion.status,
            "start_time": completion.start_time,
            "end_time": completion.end_time,
            "distance_metres": summary.distance_metres,
            "travel_time_seconds": summary.travel_time_seconds,
            "make": details.make if details else "",
            "model": details.model if details else "",
            "year": details.year if details else "",
            "vin": details.vin if details else "",
            "service_type": details.service_type if details else "",
            "loss_type": details.loss_type if details else "",
            "supplied_service": details.supplied_service if details else "",
            "net_cost": details.net_cost if details else None,
            "disposed_skus": ", ".join(disposed_skus.get(report_id, [])),
            "completion_note": completion.note,
        }
        _obj, was_created = CompletedJob.objects.update_or_create(
            order_no=summary.order_no, defaults=defaults
        )
        if was_created:
            created += 1
        else:
            updated += 1

    skipped_not_completed = len(order_summaries) - len(completed_summaries)
    return PullSummary(
        created=created,
        updated=updated,
        skipped_not_completed=skipped_not_completed,
        skipped_admin=skipped_admin,
    )
