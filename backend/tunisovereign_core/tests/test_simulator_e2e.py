"""Telemetry simulator: model properties (pure) and a real end-to-end run  simulator -> gRPC -> PostgreSQL."""
from datetime import datetime, timedelta, timezone as dt_tz

import grpc
import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connection

from backend.tunisovereign_core import seed_data
from backend.tunisovereign_core.models import (
    HealthcareFacility,
    Medication,
    MedicationInventory,
    Prescription,
    StakeholderUser,
    TelemetryReading,
)
from backend.tunisovereign_core.signing import verify_packet
from backend.tunisovereign_core.constants import ALL_METRICS, GOVERNORATES, METRIC_RANGES
from grpc_service.server import build_server
from simulator import tunisian_simulator as sim

from .conftest import token_for

START = datetime(2026, 11, 10, 6, 0, tzinfo=dt_tz.utc)


# ------------------------------------------------------------------------------ pure generator properties
def test_generator_is_deterministic_and_seed_sensitive():
    a = sim.generate_telemetry_batch(hours=12, seed=7, start=START, secret_key="k")
    b = sim.generate_telemetry_batch(hours=12, seed=7, start=START, secret_key="k")
    c = sim.generate_telemetry_batch(hours=12, seed=8, start=START, secret_key="k")
    assert a == b and a != c


def test_generated_packets_are_valid_signed_and_synthetic():
    packets = sim.generate_telemetry_batch(hours=48, start=START, secret_key="unit-test-secret")
    nodes = {n["node_code"]: n for n in seed_data.build_nodes()}
    assert {p["node_code"] for p in packets} == set(nodes)
    for p in packets:
        assert p["metric_type"] in ALL_METRICS and p["governorate"] in GOVERNORATES
        low, high = METRIC_RANGES[p["metric_type"]]
        assert low <= p["value"] <= high
        assert p["is_simulated"] is True
        assert p["governorate"] == nodes[p["node_code"]]["governorate"]
        assert verify_packet("unit-test-secret", p["signature_hmac"], p["node_code"], p["governorate"],
                             p["metric_type"], p["value"], p["iso_timestamp"])
        assert not verify_packet("other-secret", p["signature_hmac"], p["node_code"], p["governorate"],
                                 p["metric_type"], p["value"], p["iso_timestamp"])
    # cold-chain temperature only for insulin
    assert {p["node_code"].split("-")[-1] for p in packets if p["metric_type"] == "COLD_CHAIN_TEMP"} == {"A10AE04"}


def test_default_window_never_reaches_the_future():
    packets = sim.generate_telemetry_batch(hours=24)
    newest = max(datetime.fromisoformat(p["iso_timestamp"]) for p in packets)
    assert newest < datetime.now(dt_tz.utc)


def test_stock_dynamics_and_injected_anomalies():
    packets = sim.generate_telemetry_batch(hours=96, start=START)
    stock = [p["value"] for p in packets if p["node_code"] == "KAS-PHA-01-A10BA02" and p["metric_type"] == "STOCK_LEVEL"]
    assert min(stock) >= 0 and stock[0] > stock[40]  # depletes without replenishment inside 40 h
    kinds = {p["anomaly_kind"] for p in packets if p["is_anomaly_injected"]}
    assert kinds == {"ABNORMAL_CONSUMPTION", "SUPPLY_DISRUPTION", "COLD_CHAIN_EXCURSION"}
    clean = sim.generate_telemetry_batch(hours=96, start=START, inject_anomalies=False)
    assert not any(p["is_anomaly_injected"] for p in clean)

    def rate(batch, window):
        vals = [p["value"] for p in batch if p["node_code"] == "KAS-PHA-01-A10BA02" and p["metric_type"] == "DISPENSED_UNITS"
                and p["hour"] in window]
        return sum(vals)
    window = range(48, 54)
    assert rate(packets, window) > 2.5 * rate(clean, window)  # hoarding-style spike is clearly visible
    # the disrupted insulin position actually runs dry
    disrupted = [p["value"] for p in packets if p["node_code"] == "KAS-HOP-01-A10AE04" and p["metric_type"] == "STOCK_LEVEL"]
    assert disrupted[-1] < disrupted[0] * 0.5
    temps = [p["value"] for p in packets if p["node_code"] == "TUN-HOP-01-A10AE04" and p["metric_type"] == "COLD_CHAIN_TEMP"]
    assert max(temps) > 10 and sorted(temps)[len(temps) // 2] < 8  # brief excursion above the 2-8 C band, normal otherwise


# ------------------------------------------------------------------------------ end-to-end
@pytest.fixture
def seeded_server():
    server, port = build_server(0)
    server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.stop(0).wait()


@pytest.mark.django_db(transaction=True)
def test_simulator_to_grpc_to_postgresql_end_to_end(seeded_server, monkeypatch):
    call_command("seed_tunihara", prescriptions=0)
    assert MedicationInventory.objects.count() == len(seed_data.build_nodes())
    agent = StakeholderUser.objects.get(username="agent_service")
    token = token_for(agent)

    packets = sim.generate_telemetry_batch(hours=6, secret_key=settings.TELEMETRY_HMAC_SECRET)
    ack = sim.stream_to_grpc(packets, seeded_server, token)
    assert ack.accepted_count == len(packets) > 1000
    assert (ack.rejected_count, ack.duplicate_count) == (0, 0)
    assert TelemetryReading.objects.count() == len(packets)
    assert connection.vendor == "postgresql"

    # the inventory snapshot follows the newest simulated STOCK_LEVEL
    inv = MedicationInventory.objects.get(node_code="KAS-PHA-01-A10BA02")
    last = [p for p in packets if p["node_code"] == inv.node_code and p["metric_type"] == "STOCK_LEVEL"][-1]
    assert inv.current_stock_units == pytest.approx(last["value"], abs=1e-3)
    assert TelemetryReading.objects.filter(is_simulated=False).count() == 0

    # the simulator CLI path (login token supplied) is idempotent against the same window
    monkeypatch.setenv("TELEMETRY_HMAC_SECRET", settings.TELEMETRY_HMAC_SECRET)
    start = min(p["iso_timestamp"] for p in packets)
    code = sim.main(["--mode", "grpc", "--hours", "6", "--start", start, "--target", seeded_server, "--token", token])
    assert code == 0
    assert TelemetryReading.objects.count() == len(packets)  # replay produced duplicates, no new rows


@pytest.mark.django_db(transaction=True)
def test_simulator_with_wrong_secret_is_rejected_by_server(seeded_server):
    call_command("seed_tunihara", prescriptions=0)
    token = token_for(StakeholderUser.objects.get(username="agent_service"))
    packets = sim.generate_telemetry_batch(hours=1, secret_key="not-the-server-secret")
    ack = sim.stream_to_grpc(packets, seeded_server, token)
    assert ack.accepted_count == 0 and ack.rejected_count == len(packets)
    assert TelemetryReading.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_simulator_without_credentials_is_refused(seeded_server):
    call_command("seed_tunihara", prescriptions=0)
    packets = sim.generate_telemetry_batch(hours=1, secret_key=settings.TELEMETRY_HMAC_SECRET)
    with pytest.raises(grpc.RpcError) as err:
        sim.stream_to_grpc(packets, seeded_server, "")
    assert err.value.code() == grpc.StatusCode.UNAUTHENTICATED


# ------------------------------------------------------------------------------ seed command
@pytest.mark.django_db
def test_seed_command_is_idempotent_and_synthetic():
    call_command("seed_tunihara", prescriptions=60)
    counts = (HealthcareFacility.objects.count(), Medication.objects.count(), MedicationInventory.objects.count(),
              StakeholderUser.objects.count(), Prescription.objects.count())
    call_command("seed_tunihara", prescriptions=60)
    assert counts == (HealthcareFacility.objects.count(), Medication.objects.count(), MedicationInventory.objects.count(),
                      StakeholderUser.objects.count(), Prescription.objects.count())
    assert counts[:4] == (12, 7, len(seed_data.build_nodes()), 6)
    assert not HealthcareFacility.objects.filter(is_simulated=False).exists()
    assert not Prescription.objects.filter(is_simulated=False).exists()
    assert all(len(p) == 64 for p in Prescription.objects.values_list("patient_pseudonym", flat=True))
    assert set(StakeholderUser.objects.values_list("role", flat=True)) == {
        "COOP_MEMBER", "REGIONAL_AUDITOR", "NATIONAL_ADMIN", "AGENT_SERVICE"}
    assert set(Medication.objects.values_list("cnam_subsidy_status", flat=True)) == {"ILLUSTRATIVE_SUBSIDISED"}
