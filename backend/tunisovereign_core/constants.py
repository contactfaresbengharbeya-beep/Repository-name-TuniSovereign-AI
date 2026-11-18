"""Canonical identifiers shared by models, API, simulator and gRPC (pure Python, no Django imports)."""

# The 24 governorates of Tunisia, stored as ASCII codes with underscores. This list is the only
# accepted spelling: it prevents scope-check mismatches such as "Sidi Bouzid" vs "Sidi_Bouzid".
GOVERNORATES = [
    "Tunis", "Ariana", "Ben_Arous", "Manouba", "Nabeul", "Zaghouan", "Bizerte", "Beja",
    "Jendouba", "Kef", "Siliana", "Sousse", "Monastir", "Mahdia", "Sfax", "Kairouan",
    "Kasserine", "Sidi_Bouzid", "Gabes", "Medenine", "Tataouine", "Gafsa", "Tozeur", "Kebili",
]
GOVERNORATE_CHOICES = [(g, g.replace("_", " ")) for g in GOVERNORATES]

ROLE_COOP_MEMBER = "COOP_MEMBER"
ROLE_REGIONAL_AUDITOR = "REGIONAL_AUDITOR"
ROLE_NATIONAL_ADMIN = "NATIONAL_ADMIN"
ROLE_AGENT_SERVICE = "AGENT_SERVICE"
ALL_ROLES = (ROLE_COOP_MEMBER, ROLE_REGIONAL_AUDITOR, ROLE_NATIONAL_ADMIN, ROLE_AGENT_SERVICE)

TRACK_DOMAIN = "Track_05_Tunihara_MedGuard"

# Telemetry metric identifiers (TelemetryReading.MetricType values / proto `metric_type` strings).
METRIC_STOCK_LEVEL = "STOCK_LEVEL"
METRIC_DISPENSED_UNITS = "DISPENSED_UNITS"
METRIC_CONSUMPTION_RATE = "CONSUMPTION_RATE"
METRIC_RX_COUNT = "RX_COUNT"
METRIC_COLD_CHAIN_TEMP = "COLD_CHAIN_TEMP"
ALL_METRICS = (
    METRIC_STOCK_LEVEL,
    METRIC_DISPENSED_UNITS,
    METRIC_CONSUMPTION_RATE,
    METRIC_RX_COUNT,
    METRIC_COLD_CHAIN_TEMP,
)

# Plausibility ranges (inclusive) used to validate telemetry on every ingestion path.
METRIC_RANGES = {
    METRIC_STOCK_LEVEL: (0.0, 10_000_000.0),
    METRIC_DISPENSED_UNITS: (0.0, 1_000_000.0),
    METRIC_CONSUMPTION_RATE: (0.0, 1_000_000.0),
    METRIC_RX_COUNT: (0.0, 100_000.0),
    METRIC_COLD_CHAIN_TEMP: (-30.0, 60.0),
}
