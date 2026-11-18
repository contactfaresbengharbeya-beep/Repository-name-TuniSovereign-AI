"""
HMAC-SHA256 packet signing shared by the simulator (client) and the ingestion service (server).

Pure Python on purpose (no Django imports). The signature protects integrity/authenticity of a packet;
it does NOT provide confidentiality or replace TLS, and it is not a freshness proof by itself
(replays are neutralised by the unique (inventory, metric, timestamp) constraint).
"""
import hashlib
import hmac


def canonical_payload(node_code: str, governorate: str, metric_type: str, value: float, iso_timestamp: str) -> str:
    return f"{node_code}|{governorate}|{metric_type}|{float(value):.4f}|{iso_timestamp}"


def sign_packet(secret: str, node_code: str, governorate: str, metric_type: str, value: float, iso_timestamp: str) -> str:
    payload = canonical_payload(node_code, governorate, metric_type, value, iso_timestamp)
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_packet(
    secret: str, signature: str, node_code: str, governorate: str, metric_type: str, value: float, iso_timestamp: str
) -> bool:
    expected = sign_packet(secret, node_code, governorate, metric_type, value, iso_timestamp)
    return hmac.compare_digest(expected, signature or "")
