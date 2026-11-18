"""Django system checks, migrations and real PostgreSQL / Redis connectivity."""
import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connection


def test_django_system_checks_pass(db):
    call_command("check")


def test_no_missing_migrations(db):
    call_command("makemigrations", "--check", "--dry-run")


def test_postgresql_connectivity(db):
    assert connection.vendor == "postgresql"
    with connection.cursor() as cursor:
        cursor.execute("SELECT version()")
        assert cursor.fetchone()[0].startswith("PostgreSQL")
    assert settings.AUTH_USER_MODEL == "tunisovereign_core.StakeholderUser"


def test_initial_migration_applied_all_tables(db):
    with connection.cursor() as cursor:
        tables = set(connection.introspection.table_names(cursor))
    expected = {
        "tunisovereign_core_stakeholderuser", "tunisovereign_core_healthcarefacility", "tunisovereign_core_medication",
        "tunisovereign_core_medicationinventory", "tunisovereign_core_prescription", "tunisovereign_core_telemetryreading",
        "tunisovereign_core_anomalyandforecastevent", "tunisovereign_core_redistributionrequest",
        "tunisovereign_core_agentloopexecution", "tunisovereign_core_onchainzkreceipt",
    }
    assert expected <= tables
    assert not any("cooperativeunit" in t or "resourcenodeplot" in t for t in tables)  # starter names fully replaced


def _redis_available():
    try:
        import redis

        return redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2).ping()
    except Exception:
        return False


@pytest.mark.redis
@pytest.mark.skipif(not _redis_available(), reason="Redis is not reachable at REDIS_URL")
def test_redis_connectivity_and_django_cache_backend(settings):
    import redis

    client = redis.Redis.from_url(settings.REDIS_URL)
    assert client.ping() is True
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": settings.REDIS_URL}}
    from django.core.cache import caches

    cache = caches["default"]
    cache.set("tunihara:test", {"ok": True}, 10)
    assert cache.get("tunihara:test") == {"ok": True}
    cache.delete("tunihara:test")
