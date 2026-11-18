"""
Telemetry ingestion service — the single code path used by gRPC (signed, AGENT_SERVICE) and REST (JWT).

Validation order (cheapest / least-informative first): field format -> metric -> value range ->
timestamp -> HMAC signature -> node lookup -> governorate consistency. Signatures are checked before the
database is consulted so that unsigned callers cannot probe which node codes exist.
Rejection reasons are machine codes only; secrets and signatures are never logged or echoed.
"""
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .constants import GOVERNORATES, METRIC_RANGES, METRIC_STOCK_LEVEL
from .models import MedicationInventory, TelemetryReading
from .signing import verify_packet

logger = logging.getLogger(__name__)

NODE_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_\-]{2,47}$")
MAX_REASONS = 50

INVALID_NODE_CODE = "INVALID_NODE_CODE"
INVALID_GOVERNORATE = "INVALID_GOVERNORATE"
INVALID_METRIC = "INVALID_METRIC"
INVALID_VALUE = "INVALID_VALUE"
INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
INVALID_SIGNATURE = "INVALID_SIGNATURE"
UNKNOWN_NODE = "UNKNOWN_NODE"
GOVERNORATE_MISMATCH = "GOVERNORATE_MISMATCH"
DUPLICATE_IN_BATCH = "DUPLICATE_IN_BATCH"


@dataclass
class Packet:
    node_code: str
    governorate: str
    metric_type: str
    value: float
    iso_timestamp: str
    signature_hmac: str = ""
    unit: str = ""
    is_simulated: bool = True


@dataclass
class IngestResult:
    accepted: int = 0
    duplicates: int = 0
    rejected_reasons: List[str] = field(default_factory=list)
    rejected_count: int = 0

    def reject(self, index: int, code: str) -> None:
        self.rejected_count += 1
        if len(self.rejected_reasons) < MAX_REASONS:
            self.rejected_reasons.append(f"{index}:{code}")


class TelemetryValidationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_metric_value(metric_type: str, value) -> float:
    if metric_type not in METRIC_RANGES:
        raise TelemetryValidationError(INVALID_METRIC)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise TelemetryValidationError(INVALID_VALUE)
    low, high = METRIC_RANGES[metric_type]
    if not math.isfinite(value) or value < low or value > high:
        raise TelemetryValidationError(INVALID_VALUE)
    return value


def validate_timestamp(ts: datetime, now: Optional[datetime] = None) -> datetime:
    if ts is None or timezone.is_naive(ts):
        raise TelemetryValidationError(INVALID_TIMESTAMP)
    now = now or timezone.now()
    if ts > now + timedelta(seconds=settings.TELEMETRY_MAX_FUTURE_SKEW_SECONDS):
        raise TelemetryValidationError(INVALID_TIMESTAMP)
    return ts


def _parse_iso(iso_timestamp: str) -> datetime:
    try:
        return parse_datetime(iso_timestamp)
    except (ValueError, TypeError):
        raise TelemetryValidationError(INVALID_TIMESTAMP)


def _validate_packet(packet: Packet, require_signature: bool, secret: str) -> Tuple[datetime, float]:
    if not NODE_CODE_RE.match(packet.node_code or ""):
        raise TelemetryValidationError(INVALID_NODE_CODE)
    if packet.governorate not in GOVERNORATES:
        raise TelemetryValidationError(INVALID_GOVERNORATE)
    value = validate_metric_value(packet.metric_type, packet.value)
    ts = validate_timestamp(_parse_iso(packet.iso_timestamp))
    if require_signature and not verify_packet(
        secret, packet.signature_hmac, packet.node_code, packet.governorate,
        packet.metric_type, packet.value, packet.iso_timestamp,
    ):
        raise TelemetryValidationError(INVALID_SIGNATURE)
    return ts, value


def refresh_inventory_snapshot(latest_stock: Dict[int, Tuple[datetime, float]]) -> None:
    """Move ``current_stock_units`` / ``last_reading_at`` forward from the newest STOCK_LEVEL readings."""
    if not latest_stock:
        return
    for inv in MedicationInventory.objects.select_for_update().filter(pk__in=latest_stock):
        ts, value = latest_stock[inv.pk]
        if inv.last_reading_at is None or ts > inv.last_reading_at:
            inv.current_stock_units = value
            inv.last_reading_at = ts
            inv.save(update_fields=["current_stock_units", "last_reading_at"])


def ingest_packets(
    packets: Iterable[Packet],
    *,
    protocol: str,
    require_signature: bool = True,
    secret: Optional[str] = None,
    index_offset: int = 0,
) -> IngestResult:
    secret = secret if secret is not None else settings.TELEMETRY_HMAC_SECRET
    result = IngestResult()
    valid: List[Tuple[int, Packet, datetime, float]] = []

    for index, packet in enumerate(packets):
        try:
            ts, value = _validate_packet(packet, require_signature, secret)
        except TelemetryValidationError as exc:
            result.reject(index + index_offset, exc.code)
            continue
        valid.append((index, packet, ts, value))

    if not valid:
        return result

    codes = {p.node_code for _, p, _, _ in valid}
    inventories = {
        inv.node_code: inv
        for inv in MedicationInventory.objects.select_related("facility").filter(node_code__in=codes)
    }

    candidates: Dict[Tuple[int, str, datetime], Tuple[Packet, float]] = {}
    for index, packet, ts, value in valid:
        inv = inventories.get(packet.node_code)
        if inv is None:
            result.reject(index + index_offset, UNKNOWN_NODE)
            continue
        if inv.facility.governorate != packet.governorate:
            result.reject(index + index_offset, GOVERNORATE_MISMATCH)
            continue
        key = (inv.pk, packet.metric_type, ts)
        if key in candidates:
            result.duplicates += 1
            continue
        candidates[key] = (packet, value)

    if not candidates:
        return result

    inv_ids = {k[0] for k in candidates}
    timestamps = [k[2] for k in candidates]
    existing = set(
        TelemetryReading.objects.filter(
            inventory_id__in=inv_ids, timestamp__gte=min(timestamps), timestamp__lte=max(timestamps)
        ).values_list("inventory_id", "metric_type", "timestamp")
    )

    new_rows: List[TelemetryReading] = []
    latest_stock: Dict[int, Tuple[datetime, float]] = {}
    for key, (packet, value) in candidates.items():
        if key in existing:
            result.duplicates += 1
            continue
        inv_id, metric, ts = key
        new_rows.append(
            TelemetryReading(
                inventory_id=inv_id, timestamp=ts, metric_type=metric, value=value,
                ingestion_protocol=protocol, is_simulated=packet.is_simulated,
                signature_verified=require_signature,
            )
        )
        if metric == METRIC_STOCK_LEVEL and (inv_id not in latest_stock or ts > latest_stock[inv_id][0]):
            latest_stock[inv_id] = (ts, value)

    with transaction.atomic():
        TelemetryReading.objects.bulk_create(new_rows, batch_size=1000, ignore_conflicts=True)
        refresh_inventory_snapshot(latest_stock)
    result.accepted = len(new_rows)
    logger.info(
        "telemetry ingest protocol=%s accepted=%d duplicates=%d rejected=%d",
        protocol, result.accepted, result.duplicates, result.rejected_count,
    )
    return result
