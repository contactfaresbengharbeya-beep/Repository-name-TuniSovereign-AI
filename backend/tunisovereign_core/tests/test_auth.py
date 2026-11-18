import pytest
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from .conftest import PASSWORD, client_for

ROLE_USERS = {
    "COOP_MEMBER": "coop_kas",
    "REGIONAL_AUDITOR": "auditor_kas",
    "NATIONAL_ADMIN": "nat_admin",
    "AGENT_SERVICE": "agent_svc",
}


@pytest.mark.parametrize("role,username", ROLE_USERS.items())
def test_four_roles_login_and_claims(world, role, username):
    response = APIClient().post("/api/auth/login/", {"username": username, "password": PASSWORD}, format="json")
    assert response.status_code == 200
    claims = AccessToken(response.data["access"])
    assert claims["role"] == role
    for claim in ("governorate", "facility_id", "wallet_address", "preferred_dialect"):
        assert claim in claims.payload
    me = APIClient()
    me.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
    assert me.get("/api/auth/me/").data["role"] == role


def test_coop_member_token_carries_facility(world):
    resp = APIClient().post("/api/auth/login/", {"username": "coop_kas", "password": PASSWORD}, format="json")
    assert AccessToken(resp.data["access"])["facility_id"] == str(world.kas1.pk)


def test_jwt_refresh(world):
    client = APIClient()
    login = client.post("/api/auth/login/", {"username": "auditor_kas", "password": PASSWORD}, format="json")
    refreshed = client.post("/api/auth/refresh/", {"refresh": login.data["refresh"]}, format="json")
    assert refreshed.status_code == 200
    new_claims = AccessToken(refreshed.data["access"])
    assert new_claims["role"] == "REGIONAL_AUDITOR" and new_claims["governorate"] == "Kasserine"
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {refreshed.data['access']}")
    assert client.get("/api/v1/facilities/").status_code == 200


def test_refresh_rejects_garbage_and_access_token(world):
    client = APIClient()
    assert client.post("/api/auth/refresh/", {"refresh": "not-a-token"}, format="json").status_code == 401
    login = client.post("/api/auth/login/", {"username": "nat_admin", "password": PASSWORD}, format="json")
    # an access token must not be usable as a refresh token
    assert client.post("/api/auth/refresh/", {"refresh": login.data["access"]}, format="json").status_code == 401


def test_wrong_password_and_inactive_user_cannot_login(world):
    client = APIClient()
    assert client.post("/api/auth/login/", {"username": "coop_kas", "password": "nope"}, format="json").status_code == 401
    world.users.coop_kas.is_active = False
    world.users.coop_kas.save()
    assert client.post("/api/auth/login/", {"username": "coop_kas", "password": PASSWORD}, format="json").status_code == 401


@pytest.mark.parametrize("path", [
    "/api/v1/facilities/", "/api/v1/medications/", "/api/v1/inventory/", "/api/v1/telemetry/",
    "/api/v1/prescriptions/", "/api/v1/anomalies/", "/api/v1/redistribution-requests/",
    "/api/v1/agent-executions/", "/api/v1/zk-receipts/", "/api/v1/users/", "/api/auth/me/",
])
def test_unauthenticated_access_is_rejected(world, path):
    assert APIClient().get(path).status_code == 401


def test_invalid_and_expired_tokens_rejected(world):
    bad = APIClient()
    bad.credentials(HTTP_AUTHORIZATION="Bearer abc.def.ghi")
    assert bad.get("/api/v1/facilities/").status_code == 401
    from datetime import timedelta
    from rest_framework_simplejwt.tokens import AccessToken as AT
    expired = AT.for_user(world.users.admin)
    expired.set_exp(lifetime=-timedelta(seconds=5))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {expired}")
    assert client.get("/api/v1/facilities/").status_code == 401


def test_register_always_creates_dataless_coop_member(world):
    payload = {
        "username": "new_pharmacist", "password": "A-very-Strong-pass-77", "governorate": "Kasserine",
        # privilege-escalation attempt: these must be ignored
        "role": "NATIONAL_ADMIN", "facility": str(world.kas1.pk), "is_staff": True, "is_superuser": True,
    }
    response = APIClient().post("/api/auth/register/", payload, format="json")
    assert response.status_code == 201
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.get(username="new_pharmacist")
    assert user.role == "COOP_MEMBER" and user.facility is None
    assert not user.is_staff and not user.is_superuser
    client = client_for(user)
    assert client.get("/api/v1/facilities/").data["results"] == []  # no facility assigned -> sees nothing
    assert client.get("/api/v1/inventory/").data["results"] == []


def test_register_validation(world):
    client = APIClient()
    assert client.post("/api/auth/register/", {"username": "x1", "password": "123", "governorate": "Kasserine"}, format="json").status_code == 400
    assert client.post("/api/auth/register/", {"username": "coop_kas", "password": "A-very-Strong-pass-77", "governorate": "Kasserine"}, format="json").status_code == 400
    assert client.post("/api/auth/register/", {"username": "x2", "password": "A-very-Strong-pass-77", "governorate": "Atlantis"}, format="json").status_code == 400


def test_auth_endpoints_are_throttled(world, monkeypatch):
    from django.core.cache import cache
    from rest_framework.throttling import ScopedRateThrottle
    cache.clear()
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"auth": "3/min"})
    client = APIClient()
    codes = [client.post("/api/auth/login/", {"username": "coop_kas", "password": "bad"}, format="json").status_code for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and 429 in codes[3:]
    cache.clear()


def test_health_endpoint_is_public(db):
    response = APIClient().get("/api/v1/health/")
    assert response.status_code == 200 and response.data["database"] == "ok"
