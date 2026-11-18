"""GraphQL: authentication, scoping, nested queries and DataLoader (constant SQL query count)."""
import asyncio
import json
from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.db import connection, connections
from django.test import AsyncClient
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from backend.tunisovereign_core.models import AnomalyAndForecastEvent, Medication, TelemetryReading
from backend.tunisovereign_core.schema_graphql import GQLContext, make_loaders, schema

from .conftest import make_facility, make_inventory, token_for

NESTED = """
{
  facilities {
    code governorate
    inventory {
      nodeCode currentStockUnits
      medication { dciName requiresColdChain }
      facility { code }
      latestReadings { metricType value }
      anomalies { anomalyType severity }
    }
  }
}
"""


async def run_counted(query, user, variables=None):
    """Execute ``query`` and count SQL statements. DB work runs in asgiref's single sync thread, so the
    capture context is opened/closed in that same thread."""
    holder = {}

    def begin():
        holder["ctx"] = CaptureQueriesContext(connection)
        holder["ctx"].__enter__()

    def end():
        holder["ctx"].__exit__(None, None, None)
        return len(holder["ctx"])

    await sync_to_async(begin)()
    try:
        result = await schema.execute(
            query, variable_values=variables, context_value=GQLContext(request=None, user=user, loaders=make_loaders())
        )
    finally:
        count = await sync_to_async(end)()
        await sync_to_async(connections.close_all)()
    return result, count


def gql(query, user, variables=None):
    return asyncio.run(run_counted(query, user, variables))


def populate(n_facilities, per_facility_inventory=3, prefix="X"):
    meds = [
        Medication.objects.get_or_create(atc_code=f"T{i:02d}", defaults=dict(dci_name=f"Med{i}", therapeutic_class="T"))[0]
        for i in range(per_facility_inventory)
    ]
    now = timezone.now() - timedelta(hours=1)
    for f in range(n_facilities):
        fac = make_facility(f"{prefix}{f:02d}-PHA-01", "Kasserine")
        for med in meds:
            inv = make_inventory(fac, med)
            for metric, value in (("STOCK_LEVEL", 50), ("DISPENSED_UNITS", 3)):
                for k in range(3):
                    TelemetryReading.objects.create(inventory=inv, timestamp=now - timedelta(minutes=k), metric_type=metric, value=value + k)
            AnomalyAndForecastEvent.objects.create(inventory=inv, anomaly_type="STOCKOUT_RISK", severity="LOW")


@pytest.mark.django_db(transaction=True)
def test_nested_query_returns_data(world):
    TelemetryReading.objects.create(
        inventory=world.inv.kas1_ins, timestamp=timezone.now() - timedelta(minutes=2), metric_type="COLD_CHAIN_TEMP", value=5.2
    )
    result, _ = gql(NESTED, world.users.admin)
    assert result.errors is None
    facilities = {f["code"]: f for f in result.data["facilities"]}
    assert set(facilities) == {"KAS-PHA-01", "KAS-HOP-01", "SFA-HOP-01"}
    kas1 = facilities["KAS-PHA-01"]["inventory"]
    assert {i["nodeCode"] for i in kas1} == {"KAS-PHA-01-A10BA02", "KAS-PHA-01-A10AE04"}
    insulin = next(i for i in kas1 if i["nodeCode"].endswith("A10AE04"))
    assert insulin["medication"]["requiresColdChain"] is True
    assert insulin["facility"]["code"] == "KAS-PHA-01"
    assert insulin["latestReadings"] == [{"metricType": "COLD_CHAIN_TEMP", "value": 5.2}]


@pytest.mark.django_db(transaction=True)
def test_graphql_respects_role_scope(world):
    u = world.users
    codes = lambda user: {f["code"] for f in gql("{ facilities { code } }", user)[0].data["facilities"]}  # noqa: E731
    assert codes(u.coop_kas) == {"KAS-PHA-01"}
    assert codes(u.auditor_kas) == {"KAS-PHA-01", "KAS-HOP-01"}
    assert codes(u.auditor_sfa) == {"SFA-HOP-01"}
    assert codes(u.admin) == {"KAS-PHA-01", "KAS-HOP-01", "SFA-HOP-01"}
    assert codes(u.coop_nofac) == set()
    # nested data of an out-of-scope facility cannot be reached through the inventory root either
    inv, _ = gql("{ inventory { nodeCode } }", u.coop_kas)
    assert {i["nodeCode"] for i in inv.data["inventory"]} == {"KAS-PHA-01-A10BA02", "KAS-PHA-01-A10AE04"}
    risk, _ = gql("{ shortageRiskByGovernorate { governorate monitoredPositions belowReorderThreshold } }", u.auditor_kas)
    assert risk.data["shortageRiskByGovernorate"] == [
        {"governorate": "Kasserine", "monitoredPositions": 3, "belowReorderThreshold": 0}
    ]


@pytest.mark.django_db(transaction=True)
def test_dataloader_prevents_n_plus_one(world):
    """Query count must be constant (independent of the number of facilities / inventories)."""
    populate(2, prefix="A")
    _, small = gql(NESTED, world.users.admin)
    populate(12, prefix="B")
    result, large = gql(NESTED, world.users.admin)
    assert result.errors is None
    assert len(result.data["facilities"]) == 3 + 2 + 12
    print(f"DATALOADER sql_queries: 5 facilities={small}, 17 facilities={large}")
    # 1 root query + one batched query per DataLoader (inventory, medication, facility, readings, anomalies)
    assert small == large <= 6, f"query count grew with data: {small} -> {large}"


@pytest.mark.django_db(transaction=True)
def test_telemetry_query_scoped_and_limited(world):
    now = timezone.now() - timedelta(hours=3)
    for k in range(5):
        TelemetryReading.objects.create(inventory=world.inv.kas1_met, timestamp=now + timedelta(minutes=k), metric_type="STOCK_LEVEL", value=k)
        TelemetryReading.objects.create(inventory=world.inv.sfa_met, timestamp=now + timedelta(minutes=k), metric_type="STOCK_LEVEL", value=k)
    q = "query($i: ID!, $l: Int){ telemetry(inventoryId: $i, limit: $l) { value metricType } }"
    ok, _ = gql(q, world.users.coop_kas, {"i": str(world.inv.kas1_met.pk), "l": 3})
    assert [r["value"] for r in ok.data["telemetry"]] == [4.0, 3.0, 2.0]
    hidden, _ = gql(q, world.users.coop_kas, {"i": str(world.inv.sfa_met.pk), "l": 3})
    assert hidden.data["telemetry"] == []
    bad, _ = gql(q, world.users.admin, {"i": "abc", "l": 3})
    assert bad.errors


@pytest.mark.django_db(transaction=True)
def test_query_depth_limit(world):
    deep = "{ facilities { inventory { facility { inventory { facility { inventory { facility { inventory { facility { code } } } } } } } } } }"
    result, _ = gql(deep, world.users.admin)
    assert result.errors and "depth" in result.errors[0].message.lower()


@pytest.mark.django_db(transaction=True)
def test_graphql_http_endpoint_requires_jwt(world):
    async def call(headers):
        client = AsyncClient()
        try:
            return await client.post(
                "/graphql/", json.dumps({"query": "{ me { username role } }"}), content_type="application/json", **headers
            )
        finally:
            await sync_to_async(connections.close_all)()

    anonymous = asyncio.run(call({}))
    assert anonymous.status_code == 401
    forged = asyncio.run(call({"headers": {"Authorization": "Bearer not.a.jwt"}}))
    assert forged.status_code == 401
    ok = asyncio.run(call({"headers": {"Authorization": f"Bearer {token_for(world.users.auditor_kas)}"}}))
    assert ok.status_code == 200
    assert ok.json()["data"]["me"] == {"username": "auditor_kas", "role": "REGIONAL_AUDITOR"}
    nested = asyncio.run(call({"headers": {"Authorization": f"Bearer {token_for(world.users.coop_kas)}"}}))
    assert nested.status_code == 200
