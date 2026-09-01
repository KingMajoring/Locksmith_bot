"""Read-only access to Handl (internally 'Soter'), an Azure SQL database.

Reached via a direct, read-only SQL connection (confirmed with the
business) rather than an API, using pymssql — a pure-Python driver, so it
works on the standard Azure App Service Python runtime without needing a
custom container image just to get the Microsoft ODBC driver installed.

Connection settings (HANDL_SQL_*, see config/settings/base.py) are read
from Key Vault references in production, not plain app settings. Until
HANDL_SQL_SERVER is set, get_handl_client() returns MockHandlClient so
the rest of the app can be built and tested against realistic-shaped
data.

A WGTK locksmith maps to one or two Soter Lookup_Locksmiths.ID values
(Locksmith.soter_id_list — usually a "(V)" and "(A)" row for the same
person), whose stock/usage gets summed together.
"""
from __future__ import annotations

import hashlib
import random
from abc import ABC, abstractmethod
from contextlib import contextmanager
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
        self, soter_locksmith_ids: list[str], since: date
    ) -> list[StockUsage]:
        """Parts used/disposed by this locksmith since `since`, for ranking fast movers."""

    @abstractmethod
    def get_expected_stock(
        self, soter_locksmith_ids: list[str], part_codes: list[str]
    ) -> dict[str, ExpectedStock]:
        """Current expected van-stock quantity and unit cost for the given part codes."""

    @abstractmethod
    def list_locksmiths(self) -> list[tuple[str, str, str]]:
        """(ID, LocksmithName, EmailAddress) for every active WGTK
        locksmith (WGTKLocksmith=1, isDeleted=0 in Lookup_Locksmiths) —
        excludes panel/subcontractor firms and soft-deleted rows."""


class MockHandlClient(HandlClient):
    """Deterministic fake data for local dev/tests, standing in until the
    real Soter DB connection is available (e.g. no HANDL_SQL_SERVER set)."""

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

    def _rng_for(self, soter_locksmith_ids: list[str]) -> random.Random:
        key = ",".join(sorted(soter_locksmith_ids))
        seed = int(hashlib.sha256(key.encode()).hexdigest(), 16) % (2**32)
        return random.Random(seed)

    def get_stock_usage(
        self, soter_locksmith_ids: list[str], since: date
    ) -> list[StockUsage]:
        rng = self._rng_for(soter_locksmith_ids)
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
        self, soter_locksmith_ids: list[str], part_codes: list[str]
    ) -> dict[str, ExpectedStock]:
        rng = self._rng_for(soter_locksmith_ids)
        result = {}
        for code in part_codes:
            name = next((n for c, n in self._CATALOGUE if c == code), code)
            result[code] = ExpectedStock(
                part_code=code,
                expected_qty=rng.randint(2, 12),
                unit_cost=round(rng.uniform(3, 85), 2),
            )
        return result

    def list_locksmiths(self) -> list[tuple[str, str, str]]:
        # Mirrors what the real SQL query already returns: only active
        # WGTK-flagged, non-deleted rows — panel firms and ex-staff
        # never come back from Soter in the first place.
        return [
            ("1204", "WGTK - Andrew S", "andrew.s@wgtk.co.uk"),
            ("887", "WGTK - Dean S (A)", "dean.s@wgtk.co.uk"),
            ("885", "WGTK - Dean S (V)", "dean.s@wgtk.co.uk"),
            ("999", "WGTK - BCA", ""),
        ]


class SQLHandlClient(HandlClient):
    """Real Soter (Handl) DB-backed implementation, over pymssql.

    Usage comes from Inventory_Disposals (has LookupLocksmithId
    directly); expected stock from Inventory_Locksmith_Stock, the same
    table the business's own "current van stock" Excel report is built
    from. Both are summed across a locksmith's Soter ID(s) — Soter
    tracks "(V)" and "(A)" as separate stock locations for one physical
    person. Unit cost is Inventory_Stock's PartValue (cost basis) rather
    than Inventory_Parts.RecommendedRetailPrice (sell price), taken from
    that part's most recently received batch rather than averaged
    across all of them.
    """

    @contextmanager
    def _connection(self):
        import pymssql

        conn = pymssql.connect(
            server=settings.HANDL_SQL_SERVER,
            port=settings.HANDL_SQL_PORT,
            database=settings.HANDL_SQL_DATABASE,
            user=settings.HANDL_SQL_USER,
            password=settings.HANDL_SQL_PASSWORD,
            as_dict=True,
        )
        try:
            yield conn
        finally:
            conn.close()

    def get_stock_usage(
        self, soter_locksmith_ids: list[str], since: date
    ) -> list[StockUsage]:
        id_placeholders = ", ".join(f"%(lid{i})s" for i in range(len(soter_locksmith_ids)))
        query = f"""
            SELECT
                ip.SKU AS part_code,
                ip.Name AS part_name,
                SUM(idp.Quantity) AS qty_used
            FROM Inventory_Disposals idp
            JOIN Inventory_Stock ist ON idp.StockId = ist.Id
            JOIN Inventory_Parts ip ON ist.PartId = ip.Id
            WHERE idp.LookupLocksmithId IN ({id_placeholders})
              AND idp.DateCreated >= %(since)s
            GROUP BY ip.SKU, ip.Name
            HAVING SUM(idp.Quantity) > 0
            ORDER BY qty_used DESC
        """
        params = {f"lid{i}": int(lid) for i, lid in enumerate(soter_locksmith_ids)}
        params["since"] = since
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
        return [
            StockUsage(
                part_code=row["part_code"],
                part_name=row["part_name"],
                qty_used=row["qty_used"],
            )
            for row in rows
        ]

    def get_expected_stock(
        self, soter_locksmith_ids: list[str], part_codes: list[str]
    ) -> dict[str, ExpectedStock]:
        if not part_codes:
            return {}
        id_placeholders = ", ".join(f"%(lid{i})s" for i in range(len(soter_locksmith_ids)))
        code_placeholders = ", ".join(f"%(code{i})s" for i in range(len(part_codes)))
        params = {f"lid{i}": int(lid) for i, lid in enumerate(soter_locksmith_ids)}
        params.update({f"code{i}": code for i, code in enumerate(part_codes)})

        # Two separate queries, not one joined together: Inventory_Stock
        # is a company-wide batch table with no locksmith scoping, so
        # joining it onto Inventory_Locksmith_Stock via PartId alone
        # fans out — each ils row gets multiplied by every batch that
        # part has ever had, inflating SUM(ils.Quantity) wildly.
        qty_query = f"""
            SELECT ipa.SKU AS part_code, SUM(ils.Quantity) AS expected_qty
            FROM Inventory_Locksmith_Stock ils
            JOIN Inventory_Parts ipa ON ils.PartId = ipa.Id
            WHERE ils.LookupLocksmithId IN ({id_placeholders})
              AND ipa.SKU IN ({code_placeholders})
            GROUP BY ipa.SKU
        """
        # Most recently *priced* batch's PartValue — not an average
        # across all history (confirmed on real data: CR2032 averaged
        # £13.13 vs £0.25-£2.45 actually seen across its real
        # suppliers), and not just the most recent row by date either
        # (also confirmed on real data: that returned PartValue=0/NULL
        # for several parts — Inventory_Stock holds non-purchase
        # movements too, e.g. recounts/adjustments, with no real price).
        # Excluding those before ranking gets the true last-purchased
        # price, matching how Soter's own UI shows "last purchased cost
        # per unit" per supplier.
        cost_query = f"""
            SELECT part_code, unit_cost FROM (
                SELECT
                    ipa.SKU AS part_code,
                    ist.PartValue AS unit_cost,
                    ROW_NUMBER() OVER (
                        PARTITION BY ipa.SKU ORDER BY ist.DateCreated DESC
                    ) AS rn
                FROM Inventory_Stock ist
                JOIN Inventory_Parts ipa ON ist.PartId = ipa.Id
                WHERE ipa.SKU IN ({code_placeholders})
                  AND ist.PartValue > 0
            ) ranked
            WHERE rn = 1
        """
        cost_params = {f"code{i}": code for i, code in enumerate(part_codes)}

        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(qty_query, params)
            qty_rows = cursor.fetchall()
            cursor.execute(cost_query, cost_params)
            cost_rows = cursor.fetchall()

        unit_costs = {row["part_code"]: float(row["unit_cost"] or 0) for row in cost_rows}
        return {
            row["part_code"]: ExpectedStock(
                part_code=row["part_code"],
                expected_qty=row["expected_qty"],
                unit_cost=unit_costs.get(row["part_code"], 0),
            )
            for row in qty_rows
        }

    def list_locksmiths(self) -> list[tuple[str, str, str]]:
        # WGTKLocksmith=1 identifies WGTK's own staff vs panel/
        # subcontractor firms (also everything else in this table);
        # isDeleted=0 excludes soft-deleted rows. Not filtering on
        # Active — its exact meaning (currently employed? on shift
        # today?) isn't confirmed, and excluding on a guess risks
        # silently dropping someone who should still get stock checks.
        query = """
            SELECT ID, LocksmithName, EmailAddress
            FROM Lookup_Locksmiths
            WHERE WGTKLocksmith = 1 AND isDeleted = 0
            ORDER BY LocksmithName
        """
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
        return [
            (str(row["ID"]), row["LocksmithName"] or "", row["EmailAddress"] or "")
            for row in rows
        ]


def get_handl_client() -> HandlClient:
    if settings.HANDL_SQL_SERVER:
        return SQLHandlClient()
    return MockHandlClient()
