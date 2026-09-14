"""Logs Engine (Area: office job lookup) — office staff look up one Handl
ReportID at a time to see the job's own details, and which active WGTK
locksmith is best placed to take it: ranked by distance from whichever
is closer, their home postcode or where their soonest already-booked
future job already has them going (they're in that area anyway, so
that beats driving over from home) — either signal alone is enough to
put a locksmith in the running, so one with no home postcode on file
can still surface via an upcoming job. A locksmith with a known home
location clearly too far from the job (straight-line, see
_MAX_HOME_STRAIGHT_LINE_MILES) is filtered out before ever calling
Google — no point spending a real distance lookup, and a slot in the
25-origins-per-request batch, confirming what a rough distance already
rules out — and anyone whose real drive time comes back over
_MAX_DRIVE_TIME_MINUTES doesn't make the list at all, home or future
job alike. Shift information (who's actually on today, via Microsoft
Teams Shifts) isn't wired up yet, so this ranks every eligible active
locksmith rather than only ones on shift — a human still picks from
the list.
"""
import logging
from math import asin, cos, radians, sin, sqrt

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.integrations.google_maps import get_google_maps_client
from apps.integrations.handl import get_handl_client
from apps.job_completion.services.labels import display_loss_type
from apps.locksmiths.models import Locksmith

logger = logging.getLogger(__name__)

_EARTH_RADIUS_MILES = 3958.8

# A locksmith whose home is further than this from the job, as the crow
# flies, isn't a sensible suggestion — no real point spending a Distance
# Matrix API call (and a slot in RealGoogleMapsClient's 25-origins-per-
# request batch) confirming that Glasgow is a long way from Aylesbury.
# Generous rather than tight: UK driving distance often runs 1.3-1.4x
# straight-line, and this is filtering out candidates entirely, not
# just how they're ranked.
_MAX_HOME_STRAIGHT_LINE_MILES = 75

# Beyond this real drive time, a locksmith isn't a sensible suggestion
# regardless of which signal (home or a future job) got them onto the
# list — a future-job postcode has no straight-line pre-filter like a
# home lat/lng does (see _home_origin), so this is the only thing
# stopping "already booked nearby" from meaning five hours away.
_MAX_DRIVE_TIME_MINUTES = 120


def _straight_line_miles(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = map(radians, (lat1, lng1, lat2, lng2))
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return 2 * _EARTH_RADIUS_MILES * asin(sqrt(a))


def _home_origin(locksmith, job):
    """Google Distance Matrix origin string for this locksmith's home
    base — precise lat,lng when we have it (from Optimo's own "driver
    starting location" export, see apps.locksmiths.services — a proper
    coordinate is more accurate than a postcode's centroid), else
    falling back to their home postcode. "" when neither is set, or when
    a lat/lng home is clearly too far from this job to be worth a real
    distance lookup (see _MAX_HOME_STRAIGHT_LINE_MILES) — a postcode-only
    home can't be cheaply distance-checked this way (no coordinate to
    measure from without geocoding it), so is always let through; there
    are few enough of those on file that it doesn't matter."""
    if locksmith.home_latitude is not None and locksmith.home_longitude is not None:
        distance = _straight_line_miles(
            job.vehicle_latitude, job.vehicle_longitude,
            locksmith.home_latitude, locksmith.home_longitude,
        )
        if distance > _MAX_HOME_STRAIGHT_LINE_MILES:
            return ""
        return f"{locksmith.home_latitude},{locksmith.home_longitude}"
    return locksmith.home_postcode


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
    """((locksmith, LocksmithDistance, FutureLocksmithAttendance | None)
    triples, error_message) — ranked nearest first, for every active
    locksmith who has *either* a home base (lat/lng, or a postcode as a
    fallback) *or* a soonest already-booked future job with a usable
    postcode — a locksmith with no home location on file shouldn't be
    silently excluded just because they happen to already have an
    upcoming job near this one. The attendance is set when that
    locksmith's ranked distance came from a future job location rather
    than their home base.

    error_message is set (and the list empty) only when there WERE
    candidate locksmiths to check but the Google call itself failed —
    e.g. an API key restriction or a disabled API returns a clear
    top-level status Distance Matrix hands back, worth surfacing
    directly rather than just logging server-side, since this office
    tool has no other easy way to see that. Empty list with no error
    just means no locksmith had a usable location, or none resolved."""
    if job.vehicle_latitude is None or job.vehicle_longitude is None:
        return [], ""
    locksmiths = list(Locksmith.objects.filter(active=True).order_by("name"))
    if not locksmiths:
        return [], ""

    soonest_future = _soonest_future_attendance_by_locksmith(locksmiths)

    # Two candidate origins per locksmith where they have both: home
    # base, and their soonest future job's postcode — ranked together
    # below so whichever is actually closer wins. A locksmith with
    # neither gets no origin at all, and so never enters the ranking.
    origins, origin_locksmiths, origin_attendances = [], [], []
    for locksmith in locksmiths:
        home_origin = _home_origin(locksmith, job)
        if home_origin:
            origins.append(home_origin)
            origin_locksmiths.append(locksmith)
            origin_attendances.append(None)
        attendance = soonest_future.get(locksmith.pk)
        if attendance:
            origins.append(attendance.vehicle_postcode)
            origin_locksmiths.append(locksmith)
            origin_attendances.append(attendance)

    if not origins:
        return [], ""

    try:
        distances = get_google_maps_client().get_distances(
            origins, job.vehicle_latitude, job.vehicle_longitude,
        )
    except Exception as exc:
        logger.exception("Failed to fetch Google distances for Logs Engine lookup %s", job.report_id)
        return [], str(exc)

    best_by_locksmith = {}
    for locksmith, attendance, distance in zip(origin_locksmiths, origin_attendances, distances):
        if distance.status != "OK" or distance.distance_metres is None:
            continue
        if distance.duration_minutes is None or distance.duration_minutes > _MAX_DRIVE_TIME_MINUTES:
            continue
        current = best_by_locksmith.get(locksmith.pk)
        if current is None or distance.distance_metres < current[1].distance_metres:
            best_by_locksmith[locksmith.pk] = (locksmith, distance, attendance)

    ranked = list(best_by_locksmith.values())
    ranked.sort(key=lambda item: item[1].distance_metres)
    return ranked, ""


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

    nearest_locksmiths, nearest_locksmiths_error = _nearest_locksmiths(job) if job else ([], "")

    return render(
        request,
        "logs_engine/lookup.html",
        {
            "report_id": report_id,
            "searched": bool(report_id),
            "job": job,
            "service_label": display_loss_type(job.loss_type) if job else "",
            "nearest_locksmiths": nearest_locksmiths,
            "nearest_locksmiths_error": nearest_locksmiths_error,
            "locksmiths_missing_postcode": (
                Locksmith.objects.filter(
                    active=True, home_postcode="", home_latitude__isnull=True,
                ).count()
                if job else 0
            ),
        },
    )
