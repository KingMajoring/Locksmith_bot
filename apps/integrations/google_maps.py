"""Access to the Google Distance Matrix API, for Logs Engine (office job
lookup) — how far each active WGTK locksmith's home postcode is from a
job's vehicle location, to suggest a nearby one.

One call gives distance/time from every locksmith postcode (origins) to
one job location (destination, its lat/lng — more exact than a geocoded
address string). Distance Matrix, not the newer Routes API — origins vs
one destination is exactly its shape, and it needs no request-body
migration the way Routes would.

Until an API key is set (via the admin's Google Maps API settings — see
GoogleMapsSettings in models.py — or the GOOGLE_MAPS_API_KEY app setting
as a fallback), get_google_maps_client() returns MockGoogleMapsClient so
the rest of the app can be built and tested against realistic-shaped
data.
"""
from __future__ import annotations

import hashlib
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class LocksmithDistance:
    origin: str
    distance_metres: float | None
    duration_seconds: int | None
    status: str

    @property
    def distance_miles(self) -> float | None:
        return round(self.distance_metres / 1609.344, 1) if self.distance_metres is not None else None

    @property
    def duration_minutes(self) -> int | None:
        return round(self.duration_seconds / 60) if self.duration_seconds is not None else None


class GoogleMapsClient(ABC):
    @abstractmethod
    def get_distances(
        self, origins: list[str], destination_lat: float, destination_lng: float
    ) -> list[LocksmithDistance]:
        """Driving distance/time from each origin (a postcode or free-text
        address) to one destination coordinate, in the same order as
        origins — via the Distance Matrix API. An origin Google can't
        resolve (bad postcode, no route) comes back with its own
        per-element status and null distance/duration rather than
        failing the whole batch."""


class MockGoogleMapsClient(GoogleMapsClient):
    """Deterministic fake distances for local dev/tests, standing in
    until a real GOOGLE_MAPS_API_KEY is available."""

    def get_distances(
        self, origins: list[str], destination_lat: float, destination_lng: float
    ) -> list[LocksmithDistance]:
        results = []
        for origin in origins:
            seed = f"{origin}:{destination_lat}:{destination_lng}"
            rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest(), 16) % (2**32))
            distance_metres = round(rng.uniform(800, 40000), 1)
            results.append(LocksmithDistance(
                origin=origin,
                distance_metres=distance_metres,
                duration_seconds=round(distance_metres / rng.uniform(8, 14)),  # ~18-31mph avg
                status="OK",
            ))
        return results


class RealGoogleMapsClient(GoogleMapsClient):
    """Real Google Distance Matrix API-backed implementation, over the
    requests library."""

    _BASE_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
    # Distance Matrix rejects a request with more than 25 origins (or
    # destinations) in one call with MAX_DIMENSIONS_EXCEEDED — confirmed
    # live: Logs Engine sends one origin per locksmith (two for anyone
    # with both a home location and an upcoming future job), which
    # crossed 25 the moment enough locksmiths had a home location set,
    # and every result came back empty as a result. Batching keeps each
    # request under that limit and stitches the results back together
    # in the caller's original order.
    _MAX_ORIGINS_PER_REQUEST = 25

    def __init__(self, api_key: str):
        self._api_key = api_key

    def get_distances(
        self, origins: list[str], destination_lat: float, destination_lng: float
    ) -> list[LocksmithDistance]:
        if not origins:
            return []
        results = []
        for start in range(0, len(origins), self._MAX_ORIGINS_PER_REQUEST):
            batch = origins[start:start + self._MAX_ORIGINS_PER_REQUEST]
            results.extend(self._fetch_batch(batch, destination_lat, destination_lng))
        return results

    def _fetch_batch(
        self, origins: list[str], destination_lat: float, destination_lng: float
    ) -> list[LocksmithDistance]:
        import requests

        response = requests.get(
            self._BASE_URL,
            params={
                "origins": "|".join(origins),
                "destinations": f"{destination_lat},{destination_lng}",
                "mode": "driving",
                "key": self._api_key,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "OK":
            raise ValueError(f"Distance Matrix request failed: {data.get('status')} — {data.get('error_message', '')}")

        rows = data.get("rows", [])
        results = []
        for origin, row in zip(origins, rows):
            elements = row.get("elements") or []
            element = elements[0] if elements else {}
            status = element.get("status", "UNKNOWN")
            distance = element.get("distance") or {}
            duration = element.get("duration") or {}
            results.append(LocksmithDistance(
                origin=origin,
                distance_metres=float(distance["value"]) if "value" in distance else None,
                duration_seconds=int(duration["value"]) if "value" in duration else None,
                status=status,
            ))
        return results


def _resolve_api_key() -> str:
    # The API key is normally set via the admin (GoogleMapsSettings) so
    # it can be rotated without a redeploy; GOOGLE_MAPS_API_KEY (an app
    # setting) is only a fallback for initial bootstrapping.
    from .models import GoogleMapsSettings

    return GoogleMapsSettings.current_key() or settings.GOOGLE_MAPS_API_KEY


def get_google_maps_client() -> GoogleMapsClient:
    api_key = _resolve_api_key()
    if api_key:
        return RealGoogleMapsClient(api_key)
    return MockGoogleMapsClient()


def static_map_url(markers: list[tuple[str, list[str]]], *, size: str = "400x300") -> str:
    """A Google Static Maps API image URL for an at-a-glance hover
    preview — no JavaScript Maps SDK needed, just an <img src>. markers
    is a list of (style, locations) pairs, each becoming one `markers=`
    param — e.g. [("color:blue|label:J", ["52.63,1.30"]),
    ("color:red", ["NR1 1AA", "IP1 2AB"])] — a location can be a
    "lat,lng" pair or a free-text address/postcode (Google geocodes it
    server-side when rendering the image, same as Distance Matrix
    origins). Returns "" (nothing to render) when there's no API key
    configured or no markers to plot — this API needs the SAME key as
    Distance Matrix (Static Maps just has to also be enabled for it in
    Google Cloud console), but note it's used from an <img src> in the
    page HTML rather than a server-side call, so unlike the Distance
    Matrix key this one is visible to anyone who views the page source
    — restrict it by HTTP referrer to this app's own domain in Google
    Cloud console instead of relying on it being obscure."""
    api_key = _resolve_api_key()
    marker_params = [(style, locations) for style, locations in markers if locations]
    if not api_key or not marker_params:
        return ""
    from urllib.parse import urlencode

    params = [("size", size)]
    for style, locations in marker_params:
        params.append(("markers", f"{style}|" + "|".join(locations)))
    params.append(("key", api_key))
    return "https://maps.googleapis.com/maps/api/staticmap?" + urlencode(params)
