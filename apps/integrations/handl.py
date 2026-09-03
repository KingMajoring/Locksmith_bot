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
class CurrentStockLine:
    part_code: str
    part_name: str
    qty: int


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
    net_cost: float | None


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
        supplied_service i.e. Lookup_LocksmithSuppliedServices, net_cost
        i.e. Policy_Financial.NetCost — what the client was charged,
        excl. VAT, the business's own "selling price" figure for a job)
        for the given Handl ReportID values, keyed by report_id — for
        Area 2 (Job Completion), which resolves an Optimo orderNo of the
        form "<ReportID>_<date>" back to Handl for these details."""

    @abstractmethod
    def get_disposed_skus(self, report_ids: list[str]) -> dict[str, list[str]]:
        """SKUs of parts disposed against each Handl ReportID (most
        recent first, capped at 10 per job), keyed by report_id — for
        Area 2 (Job Completion), same disposal data as Area 1's stock
        usage but looked up by ReportID directly rather than
        LookupLocksmithId."""

    @abstractmethod
    def get_part_costs(self, skus: list[str]) -> dict[str, float]:
        """Unit cost per SKU, keyed by SKU — same cost basis as Area 1's
        get_expected_stock (Inventory_Stock's PartValue/Quantity for the
        most recently priced batch), but not scoped to a locksmith, for
        costing up a job's disposed_skus in Area 2."""

    @abstractmethod
    def list_current_stock(self, soter_locksmith_ids: list[str]) -> list[CurrentStockLine]:
        """Every part this locksmith currently has van stock of (qty > 0),
        for the locksmith portal's "what can I dispose" picker — unlike
        get_expected_stock, doesn't need the caller to already know which
        part codes to ask about."""

    @abstractmethod
    def record_disposal(
        self, soter_locksmith_id: str, report_id: str, part_code: str, quantity: int
    ) -> None:
        """Record a part disposed against a job, for the locksmith portal,
        and decrement that locksmith's recorded van stock accordingly.
        WRITES to Handl — uses a separate write-capable connection (see
        HANDL_SQL_WRITE_USER/PASSWORD) since the main HANDL_SQL_* creds
        are deliberately read-only. Full Inventory_Disposals schema and
        the Inventory_Locksmith_Stock decrement behaviour were confirmed
        via a supervised manual test — see SQLHandlClient's
        implementation for the details. Raises if
        HANDL_PORTAL_CREATED_BY_USER_ID isn't configured, or if there
        isn't enough recorded stock to satisfy the disposal."""


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
                net_cost=round(rng.uniform(60, 350), 2),
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

    def get_part_costs(self, skus: list[str]) -> dict[str, float]:
        result = {}
        for sku in skus:
            rng = random.Random(int(hashlib.sha256(sku.encode()).hexdigest(), 16) % (2**32))
            result[sku] = round(rng.uniform(3, 85), 2)
        return result

    def list_current_stock(self, soter_locksmith_ids: list[str]) -> list[CurrentStockLine]:
        rng = self._rng_for(soter_locksmith_ids)
        sample = rng.sample(self._CATALOGUE, k=min(len(self._CATALOGUE), 12))
        return [
            CurrentStockLine(part_code=code, part_name=name, qty=rng.randint(1, 8))
            for code, name in sample
        ]

    def record_disposal(
        self, soter_locksmith_id: str, report_id: str, part_code: str, quantity: int
    ) -> None:
        pass


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

    @contextmanager
    def _write_connection(self):
        # The main HANDL_SQL_* credentials are deliberately read-only
        # (confirmed with the business — see this module's docstring).
        # record_disposal() is the one place this app writes to Handl,
        # so it uses a separate write-capable credential instead of
        # widening the main connection's permissions.
        import pymssql

        conn = pymssql.connect(
            server=settings.HANDL_SQL_SERVER,
            port=settings.HANDL_SQL_PORT,
            database=settings.HANDL_SQL_DATABASE,
            user=settings.HANDL_SQL_WRITE_USER,
            password=settings.HANDL_SQL_WRITE_PASSWORD,
            as_dict=True,
        )
        try:
            yield conn
        finally:
            conn.close()

    def _fetch_unit_costs(self, cursor, skus: list[str]) -> dict[str, float]:
        """Shared by get_expected_stock and get_part_costs — same cost
        basis (Inventory_Stock's PartValue/Quantity for the most
        recently *priced* batch, see the class docstring), unscoped to
        any locksmith since a part's cost isn't locksmith-specific."""
        if not skus:
            return {}
        code_placeholders = ", ".join(f"%(code{i})s" for i in range(len(skus)))
        params = {f"code{i}": code for i, code in enumerate(skus)}
        query = f"""
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
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return {row["part_code"]: float(row["unit_cost"] or 0) for row in rows}

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
        # Quantity=0, PartValue=0 and no real price). See
        # _fetch_unit_costs for the actual query, shared with
        # get_part_costs below.
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(qty_query, params)
            qty_rows = cursor.fetchall()
            unit_costs = self._fetch_unit_costs(cursor, part_codes)

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
            ),
            Finance AS (
                -- What the client was charged (excl. VAT) — the
                -- business's own "selling price" figure for margin
                -- reporting, per their reporting query. Summed since a
                -- ReportID can have more than one Policy_Financial row
                -- (e.g. amendments).
                SELECT pf.ReportID, SUM(pf.NetCost) AS NetCost
                FROM Policy_Financial pf
                WHERE pf.ReportID IN ({id_placeholders})
                GROUP BY pf.ReportID
            )
            SELECT
                v.ReportID, v.Make, v.Model, v.yearOfManufacture, v.VehicleVIN, v.KeyType,
                lt.LossEvent, ss.SuppliedService, f.NetCost
            FROM VehicleRanked v
            LEFT JOIN LossType lt ON v.ReportID = lt.ReportID
            LEFT JOIN SuppliedServiceRanked ss ON v.ReportID = ss.ReportID AND ss.rn = 1
            LEFT JOIN Finance f ON v.ReportID = f.ReportID
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
                net_cost=float(row["NetCost"]) if row["NetCost"] is not None else None,
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

    def get_part_costs(self, skus: list[str]) -> dict[str, float]:
        if not skus:
            return {}
        with self._connection() as conn:
            cursor = conn.cursor()
            return self._fetch_unit_costs(cursor, skus)

    def list_current_stock(self, soter_locksmith_ids: list[str]) -> list[CurrentStockLine]:
        if not soter_locksmith_ids:
            return []
        id_placeholders = ", ".join(f"%(lid{i})s" for i in range(len(soter_locksmith_ids)))
        params = {f"lid{i}": int(lid) for i, lid in enumerate(soter_locksmith_ids)}
        # Same table/grouping as get_expected_stock's qty_query, but with
        # no SKU filter — the locksmith portal needs "everything they
        # currently have", not stock for a pre-known list of parts.
        query = f"""
            SELECT ipa.SKU AS part_code, ipa.Name AS part_name, SUM(ils.Quantity) AS qty
            FROM Inventory_Locksmith_Stock ils
            JOIN Inventory_Parts ipa ON ils.PartId = ipa.Id
            WHERE ils.LookupLocksmithId IN ({id_placeholders})
            GROUP BY ipa.SKU, ipa.Name
            HAVING SUM(ils.Quantity) > 0
            ORDER BY ipa.Name
        """
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
        return [
            CurrentStockLine(part_code=row["part_code"], part_name=row["part_name"] or "", qty=row["qty"])
            for row in rows
        ]

    def record_disposal(
        self, soter_locksmith_id: str, report_id: str, part_code: str, quantity: int
    ) -> None:
        # soter_locksmith_id is deliberately the locksmith's "(V)" (van)
        # id (see Locksmith.van_soter_id) — a disposal is consumed from
        # physical van stock. Known gap: list_current_stock sums a
        # locksmith's stock across BOTH their "(V)" and "(A)" ids for
        # the portal's picker, but this only looks for
        # Inventory_Locksmith_Stock rows under the single id passed in
        # — if a part's real stock happens to sit under "(A)" instead,
        # this will raise "no stock found" even though the picker showed
        # it as available. Not yet hit in testing (all real stock seen
        # so far has been under "(V)"), but worth watching for.
        #
        # Full Inventory_Disposals schema, confirmed live via a
        # supervised manual test insert (INFORMATION_SCHEMA.COLUMNS +
        # trial and error — nothing previously wrote to this table so
        # none of this was knowable from existing SELECT queries):
        #   Id (uniqueidentifier, NOT NULL, no default despite what
        #     INFORMATION_SCHEMA reports — supplied explicitly via
        #     NEWID() rather than relying on whatever's really going on
        #     there), StockId, LookupLocksmithId, ReportId, Quantity,
        #     DateCreated, CreatedByUserId (NOT NULL — see
        #     HANDL_PORTAL_CREATED_BY_USER_ID), LocksmithStockId
        #     (nullable — but populated here; see below).
        # No trigger on this table (confirmed via sys.triggers) — Soter
        # does NOT automatically decrement Inventory_Locksmith_Stock
        # when a disposal is inserted, matching the office's existing
        # manual-entry workflow. That's very likely *why* WGTK's stock
        # figures drift and Area 1 (weekly physical recounts) exists at
        # all. The portal deliberately does better: it decrements the
        # matching Inventory_Locksmith_Stock row(s) itself, in the same
        # transaction, so a locksmith can't be shown stock (and dispose
        # against it again) that they've already disposed of today.
        if not settings.HANDL_PORTAL_CREATED_BY_USER_ID:
            raise ValueError(
                "HANDL_PORTAL_CREATED_BY_USER_ID isn't configured — set it to a "
                "real Soter user id (ideally a dedicated 'Locksmith Portal' "
                "account, not a real staff member's) before disposals can be "
                "recorded."
            )

        from datetime import datetime as _datetime

        with self._write_connection() as conn:
            cursor = conn.cursor()

            # Most recently created Inventory_Stock batch for the SKU —
            # mirrors the "most recent batch" ranking already proven
            # correct for cost lookups elsewhere in this file.
            # Unconfirmed whether that's actually how Soter's own app
            # chooses which batch to dispose against.
            cursor.execute(
                """
                SELECT TOP 1 ist.Id
                FROM Inventory_Stock ist
                JOIN Inventory_Parts ipa ON ist.PartId = ipa.Id
                WHERE ipa.SKU = %(sku)s
                ORDER BY ist.DateCreated DESC
                """,
                {"sku": part_code},
            )
            stock_row = cursor.fetchone()
            if not stock_row:
                raise ValueError(f"No Inventory_Stock batch found for SKU {part_code!r}.")

            # This locksmith's own van-stock rows for the part —
            # confirmed on real data there can be several (mostly
            # emptied historical ones sitting at 0 alongside one "live"
            # row with the real quantity). Decrement greedily,
            # largest-first, so a disposal this app already validated
            # against the *summed* stock (see list_current_stock)
            # still succeeds even if it happens to span more than one
            # row — and never lets any single row go negative.
            cursor.execute(
                """
                SELECT ils.Id, ils.Quantity
                FROM Inventory_Locksmith_Stock ils
                JOIN Inventory_Parts ipa ON ils.PartId = ipa.Id
                WHERE ils.LookupLocksmithId = %(lid)s AND ipa.SKU = %(sku)s
                  AND ils.Quantity > 0
                ORDER BY ils.Quantity DESC
                """,
                {"lid": int(soter_locksmith_id), "sku": part_code},
            )
            locksmith_stock_rows = cursor.fetchall()
            if not locksmith_stock_rows:
                raise ValueError(
                    f"No Inventory_Locksmith_Stock found for locksmith "
                    f"{soter_locksmith_id!r}, SKU {part_code!r}."
                )

            remaining = quantity
            primary_locksmith_stock_id = locksmith_stock_rows[0]["Id"]
            for row in locksmith_stock_rows:
                if remaining <= 0:
                    break
                take = min(remaining, row["Quantity"])
                cursor.execute(
                    "UPDATE Inventory_Locksmith_Stock SET Quantity = Quantity - %(take)s "
                    "WHERE Id = %(id)s",
                    {"take": take, "id": row["Id"]},
                )
                remaining -= take
            if remaining > 0:
                raise ValueError(
                    f"Only {quantity - remaining} of {quantity} {part_code} available "
                    f"in Inventory_Locksmith_Stock for locksmith {soter_locksmith_id!r}."
                )

            cursor.execute(
                """
                INSERT INTO Inventory_Disposals
                    (Id, LookupLocksmithId, ReportId, StockId, LocksmithStockId,
                     Quantity, DateCreated, CreatedByUserId)
                VALUES
                    (NEWID(), %(lid)s, %(report_id)s, %(stock_id)s, %(locksmith_stock_id)s,
                     %(qty)s, %(now)s, %(created_by)s)
                """,
                {
                    "lid": int(soter_locksmith_id),
                    "report_id": report_id,
                    "stock_id": stock_row["Id"],
                    "locksmith_stock_id": primary_locksmith_stock_id,
                    "qty": quantity,
                    "now": _datetime.utcnow(),
                    "created_by": settings.HANDL_PORTAL_CREATED_BY_USER_ID,
                },
            )
            conn.commit()


def get_handl_client() -> HandlClient:
    if settings.HANDL_SQL_SERVER:
        return SQLHandlClient()
    return MockHandlClient()
