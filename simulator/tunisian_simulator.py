"""
Tunihara-MedGuard telemetry simulator (SYNTHETIC data only) — Week 1.

Generates hourly medication telemetry for every (facility x medication) position of the seed catalogue:
STOCK_LEVEL, DISPENSED_UNITS, RX_COUNT, CONSUMPTION_RATE (and COLD_CHAIN_TEMP for insulin), with
  * pharmacy opening hours, a Sunday dip, mild seasonality (respiratory medication peaks in winter),
  * periodic replenishment, and a supply-disruption scenario that produces a genuine stock-out trajectory,
  * labelled injected anomalies (ground truth for the Week 2 evaluation): abnormal consumption (hoarding
    style spike), supply disruption, cold-chain excursion,
  * HMAC-SHA256 signed packets, streamed to the REAL gRPC service (`--mode grpc`).

Usage (from the repo root, with the DB seeded and the gRPC server running):
    python -m simulator.tunisian_simulator --mode grpc --hours 24
    python -m simulator.tunisian_simulator --mode print --hours 6
"""
import argparse
import json
import math
import os
import random
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"), override=False)
except ImportError:  # pragma: no cover
    pass

from backend.tunisovereign_core import seed_data  # noqa: E402
from backend.tunisovereign_core.signing import sign_packet  # noqa: E402

TUNIS_UTC_OFFSET_HOURS = 1
OPENING_HOURS = {"PHARMACY": range(8, 20), "CLINIC": range(8, 17)}  # local time; hospitals/depots: all day
DELIVERY_EVERY_HOURS = 72
AVG_UNITS_PER_PRESCRIPTION = 1.8


def _hour_weights(facility_type: str) -> list:
    """Share of the daily demand dispensed in each local hour (sums to 1)."""
    open_hours = OPENING_HOURS.get(facility_type)
    weights = []
    for h in range(24):
        if open_hours is not None:
            w = 1.0 + 0.5 * math.sin(math.pi * (h - 8) / 12) if h in open_hours else 0.0
        else:
            w = 0.35 + 0.65 * max(0.0, math.sin(math.pi * (h - 6) / 14)) if 6 <= h <= 20 else 0.2
        weights.append(max(w, 0.0))
    total = sum(weights)
    return [w / total for w in weights]


def _seasonal_factor(dci_name: str, month: int) -> float:
    winter = math.cos(2 * math.pi * (month - 1) / 12)  # +1 in January, -1 in July
    amplitude = 0.30 if dci_name == "Salbutamol" else 0.05
    return 1.0 + amplitude * winter


def _anomaly_plan(nodes: list, hours: int) -> dict:
    """Deterministic ground-truth anomaly schedule (kind, first_hour, last_hour_exclusive) per node_code."""
    by_code = {n["node_code"]: n for n in nodes}
    plan = {}
    start = max(1, hours // 2)
    if "KAS-PHA-01-A10BA02" in by_code:
        plan["KAS-PHA-01-A10BA02"] = ("ABNORMAL_CONSUMPTION", start, min(hours, start + 6))
    if "KAS-HOP-01-A10AE04" in by_code:
        plan["KAS-HOP-01-A10AE04"] = ("SUPPLY_DISRUPTION", 0, hours)
    if "TUN-HOP-01-A10AE04" in by_code:
        cold_start = max(1, int(hours * 0.7))
        plan["TUN-HOP-01-A10AE04"] = ("COLD_CHAIN_EXCURSION", cold_start, min(hours, cold_start + 4))
    return plan


def generate_telemetry_batch(
    hours: int = 24,
    seed: int = 42,
    start: datetime = None,
    secret_key: str = None,
    nodes: list = None,
    inject_anomalies: bool = True,
) -> list:
    """Return signed packet dicts (proto field names + ground-truth columns), sorted by timestamp.

    ``start`` defaults to (now floored to the hour) - ``hours`` so that no packet lies in the future.
    Packets are signed only when ``secret_key`` is given.
    """
    nodes = nodes if nodes is not None else seed_data.build_nodes()
    if start is None:
        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours)
    plan = _anomaly_plan(nodes, hours) if inject_anomalies else {}
    packets = []

    for node in nodes:
        rng = random.Random(f"{seed}:{node['node_code']}")
        weights = _hour_weights(node["facility_type"])
        kind, a_from, a_to = plan.get(node["node_code"], (None, 0, 0))
        stock = node["initial_stock"] * (0.35 if kind == "SUPPLY_DISRUPTION" else 1.0)
        history = []

        for h in range(hours):
            ts = start + timedelta(hours=h)
            local_hour = (ts.hour + TUNIS_UTC_OFFSET_HOURS) % 24
            weekday = 0.4 if (ts.weekday() == 6 and node["facility_type"] in ("PHARMACY", "CLINIC")) else 1.0
            expected = node["daily_demand"] * weights[local_hour] * weekday * _seasonal_factor(node["dci_name"], ts.month)

            anomalous = kind is not None and a_from <= h < a_to
            if anomalous and kind == "ABNORMAL_CONSUMPTION":
                expected = max(expected, node["daily_demand"] / 12.0) * 4.0
            if kind == "SUPPLY_DISRUPTION":
                expected *= 1.4  # panic buying during a disruption

            dispensed = max(0.0, round(rng.gauss(expected, 0.15 * expected + 0.2)))
            if h % DELIVERY_EVERY_HOURS == DELIVERY_EVERY_HOURS - 1 and kind != "SUPPLY_DISRUPTION":
                stock += max(0.0, node["initial_stock"] - stock)  # replenish to target cover
            dispensed = min(dispensed, stock)
            stock -= dispensed
            history.append(dispensed)
            window = history[-24:]
            rate = sum(window) * 24.0 / len(window)

            metrics = [
                ("STOCK_LEVEL", stock),
                ("DISPENSED_UNITS", dispensed),
                ("RX_COUNT", float(round(dispensed / AVG_UNITS_PER_PRESCRIPTION))),
                ("CONSUMPTION_RATE", rate),
            ]
            if node["requires_cold_chain"]:
                temp = rng.gauss(5.0, 0.5)
                if anomalous and kind == "COLD_CHAIN_EXCURSION":
                    temp = rng.uniform(11.0, 16.0)
                metrics.append(("COLD_CHAIN_TEMP", temp))

            iso_ts = ts.isoformat()
            for metric, value in metrics:
                value = round(value, 4)
                packets.append({
                    "node_code": node["node_code"],
                    "governorate": node["governorate"],
                    "metric_type": metric,
                    "value": value,
                    "iso_timestamp": iso_ts,
                    "signature_hmac": sign_packet(secret_key, node["node_code"], node["governorate"], metric, value, iso_ts)
                    if secret_key else "",
                    "unit": "C" if metric == "COLD_CHAIN_TEMP" else node["unit"],
                    "is_simulated": True,
                    "hour": h,
                    "is_anomaly_injected": anomalous,
                    "anomaly_kind": kind if anomalous else "",
                })
    packets.sort(key=lambda p: (p["iso_timestamp"], p["node_code"], p["metric_type"]))
    return packets


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def fetch_access_token(api_url: str, username: str, password: str) -> str:
    """Log in through the REST API as the AGENT_SERVICE account and return a JWT access token."""
    request = urllib.request.Request(
        api_url.rstrip("/") + "/api/auth/login/",
        data=json.dumps({"username": username, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read())["access"]
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not obtain a token from {api_url}: {exc}")


def stream_to_grpc(packets: list, target: str, access_token: str, tls_ca_file: str = "", timeout: float = 300.0):
    """Stream packets to the real TelemetryIngestService.StreamTelemetry RPC; returns the IngestAck."""
    import grpc

    from grpc_service import tunisovereign_pb2 as pb
    from grpc_service import tunisovereign_pb2_grpc as pb_grpc

    if tls_ca_file:
        with open(tls_ca_file, "rb") as fh:
            channel = grpc.secure_channel(target, grpc.ssl_channel_credentials(root_certificates=fh.read()))
    else:
        channel = grpc.insecure_channel(target)

    def messages():
        for p in packets:
            yield pb.TelemetryPacket(
                node_code=p["node_code"], governorate=p["governorate"], metric_type=p["metric_type"],
                value=p["value"], iso_timestamp=p["iso_timestamp"], signature_hmac=p["signature_hmac"],
                unit=p["unit"], is_simulated=p["is_simulated"],
            )

    with channel:
        stub = pb_grpc.TelemetryIngestServiceStub(channel)
        return stub.StreamTelemetry(
            messages(), metadata=(("authorization", f"Bearer {access_token}"),), timeout=timeout
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Tunihara-MedGuard synthetic telemetry simulator")
    parser.add_argument("--mode", choices=["print", "jsonl", "grpc"], default="print")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start", help="ISO-8601 start time (timezone-aware); default = now - hours")
    parser.add_argument("--no-anomalies", action="store_true")
    parser.add_argument("--target", default=os.environ.get("SIMULATOR_GRPC_TARGET", "127.0.0.1:50051"))
    parser.add_argument("--api-url", default=os.environ.get("SIMULATOR_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("AGENT_SERVICE_TOKEN", ""))
    args = parser.parse_args(argv)

    secret = os.environ.get("TELEMETRY_HMAC_SECRET", "")
    if args.mode == "grpc" and not secret:
        raise SystemExit("TELEMETRY_HMAC_SECRET must be set (same value as the server) to sign packets.")
    start = datetime.fromisoformat(args.start) if args.start else None
    packets = generate_telemetry_batch(
        hours=args.hours, seed=args.seed, start=start, secret_key=secret or None,
        inject_anomalies=not args.no_anomalies,
    )
    injected = [p for p in packets if p["is_anomaly_injected"]]
    nodes = {p["node_code"] for p in packets}
    print(f"Generated {len(packets)} SYNTHETIC packets for {len(nodes)} medication positions over {args.hours} h "
          f"({len(injected)} labelled anomaly packets).")

    if args.mode == "print":
        print("Sample packet:", {k: v for k, v in packets[0].items() if k != "signature_hmac"})
    elif args.mode == "jsonl":
        for p in packets:
            print(json.dumps({k: v for k, v in p.items() if k != "signature_hmac"}))
    else:
        token = args.token or fetch_access_token(
            args.api_url, os.environ.get("SIMULATOR_USERNAME", "agent_service"), os.environ.get("SIMULATOR_PASSWORD", "")
        )
        ack = stream_to_grpc(packets, args.target, token, os.environ.get("GRPC_TLS_CA_FILE", ""))
        print(f"IngestAck: accepted={ack.accepted_count} duplicates={ack.duplicate_count} "
              f"rejected={ack.rejected_count} reasons={list(ack.rejected_reasons)[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
