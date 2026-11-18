"""Shared fixtures. All data is synthetic; passwords exist only inside the throw-away test database."""
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from backend.tunisovereign_core.models import HealthcareFacility, Medication, MedicationInventory
from backend.tunisovereign_core.serializers import SovereignTokenObtainPairSerializer

PASSWORD = "Test-Only-Passw0rd!"
User = get_user_model()


def token_for(user) -> str:
    return str(SovereignTokenObtainPairSerializer.get_token(user).access_token)


def client_for(user=None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token_for(user)}")
    return client


def make_facility(code, gov, ftype="PHARMACY"):
    return HealthcareFacility.objects.create(code=code, name=f"{code} (simulé)", governorate=gov, facility_type=ftype)


def make_inventory(facility, medication, stock=100.0, threshold=70.0):
    return MedicationInventory.objects.create(
        facility=facility, medication=medication, node_code=f"{facility.code}-{medication.atc_code}",
        current_stock_units=stock, reorder_threshold_units=threshold,
    )


@pytest.fixture
def world(db):
    """Two Kasserine facilities, one Sfax facility, two medications, inventories and one user per role."""
    metformin = Medication.objects.create(dci_name="Metformin", atc_code="A10BA02", therapeutic_class="DIABETES")
    insulin = Medication.objects.create(
        dci_name="Insulin glargine", atc_code="A10AE04", therapeutic_class="DIABETES", requires_cold_chain=True
    )
    kas1 = make_facility("KAS-PHA-01", "Kasserine")
    kas2 = make_facility("KAS-HOP-01", "Kasserine", "HOSPITAL")
    sfa = make_facility("SFA-HOP-01", "Sfax", "HOSPITAL")
    inv = {
        "kas1_met": make_inventory(kas1, metformin),
        "kas1_ins": make_inventory(kas1, insulin),
        "kas2_met": make_inventory(kas2, metformin),
        "sfa_met": make_inventory(sfa, metformin),
    }
    users = SimpleNamespace(
        coop_kas=User.objects.create_user("coop_kas", password=PASSWORD, role="COOP_MEMBER", governorate="Kasserine", facility=kas1),
        coop_sfa=User.objects.create_user("coop_sfa", password=PASSWORD, role="COOP_MEMBER", governorate="Sfax", facility=sfa),
        coop_nofac=User.objects.create_user("coop_nofac", password=PASSWORD, role="COOP_MEMBER", governorate="Kasserine"),
        auditor_kas=User.objects.create_user("auditor_kas", password=PASSWORD, role="REGIONAL_AUDITOR", governorate="Kasserine"),
        auditor_sfa=User.objects.create_user("auditor_sfa", password=PASSWORD, role="REGIONAL_AUDITOR", governorate="Sfax"),
        admin=User.objects.create_user("nat_admin", password=PASSWORD, role="NATIONAL_ADMIN", governorate="Tunis"),
        agent=User.objects.create_user("agent_svc", password=PASSWORD, role="AGENT_SERVICE", governorate="Tunis"),
    )
    return SimpleNamespace(
        metformin=metformin, insulin=insulin, kas1=kas1, kas2=kas2, sfa=sfa, inv=SimpleNamespace(**inv), users=users
    )
