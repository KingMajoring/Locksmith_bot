"""Logs Engine (Area: office job lookup) — office staff look up one Handl
ReportID at a time to see the job's own details, and how far each
active WGTK locksmith's home postcode is from it. Shift information
(who's actually on today, via Microsoft Teams Shifts) isn't wired up
yet, so this ranks every active locksmith with a postcode set rather
than only ones on shift — a human still picks from the list.
"""
import logging

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.integrations.google_maps import get_google_maps_client
from apps.integrations.handl import get_handl_client
from apps.job_completion.services.labels import display_loss_type
from apps.locksmiths.models import Locksmith

logger = logging.getLogger(__name__)


def _nearest_locksmiths(job):
    """(locksmith, LocksmithDistance) pairs, nearest first, for every
    active locksmith with a home postcode set. Best-effort, same
    rationale as every other external lookup on this page — a missing
    vehicle location or a Google API failure just means no suggestions
    rather than a broken page."""
    if job.vehicle_latitude is None or job.vehicle_longitude is None:
        return []
    locksmiths = list(
        Locksmith.objects.filter(active=True).exclude(home_postcode="").order_by("name")
    )
    if not locksmiths:
        return []
    try:
        distances = get_google_maps_client().get_distances(
            [l.home_postcode for l in locksmiths], job.vehicle_latitude, job.vehicle_longitude,
        )
    except Exception:
        logger.exception("Failed to fetch Google distances for Logs Engine lookup %s", job.report_id)
        return []
    paired = [
        (locksmith, distance)
        for locksmith, distance in zip(locksmiths, distances)
        if distance.status == "OK" and distance.distance_metres is not None
    ]
    paired.sort(key=lambda pair: pair[1].distance_metres)
    return paired


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
            "nearest_locksmiths": _nearest_locksmiths(job) if job else [],
            "locksmiths_missing_postcode": (
                Locksmith.objects.filter(active=True, home_postcode="").count() if job else 0
            ),
        },
    )
