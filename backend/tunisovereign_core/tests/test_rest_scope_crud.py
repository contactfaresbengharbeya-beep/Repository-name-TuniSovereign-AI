"""Facility/governorate isolation, role matrix, CRUD and validation on the REST API."""
from datetime import timedelta

import pytest
from django.utils import timezone

from backend.tunisovereign_core.models import (
    AgentLoopExecution,
    AnomalyAndForecastEvent,
    OnChainZKReceipt,
    Prescription,
    RedistributionRequest,
    TelemetryReading,
)

from .conftest import client_for

V1 = "/api/v1"


def ids(response):
    return {str(row["id"]) for row in response.data["results"]}


# --------------------------------------------------------------------------- isolation
def test_facility_isolation_between_roles(world):
    u = world.users
    assert ids(client_for(u.coop_kas).get(f"{V1}/facilities/")) == {str(world.kas1.pk)}
    assert ids(client_for(u.coop_sfa).get(f"{V1}/facilities/")) == {str(world.sfa.pk)}
    assert ids(client_for(u.auditor_kas).get(f"{V1}/facilities/")) == {str(world.kas1.pk), str(world.kas2.pk)}
    assert ids(client_for(u.auditor_sfa).get(f"{V1}/facilities/")) == {str(world.sfa.pk)}
    everything = {str(world.kas1.pk), str(world.kas2.pk), str(world.sfa.pk)}
    assert ids(client_for(u.admin).get(f"{V1}/facilities/")) == everything
    assert ids(client_for(u.agent).get(f"{V1}/facilities/")) == everything
    assert client_for(u.coop_nofac).get(f"{V1}/facilities/").data["results"] == []


def test_out_of_scope_objects_are_404_not_403(world):
    coop = client_for(world.users.coop_kas)
    assert coop.get(f"{V1}/facilities/{world.sfa.pk}/").status_code == 404
    assert coop.get(f"{V1}/inventory/{world.inv.sfa_met.pk}/").status_code == 404
    assert coop.get(f"{V1}/facilities/{world.kas1.pk}/").status_code == 200


def test_inventory_and_telemetry_lists_are_scoped(world):
    now = timezone.now() - timedelta(hours=1)
    for inv in (world.inv.kas1_met, world.inv.sfa_met):
        TelemetryReading.objects.create(inventory=inv, timestamp=now, metric_type="STOCK_LEVEL", value=10)
    coop = client_for(world.users.coop_kas)
    assert ids(coop.get(f"{V1}/inventory/")) == {str(world.inv.kas1_met.pk), str(world.inv.kas1_ins.pk)}
    assert len(coop.get(f"{V1}/telemetry/").data["results"]) == 1
    assert len(client_for(world.users.admin).get(f"{V1}/telemetry/").data["results"]) == 2
    # a COOP_MEMBER cannot reach another facility's data by filtering either
    assert coop.get(f"{V1}/telemetry/?inventory={world.inv.sfa_met.pk}").data["results"] == []
    assert coop.get(f"{V1}/inventory/?facility={world.sfa.pk}").data["results"] == []


def test_malformed_filters_are_400(world):
    admin = client_for(world.users.admin)
    assert admin.get(f"{V1}/inventory/?facility=not-a-uuid").status_code == 400
    assert admin.get(f"{V1}/telemetry/?from=yesterday").status_code == 400
    assert admin.get(f"{V1}/telemetry/?inventory=abc").status_code == 400


def test_pagination_limit_is_capped(world):
    data = client_for(world.users.admin).get(f"{V1}/telemetry/?limit=100000").data
    assert "results" in data and "count" in data


# --------------------------------------------------------------------------- facilities / medications CRUD
def test_facility_crud_admin_only(world):
    new = {"code": "GAF-HOP-01", "name": "Hôpital (simulé)", "facility_type": "HOSPITAL", "governorate": "Gafsa"}
    for user in (world.users.coop_kas, world.users.auditor_kas, world.users.agent):
        assert client_for(user).post(f"{V1}/facilities/", new, format="json").status_code == 403
    admin = client_for(world.users.admin)
    created = admin.post(f"{V1}/facilities/", new, format="json")
    assert created.status_code == 201 and created.data["track_domain"] == "Track_05_Tunihara_MedGuard"
    assert admin.patch(f"{V1}/facilities/{created.data['id']}/", {"name": "Renamed (simulé)"}, format="json").status_code == 200
    assert client_for(world.users.coop_kas).delete(f"{V1}/facilities/{world.kas1.pk}/").status_code == 403
    assert admin.delete(f"{V1}/facilities/{created.data['id']}/").status_code == 204
    bad = dict(new, code="X-1", governorate="Atlantis")
    assert admin.post(f"{V1}/facilities/", bad, format="json").status_code == 400
    assert admin.post(f"{V1}/facilities/", new, format="json").status_code == 201  # recreate: unique code was freed
    assert admin.post(f"{V1}/facilities/", new, format="json").status_code == 400  # duplicate code


def test_medications_readable_by_all_writable_by_admin(world):
    payload = {"dci_name": "Amlodipine", "atc_code": "C08CA01", "therapeutic_class": "HYPERTENSION"}
    for user in (world.users.coop_kas, world.users.auditor_kas, world.users.agent):
        assert client_for(user).get(f"{V1}/medications/").status_code == 200
        assert client_for(user).post(f"{V1}/medications/", payload, format="json").status_code == 403
    assert client_for(world.users.admin).post(f"{V1}/medications/", payload, format="json").status_code == 201


# --------------------------------------------------------------------------- inventory
def test_inventory_update_rules(world):
    coop = client_for(world.users.coop_kas)
    url = f"{V1}/inventory/{world.inv.kas1_met.pk}/"
    assert coop.patch(url, {"reorder_threshold_units": 55, "is_redistribution_hold": True}, format="json").status_code == 200
    # stock level is server-controlled: silently ignored
    coop.patch(url, {"current_stock_units": 999999}, format="json")
    world.inv.kas1_met.refresh_from_db()
    assert world.inv.kas1_met.current_stock_units == 100.0 and world.inv.kas1_met.reorder_threshold_units == 55
    # identity fields are immutable
    assert coop.patch(url, {"node_code": "HACK-0001"}, format="json").status_code == 400
    assert coop.patch(f"{V1}/inventory/{world.inv.sfa_met.pk}/", {"safety_stock_days": 1}, format="json").status_code == 404
    assert client_for(world.users.auditor_kas).patch(url, {"safety_stock_days": 1}, format="json").status_code == 403
    assert coop.delete(url).status_code == 403
    assert coop.post(f"{V1}/inventory/", {}, format="json").status_code == 403


# --------------------------------------------------------------------------- telemetry
def telemetry_payload(inv, **kw):
    payload = {
        "inventory": inv.pk, "timestamp": (timezone.now() - timedelta(minutes=5)).isoformat(),
        "metric_type": "STOCK_LEVEL", "value": 42.0,
    }
    payload.update(kw)
    return payload


def test_telemetry_create_rules_and_server_controlled_fields(world):
    inv = world.inv.kas1_met
    coop = client_for(world.users.coop_kas)
    response = coop.post(
        f"{V1}/telemetry/",
        telemetry_payload(inv, signature_verified=True, ingestion_protocol="gRPC"),
        format="json",
    )
    assert response.status_code == 201
    row = TelemetryReading.objects.get()
    assert row.signature_verified is False and row.ingestion_protocol == "REST"  # clients cannot forge these
    inv.refresh_from_db()
    assert inv.current_stock_units == 42.0 and inv.last_reading_at is not None
    assert client_for(world.users.auditor_kas).post(f"{V1}/telemetry/", telemetry_payload(inv), format="json").status_code == 403
    assert coop.post(f"{V1}/telemetry/", telemetry_payload(world.inv.sfa_met), format="json").status_code == 400
    assert client_for(world.users.agent).post(f"{V1}/telemetry/", telemetry_payload(world.inv.sfa_met, value=5), format="json").status_code == 201
    assert coop.delete(f"{V1}/telemetry/{row.pk}/").status_code == 403
    assert coop.patch(f"{V1}/telemetry/{row.pk}/", {"value": 1}, format="json").status_code == 405  # immutable


@pytest.mark.parametrize("override", [
    {"value": -1}, {"metric_type": "COLD_CHAIN_TEMP", "value": 500}, {"metric_type": "SOIL_MOISTURE"},
    {"timestamp": "2999-01-01T00:00:00+00:00"},
])
def test_telemetry_validation_rejects_bad_values(world, override):
    response = client_for(world.users.agent).post(
        f"{V1}/telemetry/", telemetry_payload(world.inv.kas1_met, **override), format="json"
    )
    assert response.status_code == 400
    assert TelemetryReading.objects.count() == 0


def test_telemetry_duplicate_rejected(world):
    agent = client_for(world.users.agent)
    payload = telemetry_payload(world.inv.kas1_met)
    assert agent.post(f"{V1}/telemetry/", payload, format="json").status_code == 201
    assert agent.post(f"{V1}/telemetry/", payload, format="json").status_code == 400


# --------------------------------------------------------------------------- prescriptions
def rx_payload(facility, medication, **kw):
    payload = {
        "facility": str(facility.pk), "medication": medication.pk, "issued_at": timezone.now().isoformat(),
        "patient_pseudonym": "a" * 64, "quantity_units": 2, "cnam_coverage_category": "CHRONIC_FULL",
    }
    payload.update(kw)
    return payload


def test_prescriptions_pseudonymised_and_scoped(world):
    coop = client_for(world.users.coop_kas)
    assert coop.post(f"{V1}/prescriptions/", rx_payload(world.kas1, world.metformin), format="json").status_code == 201
    # real identifiers are structurally impossible
    for bad in ("Mohamed Ben Ali", "12345678", "A" * 64, "a" * 63):
        assert coop.post(f"{V1}/prescriptions/", rx_payload(world.kas1, world.metformin, patient_pseudonym=bad), format="json").status_code == 400
    assert coop.post(f"{V1}/prescriptions/", rx_payload(world.sfa, world.metformin), format="json").status_code == 400
    assert coop.post(f"{V1}/prescriptions/", rx_payload(world.kas1, world.metformin, quantity_units=0), format="json").status_code == 400
    assert client_for(world.users.auditor_kas).post(f"{V1}/prescriptions/", rx_payload(world.kas1, world.metformin), format="json").status_code == 403
    Prescription.objects.create(
        facility=world.sfa, medication=world.metformin, issued_at=timezone.now(), patient_pseudonym="b" * 64, quantity_units=1
    )
    assert len(coop.get(f"{V1}/prescriptions/").data["results"]) == 1
    assert len(client_for(world.users.admin).get(f"{V1}/prescriptions/").data["results"]) == 2
    assert not any(f.name in ("patient_name", "cin", "national_id") for f in Prescription._meta.get_fields())


# --------------------------------------------------------------------------- anomalies
def test_anomalies_created_by_agent_only(world):
    payload = {"inventory": world.inv.kas1_met.pk, "anomaly_type": "STOCKOUT_RISK", "severity": "CRITICAL",
               "forecasted_stockout_days": 3.5}
    for user in (world.users.coop_kas, world.users.auditor_kas):
        assert client_for(user).post(f"{V1}/anomalies/", payload, format="json").status_code == 403
    assert client_for(world.users.agent).post(f"{V1}/anomalies/", payload, format="json").status_code == 201
    AnomalyAndForecastEvent.objects.create(inventory=world.inv.sfa_met, anomaly_type="COLD_CHAIN", severity="LOW")
    assert len(client_for(world.users.coop_kas).get(f"{V1}/anomalies/").data["results"]) == 1
    assert len(client_for(world.users.auditor_sfa).get(f"{V1}/anomalies/").data["results"]) == 1
    assert len(client_for(world.users.admin).get(f"{V1}/anomalies/").data["results"]) == 2


# --------------------------------------------------------------------------- redistribution workflow
def rr_payload(src, dst, medication, **kw):
    payload = {"source_facility": str(src.pk), "destination_facility": str(dst.pk), "medication": medication.pk,
               "units": 25, "rationale": {"note": "synthetic"}}
    payload.update(kw)
    return payload


def test_redistribution_creation_rules(world):
    coop = client_for(world.users.coop_kas)
    created = coop.post(
        f"{V1}/redistribution-requests/",
        rr_payload(world.kas2, world.kas1, world.metformin, status="EXECUTED", proposed_by=world.users.admin.pk),
        format="json",
    )
    assert created.status_code == 201
    assert created.data["status"] == "PROPOSED" and created.data["proposed_by"] == world.users.coop_kas.pk
    assert coop.post(f"{V1}/redistribution-requests/", rr_payload(world.kas1, world.kas1, world.metformin), format="json").status_code == 400
    # a request between two facilities outside the caller's scope is refused
    assert coop.post(f"{V1}/redistribution-requests/", rr_payload(world.kas2, world.sfa, world.metformin), format="json").status_code == 403
    assert coop.post(f"{V1}/redistribution-requests/", rr_payload(world.kas2, world.kas1, world.metformin, units=-5), format="json").status_code == 400


def test_redistribution_approval_workflow(world):
    rr_local = RedistributionRequest.objects.create(
        source_facility=world.kas2, destination_facility=world.kas1, medication=world.metformin, units=10,
        proposed_by=world.users.admin)
    rr_cross = RedistributionRequest.objects.create(
        source_facility=world.kas2, destination_facility=world.sfa, medication=world.metformin, units=10,
        proposed_by=world.users.admin)
    url_local = f"{V1}/redistribution-requests/{rr_local.pk}/"
    url_cross = f"{V1}/redistribution-requests/{rr_cross.pk}/"
    assert client_for(world.users.coop_kas).patch(url_local, {"status": "APPROVED"}, format="json").status_code == 403
    auditor = client_for(world.users.auditor_kas)
    assert auditor.patch(url_local, {"status": "EXECUTED"}, format="json").status_code == 400  # invalid transition
    approved = auditor.patch(url_local, {"status": "APPROVED"}, format="json")
    assert approved.status_code == 200 and approved.data["approved_by"] == world.users.auditor_kas.pk
    assert auditor.patch(url_local, {"status": "EXECUTED"}, format="json").status_code == 403  # admin only
    assert auditor.patch(url_cross, {"status": "APPROVED"}, format="json").status_code == 403  # cross-governorate
    # an auditor of an unrelated governorate cannot even see it
    assert client_for(world.users.auditor_sfa).patch(url_local, {"status": "REJECTED"}, format="json").status_code == 404
    admin = client_for(world.users.admin)
    assert admin.patch(url_local, {"status": "EXECUTED"}, format="json").status_code == 200
    assert admin.patch(url_cross, {"status": "REJECTED"}, format="json").status_code == 200
    assert admin.patch(url_cross, {"status": "APPROVED"}, format="json").status_code == 400  # terminal state


def test_redistribution_visibility_scoped(world):
    RedistributionRequest.objects.create(source_facility=world.kas2, destination_facility=world.kas1, medication=world.metformin, units=1)
    assert len(client_for(world.users.coop_kas).get(f"{V1}/redistribution-requests/").data["results"]) == 1
    assert len(client_for(world.users.coop_sfa).get(f"{V1}/redistribution-requests/").data["results"]) == 0
    assert len(client_for(world.users.auditor_kas).get(f"{V1}/redistribution-requests/").data["results"]) == 1


# --------------------------------------------------------------------------- agent executions / ZK receipts
def test_agent_data_is_read_only_and_scoped(world):
    kas = AgentLoopExecution.objects.create(thread_id="t-kas", governorate="Kasserine", trigger_source="test",
                                            final_recommendation="x", confidence_score=0.5)
    sfa = AgentLoopExecution.objects.create(thread_id="t-sfa", governorate="Sfax", trigger_source="test",
                                            final_recommendation="x", confidence_score=0.5)
    OnChainZKReceipt.objects.create(agent_execution=kas, zk_commitment_hash="0x" + "1" * 64, tx_hash="0x" + "2" * 64, block_number=1)
    OnChainZKReceipt.objects.create(agent_execution=sfa, zk_commitment_hash="0x" + "3" * 64, tx_hash="0x" + "4" * 64, block_number=2)
    assert client_for(world.users.coop_kas).get(f"{V1}/agent-executions/").status_code == 403
    assert client_for(world.users.coop_kas).get(f"{V1}/zk-receipts/").status_code == 403
    assert {r["thread_id"] for r in client_for(world.users.auditor_kas).get(f"{V1}/agent-executions/").data["results"]} == {"t-kas"}
    assert len(client_for(world.users.auditor_kas).get(f"{V1}/zk-receipts/").data["results"]) == 1
    assert len(client_for(world.users.admin).get(f"{V1}/zk-receipts/").data["results"]) == 2
    assert OnChainZKReceipt.objects.first().is_verified_on_chain is False
    # clients can never write tx_hash & co.
    forged = {"agent_execution": kas.pk, "zk_commitment_hash": "0x" + "9" * 64, "tx_hash": "0x" + "8" * 64, "block_number": 9}
    for user in (world.users.admin, world.users.agent, world.users.auditor_kas):
        assert client_for(user).post(f"{V1}/zk-receipts/", forged, format="json").status_code == 405
        assert client_for(user).post(f"{V1}/agent-executions/", {}, format="json").status_code == 405


# --------------------------------------------------------------------------- user management
def test_user_admin_endpoint(world):
    payload = {"username": "auditor_new", "password": "Another-Strong-pass-5", "role": "REGIONAL_AUDITOR", "governorate": "Gafsa"}
    for user in (world.users.coop_kas, world.users.auditor_kas, world.users.agent):
        assert client_for(user).get(f"{V1}/users/").status_code == 403
        assert client_for(user).post(f"{V1}/users/", payload, format="json").status_code == 403
    admin = client_for(world.users.admin)
    created = admin.post(f"{V1}/users/", payload, format="json")
    assert created.status_code == 201 and created.data["role"] == "REGIONAL_AUDITOR" and "password" not in created.data
    assert admin.post(f"{V1}/users/", dict(payload, username="u2", password="123"), format="json").status_code == 400
    # facility must lie in the user's governorate
    wrong = admin.patch(f"{V1}/users/{world.users.coop_nofac.pk}/", {"facility": str(world.sfa.pk)}, format="json")
    assert wrong.status_code == 400
    ok = admin.patch(f"{V1}/users/{world.users.coop_nofac.pk}/", {"facility": str(world.kas2.pk)}, format="json")
    assert ok.status_code == 200
    assert admin.delete(f"{V1}/users/{world.users.coop_nofac.pk}/").status_code == 405
