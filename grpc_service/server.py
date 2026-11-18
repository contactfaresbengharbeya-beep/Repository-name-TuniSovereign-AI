"""
gRPC Telemetry Ingestion & Audit Server (proto3 + grpcio) — Track 5 Tunihara-MedGuard, Week 1.

Implements TelemetryIngestService from protos/tunisovereign.proto:
  * StreamTelemetry    client-streaming, AGENT_SERVICE only, HMAC-verified, persisted to PostgreSQL
  * TriggerUrgentAudit unary, records an audit-request event (NO agent / blockchain call in Week 1)
  * GetInventoryStatus unary, scope-restricted read

Security: JWT bearer in gRPC metadata for every RPC; HMAC is *authenticity of the packet*, not transport
security — set GRPC_TLS_CERT_FILE / GRPC_TLS_KEY_FILE to serve over TLS (otherwise the port is plaintext and
must stay on a trusted network). Secrets are never logged.

Run:  python -m grpc_service.server
"""
import logging
import os
import sys
from concurrent import futures
from functools import wraps

import django
import grpc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.db import connections  # noqa: E402
from rest_framework_simplejwt.exceptions import TokenError  # noqa: E402
from rest_framework_simplejwt.tokens import AccessToken  # noqa: E402

from backend.tunisovereign_core.constants import (  # noqa: E402
    ROLE_AGENT_SERVICE,
    ROLE_NATIONAL_ADMIN,
    ROLE_REGIONAL_AUDITOR,
)
from backend.tunisovereign_core.models import (  # noqa: E402
    AnomalyAndForecastEvent,
    MedicationInventory,
    StakeholderUser,
)
from backend.tunisovereign_core.scope import can_access_facility  # noqa: E402
from backend.tunisovereign_core.telemetry_ingest import (  # noqa: E402
    NODE_CODE_RE,
    IngestResult,
    Packet,
    ingest_packets,
)

from . import tunisovereign_pb2 as pb  # noqa: E402
from . import tunisovereign_pb2_grpc as pb_grpc  # noqa: E402

logger = logging.getLogger("grpc_service")

FLUSH_EVERY = 500
MAX_STREAM_PACKETS = 200_000
MAX_REASON_LEN = 500


def _release_db(fn):
    """gRPC worker threads own their DB connections: always close them when the RPC ends."""

    @wraps(fn)
    def wrapper(self, request, context):
        try:
            return fn(self, request, context)
        finally:
            connections.close_all()

    return wrapper


def _authenticate(context, allowed_roles=None):
    """Return the active StakeholderUser for the bearer token, or abort the RPC."""
    header = dict(context.invocation_metadata()).get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        context.abort(grpc.StatusCode.UNAUTHENTICATED, "Missing bearer token")
    try:
        user_id = AccessToken(token)["user_id"]
        user = StakeholderUser.objects.select_related("facility").get(pk=user_id, is_active=True)
    except (TokenError, KeyError, StakeholderUser.DoesNotExist):
        context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid or expired token")
    if allowed_roles is not None and user.role not in allowed_roles:
        context.abort(grpc.StatusCode.PERMISSION_DENIED, "Role not allowed for this RPC")
    return user


def _find_inventory(user, node_code, context):
    """Inventory visible to ``user``; unknown and out-of-scope nodes are indistinguishable (NOT_FOUND)."""
    inventory = None
    if NODE_CODE_RE.match(node_code or ""):
        inventory = MedicationInventory.objects.select_related("facility", "medication").filter(
            node_code=node_code
        ).first()
    if inventory is None or not can_access_facility(user, inventory.facility):
        context.abort(grpc.StatusCode.NOT_FOUND, "Unknown node_code")
    return inventory


class TelemetryIngestServicer(pb_grpc.TelemetryIngestServiceServicer):
    def StreamTelemetry(self, request_iterator, context):
        try:
            _authenticate(context, (ROLE_AGENT_SERVICE,))
            total = IngestResult()
            batch, seen = [], 0
            for message in request_iterator:
                seen += 1
                if seen > MAX_STREAM_PACKETS:
                    context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Too many packets in one stream")
                batch.append(Packet(
                    node_code=message.node_code, governorate=message.governorate, metric_type=message.metric_type,
                    value=message.value, iso_timestamp=message.iso_timestamp,
                    signature_hmac=message.signature_hmac, unit=message.unit, is_simulated=message.is_simulated,
                ))
                if len(batch) >= FLUSH_EVERY:
                    self._flush(batch, seen - len(batch), total)
                    batch = []
            if batch:
                self._flush(batch, seen - len(batch), total)
            return pb.IngestAck(
                accepted_count=total.accepted,
                anomaly_triggered=False,
                agent_thread_id="",
                rejected_count=total.rejected_count,
                duplicate_count=total.duplicates,
                rejected_reasons=total.rejected_reasons[:50],
            )
        finally:
            connections.close_all()

    @staticmethod
    def _flush(batch, offset, total):
        result = ingest_packets(batch, protocol="gRPC", require_signature=True, index_offset=offset)
        total.accepted += result.accepted
        total.duplicates += result.duplicates
        total.rejected_count += result.rejected_count
        total.rejected_reasons.extend(result.rejected_reasons)

    @_release_db
    def TriggerUrgentAudit(self, request, context):
        user = _authenticate(context, (ROLE_REGIONAL_AUDITOR, ROLE_NATIONAL_ADMIN, ROLE_AGENT_SERVICE))
        inventory = _find_inventory(user, request.node_code, context)
        reason = (request.reason or "").strip()
        if not reason or len(reason) > MAX_REASON_LEN:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"reason must be 1-{MAX_REASON_LEN} characters")
        event = AnomalyAndForecastEvent.objects.create(
            inventory=inventory,
            anomaly_type=AnomalyAndForecastEvent.AnomalyType.MANUAL_AUDIT_REQUEST,
            severity=AnomalyAndForecastEvent.Severity.MEDIUM,
            details={"reason": reason, "requested_by": user.username, "week1_stub": True},
            is_simulated=inventory.is_simulated,
        )
        # No agent loop and no blockchain transaction exist in Week 1, so tx_hash is intentionally empty.
        return pb.AuditResponse(status="AUDIT_REQUEST_RECORDED", tx_hash="", audit_event_id=str(event.pk))

    @_release_db
    def GetInventoryStatus(self, request, context):
        user = _authenticate(context)
        inv = _find_inventory(user, request.node_code, context)
        return pb.InventoryStatus(
            node_code=inv.node_code,
            facility_code=inv.facility.code,
            governorate=inv.facility.governorate,
            medication=str(inv.medication),
            current_stock_units=inv.current_stock_units,
            reorder_threshold_units=inv.reorder_threshold_units,
            below_reorder_threshold=inv.current_stock_units < inv.reorder_threshold_units,
            last_reading_iso=inv.last_reading_at.isoformat() if inv.last_reading_at else "",
            is_simulated=inv.is_simulated,
        )


def build_server(port: int = 0, credentials=None, max_workers: int = 8):
    """Create (not start) a server. ``port=0`` picks a free port. Returns (server, bound_port)."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    pb_grpc.add_TelemetryIngestServiceServicer_to_server(TelemetryIngestServicer(), server)
    address = f"[::]:{port}"
    bound = server.add_secure_port(address, credentials) if credentials else server.add_insecure_port(address)
    return server, bound


def _tls_credentials():
    cert, key = settings.GRPC_TLS_CERT_FILE, settings.GRPC_TLS_KEY_FILE
    if not (cert and key):
        return None
    with open(key, "rb") as k, open(cert, "rb") as c:
        return grpc.ssl_server_credentials([(k.read(), c.read())])


def serve(port: int = None):
    logging.basicConfig(level=logging.INFO)
    port = port or settings.GRPC_PORT
    credentials = _tls_credentials()
    server, bound = build_server(port, credentials)
    server.start()
    mode = "TLS" if credentials else "PLAINTEXT (set GRPC_TLS_CERT_FILE/GRPC_TLS_KEY_FILE for TLS)"
    logger.info("TuniSovereign-AI gRPC server listening on port %s [%s]", bound, mode)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(grace=2)


if __name__ == "__main__":
    serve()
