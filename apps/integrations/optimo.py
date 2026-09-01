"""Access to the OptimoRoute Web Service API, for Area 2 (Job Completion).

Two calls combine to give one day's completed jobs:

- search_orders (dateRange + includeScheduleInformation) — which orders
  ran that day, which driver, and travel distance/time to the stop.
- get_completion_details (batched by orderNo) — the actual outcome:
  status ("success"/"failed"/"scheduled"/...), real on-site start/end
  times, and the driver's free-text note on failure.

Until OPTIMO_API_KEY is set, get_optimo_client() returns
MockOptimoClient so the rest of the app can be built and tested against
realistic-shaped data.
"""
from __future__ import annotations

import hashlib
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone as dt_timezone

from django.conf import settings


@dataclass(frozen=True)
class OptimoOrderSummary:
    order_no: str
    driver_serial: str
    distance_metres: float
    travel_time_seconds: int


@dataclass(frozen=True)
class OptimoCompletion:
    order_no: str
    status: str
    start_time: datetime | None
    end_time: datetime | None
    note: str


class OptimoClient(ABC):
    @abstractmethod
    def list_orders_for_date(self, for_date: date) -> list[OptimoOrderSummary]:
        """Every order scheduled on this date, with which driver and the
        travel distance/time to reach it — via search_orders."""

    @abstractmethod
    def get_completion_details(self, order_nos: list[str]) -> dict[str, OptimoCompletion]:
        """Outcome (status, on-site start/end time, failure note) for the
        given orderNo values, keyed by order_no — via get_completion_details.
        Orders not yet completed come back with status "scheduled" (or
        similar) and no start/end times."""


class MockOptimoClient(OptimoClient):
    """Deterministic fake data for local dev/tests, standing in until a
    real OPTIMO_API_KEY is available."""

    _DRIVER_SERIALS = ["011", "023", "045", "102"]

    def _rng_for(self, for_date: date) -> random.Random:
        seed = int(hashlib.sha256(for_date.isoformat().encode()).hexdigest(), 16) % (2**32)
        return random.Random(seed)

    def list_orders_for_date(self, for_date: date) -> list[OptimoOrderSummary]:
        rng = self._rng_for(for_date)
        count = rng.randint(15, 25)
        return [
            OptimoOrderSummary(
                order_no=f"{40000 + i}_{for_date.isoformat()}",
                driver_serial=rng.choice(self._DRIVER_SERIALS),
                distance_metres=round(rng.uniform(500, 25000), 1),
                travel_time_seconds=rng.randint(60, 2400),
            )
            for i in range(count)
        ]

    def get_completion_details(self, order_nos: list[str]) -> dict[str, OptimoCompletion]:
        result = {}
        for order_no in order_nos:
            rng = random.Random(int(hashlib.sha256(order_no.encode()).hexdigest(), 16) % (2**32))
            status = rng.choices(["success", "failed"], weights=[85, 15])[0]
            start = datetime(
                2026, 1, 1, rng.randint(8, 16), rng.randint(0, 59), tzinfo=dt_timezone.utc
            )
            duration_minutes = rng.randint(15, 90)
            end = start.replace(minute=(start.minute + duration_minutes) % 60)
            note = (
                ""
                if status == "success"
                else rng.choice(
                    [
                        "Customer not available",
                        "Wrong parts on van",
                        "Access issue at property",
                        "Vehicle already opened by another party",
                    ]
                )
            )
            result[order_no] = OptimoCompletion(
                order_no=order_no,
                status=status,
                start_time=start,
                end_time=end,
                note=note,
            )
        return result


class RealOptimoClient(OptimoClient):
    """Real OptimoRoute API-backed implementation, over the requests library."""

    _BASE_URL = "https://api.optimoroute.com/v1"

    def _get(self, path: str, params: dict) -> dict:
        import requests

        params = {**params, "key": settings.OPTIMO_API_KEY}
        response = requests.get(f"{self._BASE_URL}/{path}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, body: dict) -> dict:
        import requests

        response = requests.post(
            f"{self._BASE_URL}/{path}",
            params={"key": settings.OPTIMO_API_KEY},
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def list_orders_for_date(self, for_date: date) -> list[OptimoOrderSummary]:
        date_str = for_date.isoformat()
        data = self._post(
            "search_orders",
            {
                "dateRange": {"from": date_str, "to": date_str},
                "includeScheduleInformation": True,
            },
        )
        summaries = []
        for order in data.get("orders", []):
            schedule = order.get("scheduleInformation")
            if not schedule:
                continue
            order_no = order.get("data", {}).get("orderNo") or order.get("orderNo")
            if not order_no:
                continue
            summaries.append(
                OptimoOrderSummary(
                    order_no=order_no,
                    driver_serial=schedule.get("driverSerial", ""),
                    distance_metres=float(schedule.get("distance") or 0),
                    travel_time_seconds=int(schedule.get("travelTime") or 0),
                )
            )
        return summaries

    def get_completion_details(self, order_nos: list[str]) -> dict[str, OptimoCompletion]:
        if not order_nos:
            return {}
        data = self._post(
            "get_completion_details",
            {"orders": [{"orderNo": order_no} for order_no in order_nos]},
        )
        result = {}
        for entry in data.get("orders", []):
            if not entry.get("success"):
                continue
            order_no = entry.get("orderNo")
            details = entry.get("data", {})
            result[order_no] = OptimoCompletion(
                order_no=order_no,
                status=details.get("status", ""),
                start_time=_parse_optimo_time(details.get("startTime")),
                end_time=_parse_optimo_time(details.get("endTime")),
                note=(details.get("form") or {}).get("note", ""),
            )
        return result


def _parse_optimo_time(time_obj: dict | None) -> datetime | None:
    if not time_obj or "utcTime" not in time_obj:
        return None
    return datetime.fromisoformat(time_obj["utcTime"]).replace(tzinfo=dt_timezone.utc)


def get_optimo_client() -> OptimoClient:
    if settings.OPTIMO_API_KEY:
        return RealOptimoClient()
    return MockOptimoClient()
