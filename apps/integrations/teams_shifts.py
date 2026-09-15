"""Access to Microsoft Teams Shifts (via Microsoft Graph), for Logs
Engine — best-effort visibility into which WGTK locksmiths are
actually on shift today, shown alongside the nearest-locksmith
suggestion rather than used to filter it (shift data can be wrong or
stale, e.g. a locksmith swaps a shift informally — a human still picks
from the list, same as everywhere else on this page).

Reuses the SAME app-only (client credentials) Graph app registration
as MicrosoftGraphEmailBackend (see graph_email_backend.py, the
MS_GRAPH_MAIL_* settings) — it just also needs the Schedule.Read.All
application permission granted (with admin consent) alongside whatever
it already has for Mail.Send, since both call Graph as the same app.
The one genuinely new piece of information is WGTK's own rota Team ID
(see TeamsShiftsSettings — admin-editable like the other API settings
in this app, since it isn't a secret and might need changing without a
redeploy).

Until MS_GRAPH_MAIL_CLIENT_ID/SECRET/TENANT_ID AND a Team ID are both
configured, get_teams_shifts_client() returns MockTeamsShiftsClient so
the rest of the app can be built and tested against realistic-shaped
data.
"""
from __future__ import annotations

import hashlib
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import quote

from django.conf import settings

_TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


@dataclass(frozen=True)
class ShiftAssignment:
    email: str
    shift_start: datetime
    shift_end: datetime


class TeamsShiftsClient(ABC):
    @abstractmethod
    def list_shifts_for_date(self, for_date: date) -> list[ShiftAssignment]:
        """Every PUBLISHED Teams Shift (a draft/unpublished one is
        skipped — office staff can't see those in the Teams app either
        until published) whose date range overlaps this date on WGTK's
        rota Team, keyed by each rostered person's email so callers can
        match it straight to Locksmith.email."""

    @abstractmethod
    def list_shifts_for_date_range(self, start_date: date, end_date: date) -> list[ShiftAssignment]:
        """Same as list_shifts_for_date, but for every date in
        [start_date, end_date] (inclusive) in ONE call — for a caller
        that needs several different dates' shifts (e.g. Logs Engine
        needing each future-job option's own day, not just today),
        this is one Graph round trip instead of one per date, which
        matters: an unbounded/chatty shifts query is what made a
        lookup hang once already (see list_shifts_for_date's real
        implementation)."""


class MockTeamsShiftsClient(TeamsShiftsClient):
    """Deterministic fake shifts for local dev/tests, standing in until
    a real Team ID + Graph credentials are available."""

    _EMAILS = [
        "andrew.s@wgtk.co.uk", "blain.h@wgtk.co.uk", "chris.w@wgtk.co.uk",
        "dean.s@wgtk.co.uk", "james.m@wgtk.co.uk",
    ]

    # Kept for interface parity with RealTeamsShiftsClient — always
    # empty here since there's no real Graph fetch to report on.
    raw_fetch_diagnostic = ""

    def list_shifts_for_date(self, for_date: date) -> list[ShiftAssignment]:
        rng = random.Random(int(hashlib.sha256(for_date.isoformat().encode()).hexdigest(), 16) % (2**32))
        shifts = []
        for email in self._EMAILS:
            if rng.random() < 0.3:
                continue  # this one's off today
            start_hour = rng.choice([7, 8, 9])
            shifts.append(
                ShiftAssignment(
                    email=email,
                    shift_start=datetime.combine(for_date, datetime.min.time()).replace(hour=start_hour),
                    shift_end=datetime.combine(for_date, datetime.min.time()).replace(hour=start_hour + 9),
                )
            )
        return shifts

    def list_shifts_for_date_range(self, start_date: date, end_date: date) -> list[ShiftAssignment]:
        shifts = []
        current = start_date
        while current <= end_date:
            shifts.extend(self.list_shifts_for_date(current))
            current += timedelta(days=1)
        return shifts


def _parse_graph_datetime(value: str | None) -> datetime | None:
    """Graph hands back shift times as UTC ISO 8601 ("...Z") — converted
    to naive UK local time (BST-aware) here, same convention as
    apps.integrations.handl._handl_now, so a caller can compare it
    directly against a plain "now" without juggling timezones itself."""
    if not value:
        return None
    from django.utils import timezone as django_timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return django_timezone.localtime(parsed).replace(tzinfo=None)


class RealTeamsShiftsClient(TeamsShiftsClient):
    """Real Microsoft Graph-backed implementation, over the requests
    library — same app-only (client credentials) auth as
    MicrosoftGraphEmailBackend."""

    def __init__(self, *, client_id: str, client_secret: str, tenant_id: str, team_id: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._tenant_id = tenant_id
        self._team_id = team_id
        # Populated by list_shifts_for_date_range — a raw one-liner
        # covering the /groups/{team_id}/members and shifts fetches
        # themselves, separate from the higher-level "matched a known
        # locksmith" diagnostic in views.py. Confirmed live: a shift
        # fetch can come back with real Graph shift objects and still
        # produce zero usable ShiftAssignments if the SEPARATE
        # /groups/{team_id}/members call (needed to turn a Graph userId
        # into an email) itself returns nothing under this app's
        # client-credentials auth, even when the shifts themselves are
        # fetched fine — that failure mode is otherwise invisible, since
        # it looks identical to "no shifts published" from the outside.
        self.raw_fetch_diagnostic = ""

    def _get_access_token(self) -> str:
        import requests

        response = requests.post(
            _TOKEN_URL_TMPL.format(tenant_id=self._tenant_id),
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def _graph_get_all(self, url: str, token: str) -> list[dict]:
        """Follows @odata.nextLink so a large team roster or a busy
        rota's shift list doesn't silently truncate at one page."""
        import requests

        items = []
        headers = {"Authorization": f"Bearer {token}"}
        while url:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return items

    def list_shifts_for_date(self, for_date: date) -> list[ShiftAssignment]:
        return self.list_shifts_for_date_range(for_date, for_date)

    def list_shifts_for_date_range(self, start_date: date, end_date: date) -> list[ShiftAssignment]:
        token = self._get_access_token()

        # A Graph Shift object only carries userId (an Azure AD object
        # id), not an email address — resolve the team roster once
        # rather than looking each one up individually.
        members = self._graph_get_all(
            f"{_GRAPH_BASE}/groups/{self._team_id}/members?$select=id,mail,userPrincipalName",
            token,
        )
        email_by_user_id = {
            member["id"]: (member.get("mail") or member.get("userPrincipalName") or "").lower()
            for member in members
        }

        # Without a server-side date filter this fetches the team's
        # ENTIRE shift history — confirmed live to make a lookup hang
        # once WGTK ROTA had months of published shifts across ~28
        # people, paging through far more data than needed. A generous
        # +/- 1 day UTC window around the whole requested range (covers
        # any BST offset, so nothing right at the edges gets missed)
        # keeps this fast; the precise per-shift date check below is
        # still the real source of truth for what actually counts as
        # "covers this range". Fetching the whole range in one request
        # (rather than one request per date) is deliberate — the same
        # chattiness that made the original single-date version hang
        # would just as easily hang again if a caller looped this over
        # several dates instead.
        #
        # Graph's shifts $filter treats these as DateTimeOffset
        # literals, NOT string literals — quoting them (e.g. ge
        # '2026-09-14T00:00:00Z') is a type mismatch and Graph rejects
        # the whole request with 400 Bad Request. Confirmed live: this
        # exact query 400'd until the quotes were removed, matching
        # Microsoft's own documented example for this endpoint, which
        # is unquoted.
        window_start = datetime.combine(start_date - timedelta(days=1), datetime.min.time())
        window_end = datetime.combine(end_date + timedelta(days=1), datetime.min.time())
        filter_query = (
            f"sharedShift/startDateTime ge {window_start.isoformat()}Z "
            f"and sharedShift/endDateTime le {window_end.isoformat()}Z"
        )
        shifts = self._graph_get_all(
            f"{_GRAPH_BASE}/teams/{self._team_id}/schedule/shifts?$filter={quote(filter_query)}",
            token,
        )
        results = []
        skipped_unpublished = 0
        skipped_unparseable_dates = 0
        skipped_outside_window = 0
        skipped_no_member_email = 0
        for shift in shifts:
            shared = shift.get("sharedShift")
            if not shared:
                skipped_unpublished += 1
                continue
            start = _parse_graph_datetime(shared.get("startDateTime"))
            end = _parse_graph_datetime(shared.get("endDateTime"))
            if start is None or end is None:
                skipped_unparseable_dates += 1
                continue
            if start.date() > end_date or end.date() < start_date:
                skipped_outside_window += 1
                continue
            email = email_by_user_id.get(shift.get("userId"), "")
            if not email:
                skipped_no_member_email += 1
                continue
            results.append(ShiftAssignment(email=email, shift_start=start, shift_end=end))

        self.raw_fetch_diagnostic = (
            f"Graph: {len(members)} team member(s) resolved via /groups/{{team}}/members "
            f"({sum(1 for e in email_by_user_id.values() if e)} with a usable email), "
            f"{len(shifts)} raw shift(s) from the filtered query, {len(results)} kept "
            f"(skipped: {skipped_unpublished} unpublished/draft, {skipped_unparseable_dates} unparseable dates, "
            f"{skipped_outside_window} outside the requested window, "
            f"{skipped_no_member_email} whose Graph userId didn't match a team member with a known email)."
        )
        return results


def get_teams_shifts_client() -> TeamsShiftsClient:
    from .models import TeamsShiftsSettings

    team_id = TeamsShiftsSettings.current_team_id() or settings.MS_GRAPH_TEAM_ID
    if (
        team_id
        and settings.MS_GRAPH_MAIL_CLIENT_ID
        and settings.MS_GRAPH_MAIL_CLIENT_SECRET
        and settings.MS_GRAPH_MAIL_TENANT_ID
    ):
        return RealTeamsShiftsClient(
            client_id=settings.MS_GRAPH_MAIL_CLIENT_ID,
            client_secret=settings.MS_GRAPH_MAIL_CLIENT_SECRET,
            tenant_id=settings.MS_GRAPH_MAIL_TENANT_ID,
            team_id=team_id,
        )
    return MockTeamsShiftsClient()
