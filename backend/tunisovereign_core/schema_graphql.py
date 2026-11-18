"""
Strawberry GraphQL schema (Track 5 · Tunihara-MedGuard) with per-request DataLoaders.

Every relation traversed below the root fields (facility -> inventory, inventory -> medication /
facility / latest readings / anomalies, request -> facilities / medication) goes through a DataLoader, so
a query over N facilities issues a CONSTANT number of SQL statements (see tests/test_graphql.py).
Row-level isolation uses the same scope helpers as REST; nested relations inherit the root's scope.
"""
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import strawberry
from strawberry.dataloader import DataLoader
from strawberry.extensions import QueryDepthLimiter
from strawberry.scalars import JSON
from django.db.models import Count, F, Q

from .models import (
    AgentLoopExecution,
    AnomalyAndForecastEvent,
    HealthcareFacility,
    Medication,
    MedicationInventory,
    RedistributionRequest,
    TelemetryReading,
)
from .scope import scoped, scoped_agent_executions, scoped_redistributions

MAX_TELEMETRY_LIMIT = 1000


# ---------------------------------------------------------------------------
# DataLoaders (one set per request — never shared between users)
# ---------------------------------------------------------------------------
def _by_key(rows, key_attr):
    grouped = defaultdict(list)
    for row in rows:
        grouped[getattr(row, key_attr)].append(row)
    return grouped


@dataclass
class Loaders:
    inventory_by_facility: DataLoader
    facility_by_id: DataLoader
    medication_by_id: DataLoader
    latest_readings_by_inventory: DataLoader
    anomalies_by_inventory: DataLoader


def make_loaders() -> Loaders:
    async def load_inventory_by_facility(keys):
        rows = [r async for r in MedicationInventory.objects.filter(facility_id__in=keys).order_by("node_code")]
        grouped = _by_key(rows, "facility_id")
        return [grouped.get(k, []) for k in keys]

    async def load_facility_by_id(keys):
        found = {f.pk: f async for f in HealthcareFacility.objects.filter(pk__in=keys)}
        return [found.get(k) for k in keys]

    async def load_medication_by_id(keys):
        found = {m.pk: m async for m in Medication.objects.filter(pk__in=keys)}
        return [found.get(k) for k in keys]

    async def load_latest_readings(keys):
        # newest reading per (inventory, metric) in ONE query (PostgreSQL DISTINCT ON)
        qs = (
            TelemetryReading.objects.filter(inventory_id__in=keys)
            .order_by("inventory_id", "metric_type", "-timestamp")
            .distinct("inventory_id", "metric_type")
        )
        grouped = _by_key([r async for r in qs], "inventory_id")
        return [grouped.get(k, []) for k in keys]

    async def load_anomalies(keys):
        qs = AnomalyAndForecastEvent.objects.filter(inventory_id__in=keys).order_by("-detected_at")
        grouped = _by_key([r async for r in qs], "inventory_id")
        return [grouped.get(k, []) for k in keys]

    return Loaders(
        inventory_by_facility=DataLoader(load_fn=load_inventory_by_facility),
        facility_by_id=DataLoader(load_fn=load_facility_by_id),
        medication_by_id=DataLoader(load_fn=load_medication_by_id),
        latest_readings_by_inventory=DataLoader(load_fn=load_latest_readings),
        anomalies_by_inventory=DataLoader(load_fn=load_anomalies),
    )


@dataclass
class GQLContext:
    request: Any
    user: Any
    loaders: Loaders = field(default_factory=make_loaders)


def _user(info):
    user = getattr(info.context, "user", None)
    if user is None or not user.is_authenticated:
        raise PermissionError("Authentication required")
    return user


def _int(value, name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {name}")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
@strawberry.type
class MedicationType:
    id: strawberry.ID
    dci_name: str
    atc_code: str
    therapeutic_class: str
    dosage_form: str
    strength: str
    unit_of_measure: str
    is_chronic_care: bool
    cnam_subsidy_status: str
    requires_cold_chain: bool
    is_simulated: bool


@strawberry.type
class TelemetryReadingType:
    id: strawberry.ID
    timestamp: dt.datetime
    metric_type: str
    value: float
    ingestion_protocol: str
    is_simulated: bool
    signature_verified: bool


@strawberry.type
class AnomalyEventType:
    id: strawberry.ID
    detected_at: dt.datetime
    anomaly_type: str
    severity: str
    isolation_forest_score: Optional[float]
    forecasted_stockout_days: Optional[float]
    is_simulated: bool


@strawberry.type
class InventoryType:
    id: strawberry.ID
    node_code: str
    current_stock_units: float
    reorder_threshold_units: float
    safety_stock_days: int
    last_reading_at: Optional[dt.datetime]
    is_redistribution_hold: bool
    is_simulated: bool

    @strawberry.field
    async def facility(self, info: strawberry.Info) -> Optional["FacilityType"]:
        return await info.context.loaders.facility_by_id.load(self.facility_id)

    @strawberry.field
    async def medication(self, info: strawberry.Info) -> Optional[MedicationType]:
        return await info.context.loaders.medication_by_id.load(self.medication_id)

    @strawberry.field
    async def latest_readings(self, info: strawberry.Info) -> List[TelemetryReadingType]:
        return await info.context.loaders.latest_readings_by_inventory.load(self.pk)

    @strawberry.field
    async def anomalies(self, info: strawberry.Info) -> List[AnomalyEventType]:
        return await info.context.loaders.anomalies_by_inventory.load(self.pk)


@strawberry.type
class FacilityType:
    id: strawberry.ID
    code: str
    name: str
    facility_type: str
    governorate: str
    latitude: Optional[float]
    longitude: Optional[float]
    is_simulated: bool

    @strawberry.field
    async def inventory(self, info: strawberry.Info) -> List[InventoryType]:
        return await info.context.loaders.inventory_by_facility.load(self.pk)


@strawberry.type
class RedistributionRequestType:
    id: strawberry.ID
    units: float
    status: str
    rationale: JSON
    is_simulated: bool
    created_at: dt.datetime

    @strawberry.field
    async def source_facility(self, info: strawberry.Info) -> Optional[FacilityType]:
        return await info.context.loaders.facility_by_id.load(self.source_facility_id)

    @strawberry.field
    async def destination_facility(self, info: strawberry.Info) -> Optional[FacilityType]:
        return await info.context.loaders.facility_by_id.load(self.destination_facility_id)

    @strawberry.field
    async def medication(self, info: strawberry.Info) -> Optional[MedicationType]:
        return await info.context.loaders.medication_by_id.load(self.medication_id)


@strawberry.type
class GovernorateRiskType:
    governorate: str
    monitored_positions: int
    below_reorder_threshold: int


@strawberry.type
class MeType:
    id: strawberry.ID
    username: str
    role: str
    governorate: str


# Retained from the starter for Week 3 (empty until the agent writes executions).
@strawberry.type
class ZKReceiptType:
    zk_commitment_hash: str
    tx_hash: str
    block_number: int
    network_name: str
    is_verified_on_chain: bool


@strawberry.type
class AgentExecutionType:
    thread_id: str
    governorate: str
    trigger_source: str
    final_recommendation: str
    confidence_score: float
    created_at: dt.datetime

    @strawberry.field
    def zk_receipt(self) -> Optional[ZKReceiptType]:
        try:
            return self.zk_receipt  # cached by select_related; raises when no receipt exists
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------
@strawberry.type
class Query:
    @strawberry.field
    def me(self, info: strawberry.Info) -> MeType:
        return _user(info)

    @strawberry.field
    async def facilities(self, info: strawberry.Info, governorate: Optional[str] = None) -> List[FacilityType]:
        qs = scoped(HealthcareFacility.objects.all(), _user(info), "")
        if governorate:
            qs = qs.filter(governorate=governorate)
        return [f async for f in qs.order_by("governorate", "code")]

    @strawberry.field
    async def inventory(
        self,
        info: strawberry.Info,
        facility_id: Optional[strawberry.ID] = None,
        medication_id: Optional[strawberry.ID] = None,
        governorate: Optional[str] = None,
    ) -> List[InventoryType]:
        qs = scoped(MedicationInventory.objects.all(), _user(info), "facility")
        if facility_id:
            qs = qs.filter(facility_id=str(facility_id))
        if medication_id:
            qs = qs.filter(medication_id=_int(medication_id, "medication_id"))
        if governorate:
            qs = qs.filter(facility__governorate=governorate)
        return [i async for i in qs.order_by("node_code")]

    @strawberry.field
    async def telemetry(
        self,
        info: strawberry.Info,
        inventory_id: strawberry.ID,
        metric: Optional[str] = None,
        start: Optional[dt.datetime] = None,
        end: Optional[dt.datetime] = None,
        limit: int = 100,
    ) -> List[TelemetryReadingType]:
        qs = scoped(TelemetryReading.objects.all(), _user(info), "inventory__facility").filter(
            inventory_id=_int(inventory_id, "inventory_id")
        )
        if metric:
            qs = qs.filter(metric_type=metric)
        if start:
            qs = qs.filter(timestamp__gte=start)
        if end:
            qs = qs.filter(timestamp__lte=end)
        limit = max(1, min(limit, MAX_TELEMETRY_LIMIT))
        return [r async for r in qs.order_by("-timestamp")[:limit]]

    @strawberry.field
    async def anomalies(
        self, info: strawberry.Info, governorate: Optional[str] = None, severity: Optional[str] = None
    ) -> List[AnomalyEventType]:
        qs = scoped(AnomalyAndForecastEvent.objects.all(), _user(info), "inventory__facility")
        if governorate:
            qs = qs.filter(inventory__facility__governorate=governorate)
        if severity:
            qs = qs.filter(severity=severity)
        return [a async for a in qs.order_by("-detected_at")[:500]]

    @strawberry.field
    async def redistribution_requests(
        self, info: strawberry.Info, status: Optional[str] = None
    ) -> List[RedistributionRequestType]:
        qs = scoped_redistributions(RedistributionRequest.objects.all(), _user(info))
        if status:
            qs = qs.filter(status=status)
        return [r async for r in qs.order_by("-created_at")[:500]]

    @strawberry.field
    async def shortage_risk_by_governorate(self, info: strawberry.Info) -> List[GovernorateRiskType]:
        qs = (
            scoped(MedicationInventory.objects.all(), _user(info), "facility")
            .values("facility__governorate")
            .annotate(
                total=Count("id"),
                below=Count("id", filter=Q(current_stock_units__lt=F("reorder_threshold_units"))),
            )
            .order_by("facility__governorate")
        )
        return [
            GovernorateRiskType(
                governorate=row["facility__governorate"],
                monitored_positions=row["total"],
                below_reorder_threshold=row["below"],
            )
            async for row in qs
        ]

    @strawberry.field
    async def agent_executions(self, info: strawberry.Info) -> List[AgentExecutionType]:
        qs = scoped_agent_executions(AgentLoopExecution.objects.select_related("zk_receipt"), _user(info))
        return [e async for e in qs.order_by("-created_at")[:100]]


schema = strawberry.Schema(
    query=Query,
    extensions=[lambda: QueryDepthLimiter(max_depth=8)],
)
