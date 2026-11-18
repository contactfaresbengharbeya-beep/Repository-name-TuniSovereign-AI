"""
gRPC service tests against a REAL in-process grpc server + generated stubs + PostgreSQL:
unary RPCs, client-streaming ingestion, JWT auth, role checks, HMAC verification/rejection, persistence.
"""
from datetime import timedelta

import grpc
import pytest
from django.conf import settings
from django.db import connection
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from backend.tunisovereign_core.models import AnomalyAndForecastEvent, TelemetryReading
from backend.tunisovereign_core.signing import sign_packet
from grpc_service import tunisovereign_pb2 as pb
from grpc_service import tunisovereign_pb2_grpc as pb_grpc
from grpc_service.server import build_server

from .conftest import token_for

SECRET = settings.TELEMETRY_HMAC_SECRET


@pytest.fixture
def grpc_stub():
    server, port = build_server(0)
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield pb_grpc.TelemetryIngestServiceStub(channel)
    finally:
        channel.close()
        server.stop(0).wait()


def md(user):
    return (("authorization", f"Bearer {token_for(user)}"),)


def packet(node="KAS-PHA-01-A10BA02", gov="Kasserine", metric="STOCK_LEVEL", value=80.0, ts=None, secret=SECRET, sig=None, **kw):
    ts = ts or (timezone.now() - timedelta(minutes=10)).isoformat()
    signature = sig if sig is not None else sign_packet(secret, node, gov, metric, value, ts)
    return pb.TelemetryPacket(node_code=node, governorate=gov, metric_type=metric, value=value, iso_timestamp=ts,
                              signature_hmac=signature, unit="boxes", is_simulated=True, **kw)


def stream(stub, packets, user):
    return stub.StreamTelemetry(iter(packets), metadata=md(user))


pytestmark = pytest.mark.django_db(transaction=True)


# ----------------------------------------------------------------------------- unary: GetInventoryStatus
def test_unary_inventory_status_scoped(world, grpc_stub):
    status = grpc_stub.GetInventoryStatus(pb.InventoryRequest(node_code="KAS-PHA-01-A10BA02"), metadata=md(world.users.coop_kas))
    assert isinstance(status, pb.InventoryStatus)
    assert (status.facility_code, status.governorate, status.current_stock_units) == ("KAS-PHA-01", "Kasserine", 100.0)
    assert status.is_simulated and not status.below_reorder_threshold
    for user in (world.users.admin, world.users.auditor_kas, world.users.agent):
        assert grpc_stub.GetInventoryStatus(pb.InventoryRequest(node_code="KAS-PHA-01-A10BA02"), metadata=md(user)).node_code
    # out-of-scope and unknown nodes are indistinguishable
    for user, node in ((world.users.coop_kas, "SFA-HOP-01-A10BA02"), (world.users.auditor_sfa, "KAS-PHA-01-A10BA02"),
                       (world.users.admin, "NOPE-0000")):
        with pytest.raises(grpc.RpcError) as err:
            grpc_stub.GetInventoryStatus(pb.InventoryRequest(node_code=node), metadata=md(user))
        assert err.value.code() == grpc.StatusCode.NOT_FOUND


def test_unary_requires_valid_jwt(world, grpc_stub):
    request = pb.InventoryRequest(node_code="KAS-PHA-01-A10BA02")
    for metadata in (None, (("authorization", "Bearer garbage"),), (("authorization", "Basic abc"),)):
        with pytest.raises(grpc.RpcError) as err:
            grpc_stub.GetInventoryStatus(request, metadata=metadata)
        assert err.value.code() == grpc.StatusCode.UNAUTHENTICATED
    expired = AccessToken.for_user(world.users.admin)
    expired.set_exp(lifetime=-timedelta(seconds=5))
    with pytest.raises(grpc.RpcError) as err:
        grpc_stub.GetInventoryStatus(request, metadata=(("authorization", f"Bearer {expired}"),))
    assert err.value.code() == grpc.StatusCode.UNAUTHENTICATED
    world.users.admin.is_active = False
    world.users.admin.save()
    with pytest.raises(grpc.RpcError) as err:
        grpc_stub.GetInventoryStatus(request, metadata=md(world.users.admin))
    assert err.value.code() == grpc.StatusCode.UNAUTHENTICATED  # deactivated users lose access immediately


# ----------------------------------------------------------------------------- unary: TriggerUrgentAudit
def test_unary_urgent_audit_records_event_without_agent(world, grpc_stub):
    reply = grpc_stub.TriggerUrgentAudit(
        pb.AuditRequest(node_code="KAS-PHA-01-A10BA02", reason="Unusual metformin demand"), metadata=md(world.users.auditor_kas)
    )
    assert isinstance(reply, pb.AuditResponse)
    assert reply.status == "AUDIT_REQUEST_RECORDED" and reply.tx_hash == ""  # no fake blockchain tx
    event = AnomalyAndForecastEvent.objects.get(pk=int(reply.audit_event_id))
    assert event.anomaly_type == "MANUAL_AUDIT_REQUEST" and event.details["requested_by"] == "auditor_kas"
    assert event.inventory == world.inv.kas1_met


def test_unary_urgent_audit_authorization(world, grpc_stub):
    req = pb.AuditRequest(node_code="KAS-PHA-01-A10BA02", reason="check")
    cases = [(world.users.coop_kas, grpc.StatusCode.PERMISSION_DENIED), (world.users.auditor_sfa, grpc.StatusCode.NOT_FOUND)]
    for user, expected in cases:
        with pytest.raises(grpc.RpcError) as err:
            grpc_stub.TriggerUrgentAudit(req, metadata=md(user))
        assert err.value.code() == expected
    for bad in (pb.AuditRequest(node_code="KAS-PHA-01-A10BA02", reason=""), pb.AuditRequest(node_code="KAS-PHA-01-A10BA02", reason="x" * 501)):
        with pytest.raises(grpc.RpcError) as err:
            grpc_stub.TriggerUrgentAudit(bad, metadata=md(world.users.admin))
        assert err.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert AnomalyAndForecastEvent.objects.count() == 0


# ----------------------------------------------------------------------------- client-streaming ingestion
def test_stream_persists_to_postgresql_and_updates_snapshot(world, grpc_stub):
    base = timezone.now() - timedelta(hours=2)
    packets = [
        packet(metric="STOCK_LEVEL", value=60.0 - k, ts=(base + timedelta(minutes=k)).isoformat()) for k in range(5)
    ] + [packet(metric="DISPENSED_UNITS", value=3.0, ts=base.isoformat()),
         packet(node="KAS-PHA-01-A10AE04", metric="COLD_CHAIN_TEMP", value=5.1, ts=base.isoformat())]
    ack = stream(grpc_stub, packets, world.users.agent)
    assert isinstance(ack, pb.IngestAck)
    assert (ack.accepted_count, ack.rejected_count, ack.duplicate_count) == (7, 0, 0)
    assert ack.anomaly_triggered is False and ack.agent_thread_id == ""
    # evidence of REAL PostgreSQL persistence (not a mock)
    assert connection.vendor == "postgresql"
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*), bool_and(signature_verified), min(ingestion_protocol) FROM tunisovereign_core_telemetryreading")
        assert cursor.fetchone() == (7, True, "gRPC")
    inv = world.inv.kas1_met
    inv.refresh_from_db()
    assert inv.current_stock_units == 56.0  # newest STOCK_LEVEL
    assert inv.last_reading_at is not None


def test_stream_is_idempotent(world, grpc_stub):
    ts = (timezone.now() - timedelta(hours=1)).isoformat()
    packets = [packet(metric="DISPENSED_UNITS", value=2.0, ts=ts), packet(metric="RX_COUNT", value=1.0, ts=ts)]
    first = stream(grpc_stub, packets, world.users.agent)
    replay = stream(grpc_stub, packets, world.users.agent)
    assert (first.accepted_count, replay.accepted_count, replay.duplicate_count) == (2, 0, 2)
    assert TelemetryReading.objects.count() == 2
    in_batch = stream(grpc_stub, [packet(value=1.0, ts=(timezone.now() - timedelta(hours=5)).isoformat())] * 2, world.users.agent)
    assert (in_batch.accepted_count, in_batch.duplicate_count) == (1, 1)


def test_invalid_hmac_is_rejected_and_not_stored(world, grpc_stub):
    ts = (timezone.now() - timedelta(minutes=30)).isoformat()
    good = packet(metric="DISPENSED_UNITS", value=4.0, ts=ts)
    tampered = packet(metric="DISPENSED_UNITS", value=4.0, ts=(timezone.now() - timedelta(minutes=31)).isoformat())
    tampered.value = 400.0  # payload altered after signing
    wrong_secret = packet(metric="RX_COUNT", value=1.0, ts=ts, secret="some-other-secret")
    unsigned = packet(metric="CONSUMPTION_RATE", value=10.0, ts=ts, sig="")
    garbage = packet(metric="STOCK_LEVEL", value=5.0, ts=ts, sig="zz" * 32)
    ack = stream(grpc_stub, [good, tampered, wrong_secret, unsigned, garbage], world.users.agent)
    assert ack.accepted_count == 1 and ack.rejected_count == 4
    assert list(ack.rejected_reasons) == [f"{i}:INVALID_SIGNATURE" for i in (1, 2, 3, 4)]
    assert list(TelemetryReading.objects.values_list("metric_type", "value")) == [("DISPENSED_UNITS", 4.0)]
    # a replayed signature on a different node/metric/timestamp does not verify either
    moved = packet(node="KAS-HOP-01-A10BA02", metric="DISPENSED_UNITS", value=4.0, ts=ts, sig=good.signature_hmac)
    assert stream(grpc_stub, [moved], world.users.agent).rejected_count == 1


def test_validation_rejections_are_coded(world, grpc_stub):
    ts = (timezone.now() - timedelta(minutes=30)).isoformat()
    future = (timezone.now() + timedelta(days=1)).isoformat()
    cases = {
        "UNKNOWN_NODE": packet(node="KAS-PHA-01-ZZZZZZZ", ts=ts),
        "GOVERNORATE_MISMATCH": packet(gov="Sfax", ts=ts),
        "INVALID_GOVERNORATE": packet(gov="Atlantis", ts=ts),
        "INVALID_METRIC": packet(metric="SOIL_MOISTURE", ts=ts),
        "INVALID_VALUE": packet(value=-3.0, ts=ts),
        "INVALID_TIMESTAMP": packet(ts=future),
        "INVALID_NODE_CODE": packet(node="drop table;--", ts=ts),
    }
    for code, p in cases.items():
        ack = stream(grpc_stub, [p], world.users.agent)
        assert ack.accepted_count == 0 and list(ack.rejected_reasons) == [f"0:{code}"], code
    nan_packet = packet(value=float("nan"), ts=ts, sig="")
    assert list(stream(grpc_stub, [nan_packet], world.users.agent).rejected_reasons) == ["0:INVALID_VALUE"]
    naive = packet(ts="2026-01-01T00:00:00")
    assert list(stream(grpc_stub, [naive], world.users.agent).rejected_reasons) == ["0:INVALID_TIMESTAMP"]
    assert TelemetryReading.objects.count() == 0


def test_stream_requires_agent_service_role(world, grpc_stub):
    for user in (world.users.coop_kas, world.users.auditor_kas, world.users.admin):
        with pytest.raises(grpc.RpcError) as err:
            stream(grpc_stub, [packet()], user)
        assert err.value.code() == grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(grpc.RpcError) as err:
        grpc_stub.StreamTelemetry(iter([packet()]))  # no metadata
    assert err.value.code() == grpc.StatusCode.UNAUTHENTICATED
    assert TelemetryReading.objects.count() == 0


def test_large_stream_is_chunked_with_correct_reject_indexes(world, grpc_stub):
    base = timezone.now() - timedelta(days=2)
    packets = [packet(metric="DISPENSED_UNITS", value=1.0, ts=(base + timedelta(minutes=k)).isoformat()) for k in range(1200)]
    packets[700] = packet(metric="DISPENSED_UNITS", value=1.0, ts=base.isoformat(), sig="bad")  # index in 2nd chunk
    ack = stream(grpc_stub, packets, world.users.agent)
    assert ack.accepted_count == 1199 and ack.rejected_count == 1
    assert list(ack.rejected_reasons) == ["700:INVALID_SIGNATURE"]
    assert TelemetryReading.objects.count() == 1199
