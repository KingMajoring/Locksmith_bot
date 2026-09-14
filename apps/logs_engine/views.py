"""Logs Engine (Area: office job lookup) — office staff look up one Handl
ReportID at a time to see the job's own details. First step towards
suggesting a nearby on-shift WGTK locksmith for it (distance via a
Google API, shift via Microsoft Teams Shifts) — those two integrations
aren't built yet, so today this just surfaces the job card.
"""
import logging

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.integrations.handl import get_handl_client
from apps.job_completion.services.labels import display_loss_type

logger = logging.getLogger(__name__)


@login_required
def lookup(request):
    report_id = request.GET.get("report_id", "").strip()
    job = None
    if report_id:
        try:
            details = get_handl_client().get_job_details([report_id])
        except Exception:
            logger.exception("Failed to fetch Handl job details for Logs Engine lookup %s", report_id)
            details = {}
        job = details.get(report_id)

    return render(
        request,
        "logs_engine/lookup.html",
        {
            "report_id": report_id,
            "searched": bool(report_id),
            "job": job,
            "service_label": display_loss_type(job.loss_type) if job else "",
        },
    )
