"""Logs Engine (Area: office job lookup) — office staff look up one Handl
ReportID at a time to see the job's own details, and which active WGTK
locksmiths could take it. Results are one LocksmithCard per locksmith,
not one row per locksmith — each card lists every sensible option for
getting THAT locksmith to the job: their home base, plus one option per
job they already have booked in the next _FUTURE_JOB_WINDOW_DAYS (not
just the chronologically soonest — a job booked for Thursday can still
be a better option than one booked for tomorrow, if Thursday's is
actually closer to this job), so office staff can see and choose
between all of a locksmith's plausible options rather than only ever
being shown whichever one the system picked as "best". Cards are
sorted by their own best option first; options within a card are
sorted the same way (see LocksmithCard/LocksmithOption). Either signal
(home or an upcoming job) alone is enough to put a locksmith in the
running, so one with no home postcode on file can still surface via an
upcoming job. A locksmith with a known home location clearly too far
from the job (straight-line, see _MAX_HOME_STRAIGHT_LINE_MILES) is
filtered out before ever calling Google — no point spending a real
distance lookup, and a slot in the 25-origins-per-request batch,
confirming what a rough distance already rules out — and any option
whose real drive time comes back over _MAX_DRIVE_TIME_MINUTES is
dropped, home or future job alike. Ranking also isn't just the drive
there: a locksmith has to actually do the job (see
_JOB_DURATION_MINUTES) and then get home afterwards, so
LocksmithOption.total_minutes covers the whole round trip, not just
the outbound leg, and expected_home_after is that same round trip
expressed as an actual clock time — when they'd actually walk in the
door if sent on this job — anchored to when they'd realistically set
off: right now for the home option, or Microsoft Teams' own shift
START time for that future job's own day for a future-job option, NOT
"now" for every option regardless of which day it's actually on, and
NOT derived from the future job's own booked time either — Handl's
AvailableFromDate is date-only (always midnight), so it's a fine
signal for "which day" but useless as a real time-of-day. None when
there's no shift on file for that particular day at all (a day off,
or Teams unreachable) — see _shift_info_by_locksmith.
LocksmithCard.expected_home is the BEFORE to each option's AFTER —
when Teams says they're normally due home TODAY anyway, this job
aside — so a human can see at a glance whether sending them somewhere
actually changes their day. None when there's nothing to base it on
(no shift on file for today, Teams unreachable, etc.).
Each option also carries its OWN day_job_count/map_url (a small Static
Maps preview: this job in blue, this locksmith's other booked jobs
that SAME day in red) — a locksmith with options spread across several
different days (this job today, another one already booked for
Thursday) has a different workload on each of those days, so "how busy
are they" only makes sense answered per option/day, not once for the
whole card. The home option's day is today (that's when they'd be
leaving from home for this job); a future-job option's day is that
job's own date. This is deliberately not chained into total_minutes
(fully accounting for a day's other jobs would need real route
ordering, not attempted here) — just a signal alongside it.
LocksmithCard.on_shift shows whether Microsoft Teams
Shifts has this locksmith rostered on right now — best-effort and
never used to filter the list (shift data can be wrong or stale, e.g.
an informal shift swap Teams doesn't know about), just another signal
alongside the rest: a human still picks from the list.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone as django_timezone

from apps.integrations.google_maps import LocksmithDistance, get_google_maps_client, static_map_url
from apps.integrations.handl import FutureLocksmithAttendance, get_handl_client
from apps.integrations.teams_shifts import get_teams_shifts_client
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

# How far ahead to look for a locksmith's future booked jobs when
# ranking, not just their single soonest one — a job booked in 4 days
# might be genuinely closer to this new job than one booked for
# tomorrow, so every job within this window is checked as its own
# candidate origin, and whichever turns out closest wins (see
# _nearest_locksmiths). A week feels like the right horizon for "should
# still meaningfully inform who's a sensible pick today" — a job a
# month out says little about where someone will actually be.
_FUTURE_JOB_WINDOW_DAYS = 7


@dataclass(frozen=True)
class LocksmithOption:
    """One way a locksmith could plausibly take this job — either from
    their home base, or via a job they already have booked in nearby
    within _FUTURE_JOB_WINDOW_DAYS."""
    # Outbound leg only: home, or this specific future job -> this job.
    distance: LocksmithDistance
    attendance: FutureLocksmithAttendance | None
    # The drive back home afterwards — None exactly when total_minutes
    # is (nothing resolved it). Split out from total_minutes so the
    # page can show its own figure alongside the outbound drive time,
    # proving expected_home_after genuinely bakes in the trip home
    # rather than asking anyone to just trust an invisible number.
    return_minutes: int | None
    # Outbound drive + _JOB_DURATION_MINUTES + the drive back home
    # afterwards, as a duration — None when the return leg couldn't be
    # resolved (no home location on file at all, or that lookup itself
    # failed). Kept for sorting/colour-coding; expected_home_after is
    # the figure actually shown to office staff.
    total_minutes: int | None
    # The same round trip as total_minutes, but as an actual
    # clock-time: when this locksmith would walk in their own front
    # door if sent on this job — total_minutes added on top of when
    # they'd realistically set off (see _nearest_locksmiths). None
    # when total_minutes is (nothing to add the round trip on top of),
    # or — for a future-job option — when there's no Teams shift on
    # file for that job's own day to anchor the departure to at all.
    expected_home_after: datetime | None
    # How many jobs this locksmith already has booked in on THIS
    # option's day — today for the home option, or that future job's
    # own date for a future-job option. Includes this option's own job
    # when it's a future-job option (it IS one of their jobs that day).
    day_job_count: int
    # Google Static Maps preview (this job in blue, this locksmith's
    # other booked jobs on this option's day in red) for an at-a-glance
    # hover — "" when there's no API key configured or nothing to plot.
    map_url: str


@dataclass(frozen=True)
class LocksmithCard:
    locksmith: Locksmith
    # Every sensible option for this locksmith, best (lowest
    # total_minutes) first — see _nearest_locksmiths.
    options: list[LocksmithOption]
    # Whether Microsoft Teams Shifts has this locksmith rostered on
    # right now — None when that couldn't be checked at all (no Team ID
    # configured, no email on file, or the Graph call itself failed),
    # distinct from False ("checked, and they're not on shift").
    on_shift: bool | None
    # When Teams expects this locksmith to finish (and be home) TODAY,
    # this job aside — the BEFORE to each option's expected_home_after.
    # None when there's no shift on file for today at all (a day off),
    # or the Teams lookup itself failed/isn't configured.
    expected_home: datetime | None


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


def _future_attendance_summary_by_locksmith(locksmiths, *, now):
    """{locksmith.pk: {"within_window": [FutureLocksmithAttendance, ...],
    "by_date": {date: [FutureLocksmithAttendance, ...]}}} for every one
    of these locksmiths with at least one upcoming job (with a usable
    postcode) already booked in, within the next _FUTURE_JOB_WINDOW_DAYS.

    "within_window" is every one of their jobs booked in that window
    (not just the soonest) — each becomes its own candidate origin in
    _nearest_locksmiths, so whichever turns out to actually be closest
    to the new job wins, rather than always defaulting to whichever is
    chronologically soonest even when a later one in the same week
    would be a far more sensible pick.
    "by_date" is that same set of attendances grouped by day, so a
    caller can answer "how busy are they on THIS particular day" per
    option (see LocksmithOption.day_job_count) rather than only ever
    for one fixed date — today's home option and a Thursday-booked
    option need different answers to that question, not the same one.
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
    window_end = now + timedelta(days=_FUTURE_JOB_WINDOW_DAYS)
    summary = {}
    for attendance in attendances:
        locksmith = soter_id_to_locksmith.get(attendance.soter_locksmith_id)
        if locksmith is None or not attendance.vehicle_postcode:
            continue
        if not (now <= attendance.available_from <= window_end):
            continue
        entry = summary.setdefault(locksmith.pk, {"within_window": [], "by_date": {}})
        entry["within_window"].append(attendance)
        entry["by_date"].setdefault(attendance.available_from.date(), []).append(attendance)
    return summary


@dataclass(frozen=True)
class ShiftInfo:
    # Whether this locksmith is currently inside a published Teams
    # Shift right now — not just scheduled sometime today.
    on_shift: bool
    # The latest shift_end among today's published shifts for this
    # locksmith — when Teams expects them home today. None when they
    # have no shift on file for today at all (a day off).
    expected_home: datetime | None
    # {date: earliest shift_start} for every date in the fetched
    # window this locksmith has a published shift on — lets a
    # future-job option anchor its departure to that day's ACTUAL
    # start of work, not "now" or the job's own (date-only, no real
    # time-of-day) booking record. A date with no entry means no
    # published shift that day (a day off, or just not published yet).
    starts_by_date: dict


def _shift_info_by_locksmith(locksmiths, *, now, window_end_date):
    """({locksmith.pk: ShiftInfo} | None, error_message).

    Fetches shifts for the whole [now.date(), window_end_date] range
    in ONE call (see TeamsShiftsClient.list_shifts_for_date_range) —
    looping a per-date call over that range instead would be exactly
    the kind of chatty Graph usage that made a lookup hang once
    already (see that method's real implementation).

    The dict is None (not just empty) when this couldn't be checked at
    all, so callers can tell "confirmed nobody's on shift, and nobody
    has a shift on file" apart from "the lookup failed" rather than
    defaulting every locksmith to off-shift/no-shift-data on a Graph
    outage or missing Team ID. error_message is set (and the dict
    None) only when there WERE emails to check but the Teams/Graph
    call itself failed — same rationale as _nearest_locksmiths' own
    error_message: worth surfacing directly, since this office tool
    has no other easy way to see it. A locksmith with no email on file
    at all just gets on_shift=False, expected_home=None, an empty
    starts_by_date (as if they simply have no shifts at all).

    Matches by Locksmith.user.email (the real Microsoft sign-in email,
    verified the first time this locksmith actually logged in — see
    apps.accounts.adapter) in preference to Locksmith.email (just
    Handl/Soter's own record of it, confirmed live to sometimes drift
    slightly from what someone actually signs into Microsoft with) —
    falling back to the Handl-synced one only for a locksmith who's
    never logged into the portal yet, so user is still null."""
    emails_by_pk = {}
    for locksmith in locksmiths:
        email = (locksmith.user.email if locksmith.user_id else "") or locksmith.email
        if email:
            emails_by_pk[locksmith.pk] = email.strip().lower()
    if not emails_by_pk:
        return None, ""
    try:
        shifts = get_teams_shifts_client().list_shifts_for_date_range(now.date(), window_end_date)
    except Exception as exc:
        logger.exception("Failed to fetch Teams shifts for Logs Engine")
        return None, str(exc)

    shifts_by_email = {}
    for shift in shifts:
        shifts_by_email.setdefault(shift.email.lower(), []).append(shift)

    result = {}
    for locksmith in locksmiths:
        person_shifts = shifts_by_email.get(emails_by_pk.get(locksmith.pk, ""), [])
        starts_by_date = {}
        for shift in person_shifts:
            shift_date = shift.shift_start.date()
            if shift_date not in starts_by_date or shift.shift_start < starts_by_date[shift_date]:
                starts_by_date[shift_date] = shift.shift_start
        result[locksmith.pk] = ShiftInfo(
            on_shift=any(s.shift_start <= now <= s.shift_end for s in person_shifts),
            expected_home=max(
                (s.shift_end for s in person_shifts if s.shift_start.date() == now.date()), default=None,
            ),
            starts_by_date=starts_by_date,
        )
    return result, ""


def _return_minutes_by_locksmith(locksmiths_needing_return, job):
    """{locksmith.pk: minutes} for the drive back home after this job,
    for every locksmith in `locksmiths_needing_return` (those with at
    least one future-job-based option).

    A home-based option doesn't need a lookup here at all — its return
    leg is the exact same two points as its outbound one, just
    reversed, so that duration is reused as-is rather than spending a
    second API call to confirm a UK road is roughly the same length in
    both directions. A future-job-based option's return leg goes back
    to their REAL home — a genuinely different route from its outbound
    leg (attendance location -> job) — so that does need its own
    lookup, batched in one call the same way the main lookup is. This
    is computed once per locksmith (not once per option) since it only
    depends on their home and this job's location, not on which future
    job got them here — every future-job-based option for the same
    locksmith shares the same return trip. Best-effort, same rationale
    as every other external lookup on this page: a locksmith with no
    resolvable home for this just doesn't get a total_minutes figure,
    rather than breaking the whole list."""
    lookup_locksmiths, lookup_origins = [], []
    for locksmith in locksmiths_needing_return:
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
    """(list[LocksmithCard], distance_error, on_shift_error) — one card
    per active locksmith who has *either* a home base (lat/lng, or a
    postcode as a fallback) *or* a job with a usable postcode already
    booked in within _FUTURE_JOB_WINDOW_DAYS — a locksmith with no home
    location on file shouldn't be silently excluded just because they
    happen to already have an upcoming job near this one. Cards are
    sorted best-option-first; each card's own options are sorted the
    same way (nearest/lowest total_minutes first) — see LocksmithCard.
    An option's attendance is set when it came from a future job
    location rather than the locksmith's home base.

    distance_error is set (and the list empty) only when there WERE
    candidate locksmiths to check but the Google call itself failed —
    e.g. an API key restriction or a disabled API returns a clear
    top-level status Distance Matrix hands back, worth surfacing
    directly rather than just logging server-side, since this office
    tool has no other easy way to see that. Empty list with no error
    just means no locksmith had a usable location, or none resolved.
    on_shift_error is the same idea for the Teams Shifts lookup (see
    _shift_info_by_locksmith) — set only when that call itself failed,
    never for a locksmith simply not being on shift."""
    if job.vehicle_latitude is None or job.vehicle_longitude is None:
        return [], "", ""
    locksmiths = list(Locksmith.objects.filter(active=True).select_related("user").order_by("name"))
    if not locksmiths:
        return [], "", ""

    now = django_timezone.localtime(django_timezone.now()).replace(tzinfo=None)
    future_summary = _future_attendance_summary_by_locksmith(locksmiths, now=now)

    # Candidate origins per locksmith: their home base, plus one per
    # job they already have booked in the next _FUTURE_JOB_WINDOW_DAYS
    # (not just the soonest) — ranked together below so whichever is
    # actually closest wins, home or any one of those future jobs
    # alike. A locksmith with none of these gets no origin at all, and
    # so never enters the ranking.
    origins, origin_locksmiths, origin_attendances = [], [], []
    for locksmith in locksmiths:
        home_origin = _home_origin(locksmith, job)
        if home_origin:
            origins.append(home_origin)
            origin_locksmiths.append(locksmith)
            origin_attendances.append(None)
        for attendance in future_summary.get(locksmith.pk, {}).get("within_window", []):
            origins.append(attendance.vehicle_postcode)
            origin_locksmiths.append(locksmith)
            origin_attendances.append(attendance)

    if not origins:
        return [], "", ""

    try:
        distances = get_google_maps_client().get_distances(
            origins, job.vehicle_latitude, job.vehicle_longitude,
        )
    except Exception as exc:
        logger.exception("Failed to fetch Google distances for Logs Engine lookup %s", job.report_id)
        return [], str(exc), ""

    options_by_locksmith_pk = {}
    locksmiths_by_pk = {}
    for locksmith, attendance, distance in zip(origin_locksmiths, origin_attendances, distances):
        if distance.status != "OK" or distance.distance_metres is None:
            continue
        if distance.duration_minutes is None or distance.duration_minutes > _MAX_DRIVE_TIME_MINUTES:
            continue
        locksmiths_by_pk[locksmith.pk] = locksmith
        options_by_locksmith_pk.setdefault(locksmith.pk, []).append((distance, attendance))

    if not options_by_locksmith_pk:
        return [], "", ""

    locksmiths_needing_return = [
        locksmiths_by_pk[pk] for pk, options in options_by_locksmith_pk.items()
        if any(attendance is not None for _distance, attendance in options)
    ]
    return_minutes_by_locksmith = _return_minutes_by_locksmith(locksmiths_needing_return, job)
    window_end_date = now.date() + timedelta(days=_FUTURE_JOB_WINDOW_DAYS)
    shift_info_by_pk, on_shift_error = _shift_info_by_locksmith(
        list(locksmiths_by_pk.values()), now=now, window_end_date=window_end_date,
    )
    job_location = f"{job.vehicle_latitude},{job.vehicle_longitude}"

    cards = []
    for pk, raw_options in options_by_locksmith_pk.items():
        locksmith = locksmiths_by_pk[pk]
        by_date = future_summary.get(pk, {}).get("by_date", {})
        shift_info = shift_info_by_pk.get(pk) if shift_info_by_pk is not None else None
        options = []
        for distance, attendance in raw_options:
            return_minutes = (
                return_minutes_by_locksmith.get(pk) if attendance is not None
                else distance.duration_minutes  # home-based: return leg assumed symmetric to the outbound one
            )
            total_minutes = (
                distance.duration_minutes + _JOB_DURATION_MINUTES + return_minutes
                if return_minutes is not None else None
            )
            # When they'd actually set off: right now for the home
            # option, or Teams' own shift start time for that future
            # job's own day for a future-job option — None when there's
            # no shift on file for that day at all, rather than
            # guessing from the job's own (date-only) booking record.
            if attendance is not None:
                departure = shift_info.starts_by_date.get(attendance.available_from.date()) if shift_info else None
            else:
                departure = now
            expected_home_after = (
                departure + timedelta(minutes=total_minutes)
                if departure is not None and total_minutes is not None else None
            )
            # Home has no date of its own — today is the sensible one,
            # since that's when they'd be leaving from home for this
            # job; a future-job option uses that job's own date.
            option_date = attendance.available_from.date() if attendance is not None else now.date()
            day_attendances = by_date.get(option_date, [])
            map_url = static_map_url([
                ("color:blue|label:J", [job_location]),
                ("color:red", [a.vehicle_postcode for a in day_attendances]),
            ])
            options.append(LocksmithOption(
                distance, attendance, return_minutes, total_minutes, expected_home_after,
                day_job_count=len(day_attendances), map_url=map_url,
            ))
        options.sort(key=lambda o: (o.total_minutes is None, o.total_minutes, o.distance.distance_metres))

        cards.append(LocksmithCard(
            locksmith, options,
            on_shift=shift_info.on_shift if shift_info is not None else None,
            expected_home=shift_info.expected_home if shift_info is not None else None,
        ))

    cards.sort(key=lambda c: (
        c.options[0].total_minutes is None, c.options[0].total_minutes, c.options[0].distance.distance_metres,
    ))
    return cards, "", on_shift_error


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

    nearest_locksmiths, nearest_locksmiths_error, on_shift_error = (
        _nearest_locksmiths(job) if job else ([], "", "")
    )

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
            "on_shift_error": on_shift_error,
            "job_duration_minutes": _JOB_DURATION_MINUTES,
            "locksmiths_missing_postcode": (
                Locksmith.objects.filter(
                    active=True, home_postcode="", home_latitude__isnull=True,
                ).count()
                if job else 0
            ),
        },
    )
