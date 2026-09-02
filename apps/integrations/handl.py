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


@dataclass(frozen=True)
class JobDetails:
    report_id: str
    make: str
    model: str
    year: str
    vin: str
    service_type: str
    loss_type: str
    supplied_service: str


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

    @abstractmethod
    def get_job_details(self, report_ids: list[str]) -> dict[str, JobDetails]:
        """Vehicle/claim details (make, model, year, VIN, service_type
        i.e. Lookup_KeyType, loss_type i.e. Lookup_LossEvent_Details,
        supplied_service i.e. Lookup_LocksmithSuppliedServices) for the
        given Handl ReportID values, keyed by report_id — for Area 2
        (Job Completion), which resolves an Optimo orderNo of the form
        "<ReportID>_<date>" back to Handl for these details."""

    @abstractmethod
    def get_disposed_skus(self, report_ids: list[str]) -> dict[str, list[str]]:
        """SKUs of parts disposed against each Handl ReportID (most
        recent first, capped at 10 per job), keyed by report_id — for
        Area 2 (Job Completion), same disposal data as Area 1's stock
        usage but looked up by ReportID directly rather than
        LookupLocksmithId."""


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

    _MAKES = ["Ford", "Vauxhall", "BMW", "Volkswagen", "Audi", "Mercedes-Benz", "Toyota"]
    _MODELS = ["Focus", "Corsa", "3 Series", "Golf", "A4", "C-Class", "Yaris"]
    _SERVICE_TYPES = ["Lockout", "Key cutting", "Key programming", "Barrel change", "Boot lockout"]
    _LOSS_TYPES = ["Lost Keys", "Broken Key", "Lockout", "Keys Locked In", "Stolen Keys"]
    _SUPPLIED_SERVICES = [
        "Non-Destructive Entry", "Key Cutting", "Key Programming",
        "Lock Change", "Boot Entry",
    ]

    def get_job_details(self, report_ids: list[str]) -> dict[str, JobDetails]:
        result = {}
        for report_id in report_ids:
            rng = random.Random(int(hashlib.sha256(report_id.encode()).hexdigest(), 16) % (2**32))
            result[report_id] = JobDetails(
                report_id=report_id,
                make=rng.choice(self._MAKES),
                model=rng.choice(self._MODELS),
                year=str(rng.randint(2008, 2025)),
                vin=f"MOCK{rng.randint(10**12, 10**13 - 1)}",
                service_type=rng.choice(self._SERVICE_TYPES),
                loss_type=rng.choice(self._LOSS_TYPES),
                supplied_service=rng.choice(self._SUPPLIED_SERVICES),
            )
        return result

    def get_disposed_skus(self, report_ids: list[str]) -> dict[str, list[str]]:
        result = {}
        for report_id in report_ids:
            rng = random.Random(int(hashlib.sha256(report_id.encode()).hexdigest(), 16) % (2**32))
            count = rng.randint(0, 4)
            sample = rng.sample(self._CATALOGUE, k=min(len(self._CATALOGUE), count))
            result[report_id] = [code for code, _name in sample]
        return result


class SQLHandlClient(HandlClient):
    """Real Soter (Handl) DB-backed implementation, over pymssql.

    Usage comes from Inventory_Disposals (has LookupLocksmithId
    directly); expected stock from Inventory_Locksmith_Stock, the same
    table the business's own "current van stock" Excel report is built
    from. Both are summed across a locksmith's Soter ID(s) — Soter
    tracks "(V)" and "(A)" as separate stock locations for one physical
    person. Unit cost is Inventory_Stock's PartValue/Quantity (cost
    basis — PartValue is a batch *total*, not a per-unit price) for the
    most recently *priced* batch, rather than Inventory_Parts's
    RecommendedRetailPrice (sell price) or an average across all
    history.
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
        # Inventory_Stock.PartValue is the *batch total*, not a
        # per-unit price — confirmed on real data: a CR2032 batch with
        # Quantity=37, PartValue=90.65 gives 90.65/37 = £2.45, exactly
        # matching that part's real "last purchased cost per unit" on
        # Soter's own Suppliers page. So unit cost is PartValue/Quantity
        # for the most recently *priced* batch (not an average across
        # all history — that skewed badly, e.g. CR2032 averaged £13.13
        # — and not just the most recent row by date either, since many
        # rows are periodic recount/adjustment noise with
        # Quantity=0, PartValue=0 and no real price).
        cost_query = f"""
            SELECT part_code, unit_cost FROM (
                SELECT
                    ipa.SKU AS part_code,
                    ist.PartValue / ist.Quantity AS unit_cost,
                    ROW_NUMBER() OVER (
                        PARTITION BY ipa.SKU ORDER BY ist.DateCreated DESC
                    ) AS rn
                FROM Inventory_Stock ist
                JOIN Inventory_Parts ipa ON ist.PartId = ipa.Id
                WHERE ipa.SKU IN ({code_placeholders})
                  AND ist.PartValue > 0
                  AND ist.Quantity > 0
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

    def get_job_details(self, report_ids: list[str]) -> dict[str, JobDetails]:
        if not report_ids:
            return {}
        id_placeholders = ", ".join(f"%(rid{i})s" for i in range(len(report_ids)))
        params = {f"rid{i}": rid for i, rid in enumerate(report_ids)}
        # Confirmed live: Make/Model/yearOfManufacture/VehicleVIN live on
        # Policy_KeyClaims (not Policy_Details, which has no vehicle
        # fields at all). KeyTypeID/Lookup_KeyType ("Car", "Van", ...) is
        # broad; loss_type (Policy_Details.LossEventID ->
        # Lookup_LossEvent_Details) and supplied_service
        # (Policy_LocksmithDetails' selected row ->
        # LocksmithSuppliedServicesIds's last id ->
        # Lookup_LocksmithSuppliedServices) are the more specific
        # "what actually happened" fields, per the business's own
        # reporting query. A ReportID can have more than one
        # Policy_KeyClaims/Policy_LocksmithDetails row, so both are
        # ranked down to one (earliest key claim by ID; most recent
        # selected locksmith-details row by ID).
        query = f"""
            WITH VehicleRanked AS (
                SELECT
                    pkc.ReportID,
                    pkc.Make,
                    pkc.Model,
                    pkc.yearOfManufacture,
                    pkc.VehicleVIN,
                    lkt.KeyType,
                    ROW_NUMBER() OVER (PARTITION BY pkc.ReportID ORDER BY pkc.ID) AS rn
                FROM Policy_KeyClaims pkc
                LEFT JOIN Lookup_KeyType lkt ON pkc.KeyTypeID = lkt.KeyTypeID
                WHERE pkc.ReportID IN ({id_placeholders})
            ),
            LossType AS (
                SELECT p.ReportID, lle.LossEvent
                FROM Policy_Details p
                LEFT JOIN Lookup_LossEvent_Details lle ON p.LossEventID = lle.LossEventID
                WHERE p.ReportID IN ({id_placeholders})
            ),
            SuppliedServiceRanked AS (
                SELECT
                    pld.ReportID,
                    llss.Service AS SuppliedService,
                    ROW_NUMBER() OVER (PARTITION BY pld.ReportID ORDER BY pld.ID DESC) AS rn
                FROM Policy_LocksmithDetails pld
                LEFT JOIN Lookup_LocksmithSuppliedServices llss
                    ON llss.ID = CASE
                        WHEN pld.LocksmithSuppliedServicesIds IS NOT NULL
                        THEN TRY_CAST(
                            REVERSE(
                                PARSENAME(
                                    REPLACE(REVERSE(pld.LocksmithSuppliedServicesIds), ',', '.'),
                                    1
                                )
                            ) AS INT
                        )
                        ELSE NULL
                    END
                WHERE pld.Selected = 1 AND pld.ReportID IN ({id_placeholders})
            )
            SELECT
                v.ReportID, v.Make, v.Model, v.yearOfManufacture, v.VehicleVIN, v.KeyType,
                lt.LossEvent, ss.SuppliedService
            FROM VehicleRanked v
            LEFT JOIN LossType lt ON v.ReportID = lt.ReportID
            LEFT JOIN SuppliedServiceRanked ss ON v.ReportID = ss.ReportID AND ss.rn = 1
            WHERE v.rn = 1
        """
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
        return {
            str(row["ReportID"]): JobDetails(
                report_id=str(row["ReportID"]),
                make=row["Make"] or "",
                model=row["Model"] or "",
                year=str(row["yearOfManufacture"] or ""),
                vin=row["VehicleVIN"] or "",
                service_type=row["KeyType"] or "",
                loss_type=row["LossEvent"] or "",
                supplied_service=row["SuppliedService"] or "",
            )
            for row in rows
        }

    def get_disposed_skus(self, report_ids: list[str]) -> dict[str, list[str]]:
        if not report_ids:
            return {}
        id_placeholders = ", ".join(f"%(rid{i})s" for i in range(len(report_ids)))
        params = {f"rid{i}": rid for i, rid in enumerate(report_ids)}
        # Inventory_Disposals.ReportID links a disposal directly to the
        # claim it was used on. Most recent 10 per ReportID, matching
        # the business's own reporting pattern for this — earlier
        # batches are dropped rather than risking an unbounded list.
        query = f"""
            SELECT ReportID, SKU FROM (
                SELECT
                    idp.ReportID,
                    ipa.SKU,
                    ROW_NUMBER() OVER (
                        PARTITION BY idp.ReportID
                        ORDER BY idp.DateCreated DESC, idp.Id DESC
                    ) AS rn
                FROM Inventory_Disposals idp
                JOIN Inventory_Stock ist ON idp.StockId = ist.Id
                JOIN Inventory_Parts ipa ON ist.PartId = ipa.Id
                WHERE idp.ReportID IN ({id_placeholders})
            ) ranked
            WHERE rn <= 10
            ORDER BY ReportID, rn
        """
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            report_id = str(row["ReportID"])
            result.setdefault(report_id, []).append(row["SKU"])
        return result


def get_handl_client() -> HandlClient:
    if settings.HANDL_SQL_SERVER:
        return SQLHandlClient()
    return MockHandlClient()
