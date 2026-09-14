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
job alike. Ranking also isn't just the drive there: a locksmith has to
actually do the job (see _JOB_DURATION_MINUTES) and then get home
afterwards, so RankedLocksmith.total_minutes covers the whole round
trip, not just the outbound leg — see _nearest_locksmiths. That figure
still only accounts for the ONE soonest future job though, not a
locksmith's whole day — RankedLocksmith.future_job_count and map_url
(a small Static Maps preview: this job in blue, all their other booked
jobs in red) exist so a human can see when someone's actually busier
than the numbers alone suggest, since fully chaining multiple booked
jobs into one total would need real route ordering, not attempted
here. Shift information (who's actually on today, via Microsoft Teams
Shifts) isn't wired up yet, so this ranks every eligible active
locksmith rather than only ones on shift — a human still picks from
the list.
"""
import logging
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.integrations.google_maps import LocksmithDistance, get_google_maps_client, static_map_url
from apps.integrations.handl import FutureLocksmithAttendance, get_handl_client
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
# stopping "already booked nearby" from meaning five hours away. Only
# ever checked against the outbound leg — see total_minutes for the
# whole round trip.
_MAX_DRIVE_TIME_MINUTES = 120

# Flat placeholder for how long a job itself takes, on top of the
# driving — no per-service-type estimate exists yet (a lock change and
# a full barrel replacement don't take the same time), so this is a
# rough stand-in used for every job until a real one exists.
_JOB_DURATION_MINUTES = 40


@dataclass(frozen=True)
class RankedLocksmith:
    locksmith: Locksmith
    # Outbound leg only: home (or their soonest future job) -> this job.
    distance: LocksmithDistance
    attendance: FutureLocksmithAttendance | None
    # Outbound drive + _JOB_DURATION_MINUTES + the drive back home
    # afterwards — None when the return leg couldn't be resolved (no
    # home location on file at all, or that lookup itself failed).
    total_minutes: int | None
    # How many future jobs this locksmith already has booked in total —
    # NOT just the one `attendance` (the soonest) points at. total_minutes
    # above only ever accounts for that one, so a high count here is a
    # sign this locksmith may be busier than the figures suggest.
    future_job_count: int
    # Google Static Maps preview (this job in blue, every one of this
    # locksmith's other booked jobs in red) for an at-a-glance hover —
    # "" when there's no API key configured or nothing to plot.
    map_url: str


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


def _future_attendance_summary_by_locksmith(locksmiths):
    """{locksmith.pk: {"soonest": FutureLocksmithAttendance, "count": int,
    "postcodes": [str, ...]}} for every one of these locksmiths with at
    least one upcoming job (with a usable postcode) already booked in.

    "soonest" (used to pick the outbound origin) is just the earliest —
    a locksmith already going to be nearby tomorrow is a more useful
    suggestion than one three weeks out — but "count"/"postcodes" cover
    ALL of them, not just that one: total_minutes elsewhere only ever
    accounts for the soonest job, so the count is what actually tells
    office staff a locksmith might have a full day already booked, and
    the postcodes feed the map preview (see RankedLocksmith.map_url).
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
    summary = {}
    for attendance in attendances:
        locksmith = soter_id_to_locksmith.get(attendance.soter_locksmith_id)
        if locksmith is None or not attendance.vehicle_postcode:
            continue
        entry = summary.setdefault(locksmith.pk, {"soonest": attendance, "count": 0, "postcodes": []})
        entry["count"] += 1
        entry["postcodes"].append(attendance.vehicle_postcode)
        if attendance.available_from < entry["soonest"].available_from:
            entry["soonest"] = attendance
    return summary


def _return_minutes_by_locksmith(ranked, job):
    """{locksmith.pk: minutes} for the drive back home after this job,
    for every entry in `ranked` (a list of (locksmith, distance,
    attendance) triples) that reached the list via a future job.

    A home-based entry doesn't need a lookup here at all — its return
    leg is the exact same two points as its outbound one, just
    reversed, so that duration is reused as-is rather than spending a
    second API call to confirm a UK road is roughly the same length in
    both directions. A future-job-based entry's return leg goes back to
    their REAL home, a genuinely different route from its outbound leg
    (attendance location -> job), so that does need its own lookup —
    batched in one call the same way the main lookup is. Best-effort,
    same rationale as every other external lookup on this page: a
    locksmith with no resolvable home for this just doesn't get a
    total_minutes figure, rather than breaking the whole list."""
    lookup_locksmiths, lookup_origins = [], []
    for locksmith, _distance, attendance in ranked:
        if attendance is None:
            continue
        home_origin = _home_origin(locksmith, job)
        if home_origin:
            lookup_locksmiths.append(locksmith)
            lookup_origins.append(home_origin)

    if not lookup_origins:
        return {}
    try:
        return_distances = get_google_maps_client().get_distances(
            lookup_origins, job.vehicle_latitude, job.vehicle_longitude,
        )
    except Exception:
        logger.exception("Failed to fetch return-trip Google distances for Logs Engine lookup %s", job.report_id)
        return {}

    return {
        locksmith.pk: return_distance.duration_minutes
        for locksmith, return_distance in zip(lookup_locksmiths, return_distances)
        if return_distance.status == "OK" and return_distance.duration_minutes is not None
    }


def _nearest_locksmiths(job):
    """(list[RankedLocksmith], error_message) — ranked nearest first
    (by total_minutes, the whole round trip, when known — see
    RankedLocksmith), for every active locksmith who has *either* a
    home base (lat/lng, or a postcode as a fallback) *or* a soonest
    already-booked future job with a usable postcode — a locksmith with
    no home location on file shouldn't be silently excluded just
    because they happen to already have an upcoming job near this one.
    attendance is set when a locksmith's outbound distance came from a
    future job location rather than their home base.

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

    future_summary = _future_attendance_summary_by_locksmith(locksmiths)

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
        attendance = future_summary.get(locksmith.pk, {}).get("soonest")
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
    if not ranked:
        return [], ""

    return_minutes_by_locksmith = _return_minutes_by_locksmith(ranked, job)
    job_location = f"{job.vehicle_latitude},{job.vehicle_longitude}"

    results = []
    for locksmith, distance, attendance in ranked:
        return_minutes = (
            return_minutes_by_locksmith.get(locksmith.pk) if attendance is not None
            else distance.duration_minutes  # home-based: return leg assumed symmetric to the outbound one
        )
        total_minutes = (
            distance.duration_minutes + _JOB_DURATION_MINUTES + return_minutes
            if return_minutes is not None else None
        )
        locksmith_summary = future_summary.get(locksmith.pk, {})
        map_url = static_map_url([
            ("color:blue|label:J", [job_location]),
            ("color:red", locksmith_summary.get("postcodes", [])),
        ])
        results.append(RankedLocksmith(
            locksmith, distance, attendance, total_minutes,
            future_job_count=locksmith_summary.get("count", 0),
            map_url=map_url,
        ))

    results.sort(key=lambda r: (r.total_minutes is None, r.total_minutes, r.distance.distance_metres))
    return results, ""


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
            "job_duration_minutes": _JOB_DURATION_MINUTES,
            "locksmiths_missing_postcode": (
                Locksmith.objects.filter(
                    active=True, home_postcode="", home_latitude__isnull=True,
                ).count()
                if job else 0
            ),
        },
    )
