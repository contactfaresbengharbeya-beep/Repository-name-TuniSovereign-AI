"""Test settings: synthetic secrets (never used outside pytest), LocMem cache, relaxed throttling."""
import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-not-for-any-real-deployment-0123456789")
os.environ.setdefault("TELEMETRY_HMAC_SECRET", "test-only-telemetry-hmac-secret")
# POSTGRES_* come from the environment or the git-ignored .env (loaded by config.settings).
os.environ["CACHE_BACKEND"] = "locmem"
os.environ.setdefault("THROTTLE_AUTH_RATE", "1000/min")

from .settings import *  # noqa: E402,F401,F403

DEBUG = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # speed only, tests only
