"""Logs Engine (Area: office job lookup) — office staff look up one Handl
ReportID at a time to see the job's own details, and which active WGTK
locksmith is best placed to take it: ranked by distance from whichever
is closer, their home postcode or where their soonest already-booked
future job already has them going (they're in that area anyway, so
that beats driving over from home) — either signal alone is enough to
put a locksmith in the running, so one with no home postcode on file
can still surface via an upcoming job. Shift information (who's
actually on today, via Microsoft Teams Shifts) isn't wired up yet, so
this ranks every eligible active locksmith rather than only ones on
shift — a human still picks from the list.
"""
import logging

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.integrations.google_maps import get_google_maps_client
from apps.integrations.handl import get_handl_client
from apps.job_completion.services.labels import display_loss_type
from apps.locksmiths.models import Locksmith

logger = logging.getLogger(__name__)


def _soonest_future_attendance_by_locksmith(locksmiths):
    """{locksmith.pk: FutureLocksmithAttendance}, the soonest upcoming
    job (with a usable postcode) each of these locksmiths is already
    booked to attend — soonest, because a locksmith already going to be
    nearby tomorrow is a more useful suggestion than one three weeks out.
    Best-effort: a Handl failure here just means the ranking falls back
    to home-postcode distance alone, same as every other lookup on this
    page."""
    soter_id_to_locksmith = {
        soter_id: locksmith for locksmith in locksmiths for soter_id in locksmith.soter_id_list
    }
    try:
        attendances = get_handl_client().get_future_locksmith_attendances()
    except Exception:
        logger.exception("Failed to fetch future locksmith attendances for Logs Engine")
        return {}
    soonest = {}
    for attendance in attendances:
        locksmith = soter_id_to_locksmith.get(attendance.soter_locksmith_id)
        if locksmith is None or not attendance.vehicle_postcode:
            continue
        existing = soonest.get(locksmith.pk)
        if existing is None or attendance.available_from < existing.available_from:
            soonest[locksmith.pk] = attendance
    return soonest


def _nearest_locksmiths(job):
    """(locksmith, LocksmithDistance, FutureLocksmithAttendance | None)
    triples, nearest first, for every active locksmith who has *either*
    a home postcode set *or* a soonest already-booked future job with a
    usable postcode — a locksmith with no home postcode on file
    shouldn't be silently excluded just because they happen to already
    have an upcoming job near this one. The attendance is set when that
    locksmith's ranked distance came from a future job location rather
    than their home postcode. Best-effort, same rationale as every
    other external lookup on this page — a missing vehicle location or
    a Google API failure just means no suggestions rather than a broken
    page."""
    if job.vehicle_latitude is None or job.vehicle_longitude is None:
        return []
    locksmiths = list(Locksmith.objects.filter(active=True).order_by("name"))
    if not locksmiths:
        return []

    soonest_future = _soonest_future_attendance_by_locksmith(locksmiths)

    # Two candidate origins per locksmith where they have both: home
    # postcode, and their soonest future job's postcode — ranked
    # together below so whichever is actually closer wins. A locksmith
    # with neither gets no origin at all, and so never enters the
    # ranking.
    origins, origin_locksmiths, origin_attendances = [], [], []
    for locksmith in locksmiths:
        if locksmith.home_postcode:
            origins.append(locksmith.home_postcode)
            origin_locksmiths.append(locksmith)
            origin_attendances.append(None)
        attendance = soonest_future.get(locksmith.pk)
        if attendance:
            origins.append(attendance.vehicle_postcode)
            origin_locksmiths.append(locksmith)
            origin_attendances.append(attendance)

    if not origins:
        return []

    try:
        distances = get_google_maps_client().get_distances(
            origins, job.vehicle_latitude, job.vehicle_longitude,
        )
    except Exception:
        logger.exception("Failed to fetch Google distances for Logs Engine lookup %s", job.report_id)
        return []

    best_by_locksmith = {}
    for locksmith, attendance, distance in zip(origin_locksmiths, origin_attendances, distances):
        if distance.status != "OK" or distance.distance_metres is None:
            continue
        current = best_by_locksmith.get(locksmith.pk)
        if current is None or distance.distance_metres < current[1].distance_metres:
            best_by_locksmith[locksmith.pk] = (locksmith, distance, attendance)

    ranked = list(best_by_locksmith.values())
    ranked.sort(key=lambda item: item[1].distance_metres)
    return ranked


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
