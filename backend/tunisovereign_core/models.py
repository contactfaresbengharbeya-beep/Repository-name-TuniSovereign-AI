"""
Track 5 (Tunihara-MedGuard) domain models — 10 core models adapted from the starter template.

Starter model            -> Track 5 model
----------------------------------------------------------------
StakeholderUser          -> StakeholderUser            (+ facility scope)
CooperativeUnit          -> HealthcareFacility         (renamed)
ResourceNodePlot         -> MedicationInventory        (renamed: facility x medication stock position)
TelemetryReading         -> TelemetryReading           (new metrics, provenance, idempotency)
AnomalyAndForecastEvent  -> AnomalyAndForecastEvent    (medication semantics)
AgentLoopExecution       -> AgentLoopExecution         (+ governorate scope; populated in Week 3)
OnChainZKReceipt         -> OnChainZKReceipt           (is_verified_on_chain now defaults to False)
(new)                    -> Medication, Prescription, RedistributionRequest

Data policy: all data in this repository is SYNTHETIC. Every domain/telemetry row carries ``is_simulated``;
patients exist only as irreversible pseudonyms. No real CNAM, hospital or patient data is stored.
"""
import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models

from .constants import (
    GOVERNORATE_CHOICES,
    METRIC_COLD_CHAIN_TEMP,
    METRIC_CONSUMPTION_RATE,
    METRIC_DISPENSED_UNITS,
    METRIC_RX_COUNT,
    METRIC_STOCK_LEVEL,
    TRACK_DOMAIN,
)

PSEUDONYM_VALIDATOR = RegexValidator(
    r"^[0-9a-f]{64}$", "patient_pseudonym must be a 64-char lowercase hex digest (never a real identifier)."
)


class StakeholderUser(AbstractUser):
    class Role(models.TextChoices):
        COOP_MEMBER = "COOP_MEMBER", "Pharmacist / Facility Stock Operator"
        REGIONAL_AUDITOR = "REGIONAL_AUDITOR", "Regional Health Directorate / PCT Supervisor"
        NATIONAL_ADMIN = "NATIONAL_ADMIN", "Ministry of Health / CNAM-level National Administrator"
        AGENT_SERVICE = "AGENT_SERVICE", "Autonomous AI Agent, Simulator & MCP Service Account"

    role = models.CharField(max_length=24, choices=Role.choices, default=Role.COOP_MEMBER)
    governorate = models.CharField(max_length=64, choices=GOVERNORATE_CHOICES, db_index=True, default="Kasserine")
    # COOP_MEMBER data scope. Null until a NATIONAL_ADMIN assigns the user to a facility.
    facility = models.ForeignKey(
        "HealthcareFacility", null=True, blank=True, on_delete=models.SET_NULL, related_name="staff"
    )
    wallet_address = models.CharField(max_length=42, blank=True, null=True)  # used from Week 3
    preferred_dialect = models.CharField(max_length=16, default="ar-TN")


class HealthcareFacility(models.Model):
    class FacilityType(models.TextChoices):
        HOSPITAL = "HOSPITAL", "Hospital pharmacy"
        PCT_DEPOT = "PCT_DEPOT", "Regional central-pharmacy depot"
        PHARMACY = "PHARMACY", "Community pharmacy"
        CLINIC = "CLINIC", "Primary-care clinic"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=120)
    facility_type = models.CharField(max_length=16, choices=FacilityType.choices, default=FacilityType.PHARMACY)
    governorate = models.CharField(max_length=64, choices=GOVERNORATE_CHOICES, db_index=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    daily_dispensing_capacity_units = models.FloatField(
        null=True, blank=True, validators=[MinValueValidator(0)], help_text="Max daily dispensing capacity"
    )
    track_domain = models.CharField(max_length=64, default=TRACK_DOMAIN)
    contract_address = models.CharField(max_length=42, blank=True)  # Week 3
    is_simulated = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "healthcare facilities"
        ordering = ["governorate", "code"]

    def __str__(self):
        return f"{self.code} ({self.governorate})"


class Medication(models.Model):
    class SubsidyStatus(models.TextChoices):
        ILLUSTRATIVE_SUBSIDISED = "ILLUSTRATIVE_SUBSIDISED", "Illustrative: CNAM-subsidised"
        ILLUSTRATIVE_NOT_SUBSIDISED = "ILLUSTRATIVE_NOT_SUBSIDISED", "Illustrative: not subsidised"
        UNKNOWN = "UNKNOWN", "Unknown / unverified"

    dci_name = models.CharField(max_length=120, help_text="International non-proprietary name (DCI/INN)")
    atc_code = models.CharField(max_length=16, unique=True)
    therapeutic_class = models.CharField(max_length=64)
    dosage_form = models.CharField(max_length=48, blank=True)
    strength = models.CharField(max_length=48, blank=True)
    unit_of_measure = models.CharField(max_length=24, default="boxes")
    is_chronic_care = models.BooleanField(default=True)
    # Subsidy status is never claimed as verified CNAM data in Week 1.
    cnam_subsidy_status = models.CharField(
        max_length=32, choices=SubsidyStatus.choices, default=SubsidyStatus.UNKNOWN
    )
    requires_cold_chain = models.BooleanField(default=False)
    is_simulated = models.BooleanField(default=True)

    class Meta:
        ordering = ["dci_name"]

    def __str__(self):
        return f"{self.dci_name} {self.strength}".strip()


class MedicationInventory(models.Model):
    """One monitored stock position: a medication held by a facility. ``node_code`` is the telemetry key."""

    facility = models.ForeignKey(HealthcareFacility, on_delete=models.CASCADE, related_name="inventories")
    medication = models.ForeignKey(Medication, on_delete=models.CASCADE, related_name="inventories")
    node_code = models.CharField(max_length=48, unique=True)
    current_stock_units = models.FloatField(default=0.0, validators=[MinValueValidator(0)])
    reorder_threshold_units = models.FloatField(default=0.0, validators=[MinValueValidator(0)])
    safety_stock_days = models.PositiveIntegerField(default=7)
    last_reading_at = models.DateTimeField(null=True, blank=True)
    is_redistribution_hold = models.BooleanField(default=False)
    is_simulated = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "medication inventories"
        constraints = [
            models.UniqueConstraint(fields=["facility", "medication"], name="uniq_inventory_facility_medication")
        ]
        ordering = ["node_code"]

    def __str__(self):
        return self.node_code


class TelemetryReading(models.Model):
    class MetricType(models.TextChoices):
        STOCK_LEVEL = METRIC_STOCK_LEVEL, "Stock level (units)"
        DISPENSED_UNITS = METRIC_DISPENSED_UNITS, "Dispensed units in the interval"
        CONSUMPTION_RATE = METRIC_CONSUMPTION_RATE, "Trailing consumption rate (units/day)"
        RX_COUNT = METRIC_RX_COUNT, "Prescriptions dispensed in the interval"
        COLD_CHAIN_TEMP = METRIC_COLD_CHAIN_TEMP, "Cold-chain temperature (°C)"

    inventory = models.ForeignKey(MedicationInventory, on_delete=models.CASCADE, related_name="readings")
    timestamp = models.DateTimeField(db_index=True)
    metric_type = models.CharField(max_length=24, choices=MetricType.choices)
    value = models.FloatField()
    ingestion_protocol = models.CharField(max_length=16, default="gRPC")
    is_simulated = models.BooleanField(default=True)
    # True only when the ingestion path verified an HMAC-SHA256 signature over the packet.
    signature_verified = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["inventory", "metric_type", "timestamp"], name="uniq_reading_inventory_metric_ts"
            )
        ]
        indexes = [models.Index(fields=["inventory", "metric_type", "-timestamp"])]


class Prescription(models.Model):
    class CoverageCategory(models.TextChoices):
        CHRONIC_FULL = "CHRONIC_FULL", "Illustrative: chronic-disease full coverage"
        CHRONIC_PARTIAL = "CHRONIC_PARTIAL", "Illustrative: chronic-disease partial coverage"
        NOT_COVERED = "NOT_COVERED", "Not covered"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    facility = models.ForeignKey(HealthcareFacility, on_delete=models.CASCADE, related_name="prescriptions")
    medication = models.ForeignKey(Medication, on_delete=models.PROTECT, related_name="prescriptions")
    issued_at = models.DateTimeField()
    # Irreversible pseudonym (HMAC digest). Never a name, CIN or CNAM number.
    patient_pseudonym = models.CharField(max_length=64, validators=[PSEUDONYM_VALIDATOR], db_index=True)
    quantity_units = models.FloatField(validators=[MinValueValidator(0.0001)])
    # Minimum eligibility snapshot (no separate eligibility table in Week 1).
    cnam_coverage_category = models.CharField(
        max_length=24, choices=CoverageCategory.choices, default=CoverageCategory.CHRONIC_PARTIAL
    )
    eligibility_valid_until = models.DateField(null=True, blank=True)
    is_simulated = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=["patient_pseudonym", "medication", "issued_at"])]
        ordering = ["-issued_at"]


class AnomalyAndForecastEvent(models.Model):
    class Severity(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        CRITICAL = "CRITICAL", "Critical"

    class AnomalyType(models.TextChoices):
        ABNORMAL_CONSUMPTION = "ABNORMAL_CONSUMPTION", "Abnormal consumption"
        DUPLICATE_PRESCRIPTION = "DUPLICATE_PRESCRIPTION", "Duplicate prescription"
        STOCKOUT_RISK = "STOCKOUT_RISK", "Stock-out risk"
        COLD_CHAIN = "COLD_CHAIN", "Cold-chain excursion"
        MANUAL_AUDIT_REQUEST = "MANUAL_AUDIT_REQUEST", "Manual audit request"

    inventory = models.ForeignKey(MedicationInventory, on_delete=models.CASCADE, related_name="anomalies")
    detected_at = models.DateTimeField(auto_now_add=True)
    anomaly_type = models.CharField(max_length=32, choices=AnomalyType.choices)
    severity = models.CharField(max_length=16, choices=Severity.choices)
    # Scores/forecasts are produced by the Week 2 engine; null until then.
    isolation_forest_score = models.FloatField(null=True, blank=True)
    forecasted_stockout_days = models.FloatField(null=True, blank=True)
    related_prescription = models.ForeignKey(
        Prescription, null=True, blank=True, on_delete=models.SET_NULL, related_name="anomaly_events"
    )
    details = models.JSONField(default=dict, blank=True)
    is_simulated = models.BooleanField(default=True)

    class Meta:
        ordering = ["-detected_at"]


class RedistributionRequest(models.Model):
    class Status(models.TextChoices):
        PROPOSED = "PROPOSED", "Proposed"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        EXECUTED = "EXECUTED", "Executed"

    source_facility = models.ForeignKey(
        HealthcareFacility, on_delete=models.CASCADE, related_name="outgoing_redistributions"
    )
    destination_facility = models.ForeignKey(
        HealthcareFacility, on_delete=models.CASCADE, related_name="incoming_redistributions"
    )
    medication = models.ForeignKey(Medication, on_delete=models.PROTECT, related_name="redistribution_requests")
    units = models.FloatField(validators=[MinValueValidator(0.0001)])
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROPOSED)
    proposed_by = models.ForeignKey(
        StakeholderUser, null=True, on_delete=models.SET_NULL, related_name="proposed_redistributions"
    )
    approved_by = models.ForeignKey(
        StakeholderUser, null=True, blank=True, on_delete=models.SET_NULL, related_name="approved_redistributions"
    )
    rationale = models.JSONField(default=dict, blank=True)
    related_event = models.ForeignKey(
        AnomalyAndForecastEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name="redistributions"
    )
    is_simulated = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(source_facility=models.F("destination_facility")),
                name="redistribution_source_ne_destination",
            ),
            models.CheckConstraint(condition=models.Q(units__gt=0), name="redistribution_units_positive"),
        ]

    def clean(self):
        if self.source_facility_id and self.source_facility_id == self.destination_facility_id:
            raise ValidationError("Source and destination facilities must differ.")


class AgentLoopExecution(models.Model):
    """Week 3 will populate this table; the REST/GraphQL layer only reads it in Week 1."""

    thread_id = models.CharField(max_length=64, db_index=True)
    governorate = models.CharField(max_length=64, choices=GOVERNORATE_CHOICES, blank=True, db_index=True)
    trigger_source = models.CharField(max_length=64)
    darja_transcript = models.TextField(blank=True)
    parsed_intent_json = models.JSONField(default=dict)
    mcp_tools_invoked = models.JSONField(default=list)
    skills_executed = models.JSONField(default=list)
    final_recommendation = models.TextField()
    confidence_score = models.FloatField()
    created_at = models.DateTimeField(auto_now_add=True)


class OnChainZKReceipt(models.Model):
    agent_execution = models.OneToOneField(AgentLoopExecution, on_delete=models.CASCADE, related_name="zk_receipt")
    zk_commitment_hash = models.CharField(max_length=66, unique=True)
    public_inputs_json = models.JSONField(default=dict)
    tx_hash = models.CharField(max_length=66, unique=True)
    block_number = models.PositiveIntegerField()
    network_name = models.CharField(max_length=32, default="Foundry-Anvil-Local")
    # Defaults to False: a receipt is "verified" only once a real on-chain check has happened (Week 3).
    is_verified_on_chain = models.BooleanField(default=False)
