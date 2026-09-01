"""Read-only access to Handl.

Handl is reached via a direct, read-only database connection (confirmed
with the business) rather than an API. The real connection is configured
via HANDL_DATABASE_URL (see config/settings/base.py); until that's set,
get_handl_client() returns MockHandlClient so the rest of the app can be
built and tested against realistic-shaped data.

SQLHandlClient's methods are stubs: the actual table/column names depend
on Handl's schema, which hasn't been confirmed yet. Fill in the queries
once that's available.
"""
from __future__ import annotations

import hashlib
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, timedelta

from django.conf import settings


@dataclass(frozen=True)
class StockUsage:
    part_code: str
    part_name: str
    qty_used: int


@dataclass(frozen=True)
class ExpectedStock:
    part_code: str
    expected_qty: int
    unit_cost: float


class HandlClient(ABC):
    @abstractmethod
    def get_stock_usage(
        self, handl_engineer_id: str, since: date
    ) -> list[StockUsage]:
        """Parts used/disposed by this locksmith since `since`, for ranking fast movers."""

    @abstractmethod
    def get_expected_stock(
        self, handl_engineer_id: str, part_codes: list[str]
    ) -> dict[str, ExpectedStock]:
        """Current expected van-stock quantity and unit cost for the given part codes."""


class MockHandlClient(HandlClient):
    """Deterministic fake data for local dev/tests, standing in until the
    real Handl DB connection and schema are confirmed."""

    _CATALOGUE = [
        ("TK-100", "Transponder key blank"),
        ("TK-101", "Remote key fob"),
        ("TK-102", "Ignition barrel"),
        ("TK-103", "Door lock cylinder"),
        ("TK-104", "Key fob battery CR2032"),
        ("TK-105", "EEPROM chip"),
        ("TK-106", "Smart key shell"),
        ("TK-107", "Van lock cylinder"),
        ("TK-108", "Motorcycle key blank"),
        ("TK-109", "Padlock (van security)"),
        ("TK-110", "Steering lock repair kit"),
        ("TK-111", "Ignition switch"),
        ("TK-112", "Central locking actuator"),
        ("TK-113", "Key cutting blade"),
        ("TK-114", "Remote key case"),
        ("TK-115", "Wafer lock set"),
        ("TK-116", "Boot lock cylinder"),
        ("TK-117", "Programming lead"),
        ("TK-118", "Emergency key blade"),
        ("TK-119", "Diagnostic dongle battery"),
        ("TK-120", "Universal remote"),
        ("TK-121", "Door handle lock barrel"),
        ("TK-122", "Fuel cap lock"),
        ("TK-123", "Bike disc lock"),
        ("TK-124", "Safe lock mechanism"),
        ("TK-125", "Filing cabinet lock"),
        ("TK-126", "Mortice lock body"),
        ("TK-127", "Euro cylinder"),
        ("TK-128", "Window lock"),
        ("TK-129", "Garage door lock"),
        ("TK-130", "Caravan door lock"),
        ("TK-131", "HGV ignition barrel"),
        ("TK-132", "Plant equipment key"),
        ("TK-133", "Alarm fob"),
        ("TK-134", "Immobiliser ring"),
        ("TK-135", "Key blank (generic)"),
    ]

    def _rng_for(self, handl_engineer_id: str) -> random.Random:
        seed = int(hashlib.sha256(handl_engineer_id.encode()).hexdigest(), 16) % (2**32)
        return random.Random(seed)

    def get_stock_usage(self, handl_engineer_id: str, since: date) -> list[StockUsage]:
        rng = self._rng_for(handl_engineer_id)
        days = max((date.today() - since).days, 1)
        sample = rng.sample(self._CATALOGUE, k=min(len(self._CATALOGUE), 30))
        return [
            StockUsage(
                part_code=code,
                part_name=name,
                qty_used=rng.randint(1, max(2, days // 3)),
            )
            for code, name in sample
        ]

    def get_expected_stock(
        self, handl_engineer_id: str, part_codes: list[str]
    ) -> dict[str, ExpectedStock]:
        rng = self._rng_for(handl_engineer_id)
        result = {}
        for code in part_codes:
            name = next((n for c, n in self._CATALOGUE if c == code), code)
            result[code] = ExpectedStock(
                part_code=code,
                expected_qty=rng.randint(2, 12),
                unit_cost=round(rng.uniform(3, 85), 2),
            )
        return result


class SQLHandlClient(HandlClient):
    """Real Handl DB-backed implementation.

    TODO once the Handl schema is confirmed: fill these in using Django's
    'handl' database alias (settings.DATABASES["handl"]), e.g. via
    `django.db.connections["handl"].cursor()` and raw SQL, or unmanaged
    Django models (managed = False) pointed at the real tables. Needed:
    - the stock/parts-disposed table + how it links to an engineer,
    - the van-stock/expected-quantity table + unit cost field,
    - confirmation the engineer identifier used here matches
      Locksmith.handl_engineer_id.
    """

    def get_stock_usage(self, handl_engineer_id: str, since: date) -> list[StockUsage]:
        raise NotImplementedError(
            "SQLHandlClient.get_stock_usage: Handl schema not yet confirmed."
        )

    def get_expected_stock(
        self, handl_engineer_id: str, part_codes: list[str]
    ) -> dict[str, ExpectedStock]:
        raise NotImplementedError(
            "SQLHandlClient.get_expected_stock: Handl schema not yet confirmed."
        )


def get_handl_client() -> HandlClient:
    if "handl" in settings.DATABASES:
        return SQLHandlClient()
    return MockHandlClient()
