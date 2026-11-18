"""
DRF serializers. Rules enforced here:
  * explicit field lists only (never ``__all__``);
  * server-controlled fields (tx_hash, signature_verified, ingestion_protocol, status on create,
    current_stock_units, proposed_by/approved_by, ...) are read-only for clients;
  * write targets are validated against the caller's facility/governorate scope.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .constants import GOVERNORATE_CHOICES, ROLE_REGIONAL_AUDITOR
from .models import (
    AgentLoopExecution,
    AnomalyAndForecastEvent,
    HealthcareFacility,
    Medication,
    MedicationInventory,
    OnChainZKReceipt,
    Prescription,
    RedistributionRequest,
    TelemetryReading,
)
from .scope import can_access_facility, scoped
from .telemetry_ingest import TelemetryValidationError, validate_metric_value, validate_timestamp

User = get_user_model()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
class SovereignTokenObtainPairSerializer(TokenObtainPairSerializer):
    """SimpleJWT pair with healthcare claims: role, governorate, facility_id, wallet_address, dialect."""

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["governorate"] = user.governorate
        token["facility_id"] = str(user.facility_id) if user.facility_id else ""
        token["wallet_address"] = user.wallet_address or ""
        token["preferred_dialect"] = user.preferred_dialect
        return token


class RegisterSerializer(serializers.Serializer):
    """Public self-registration. ALWAYS creates a COOP_MEMBER without a facility (no data access)."""

    username = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    email = serializers.EmailField(required=False, allow_blank=True)
    governorate = serializers.ChoiceField(choices=GOVERNORATE_CHOICES)
    preferred_dialect = serializers.CharField(max_length=16, required=False, default="ar-TN")

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("A user with that username already exists.")
        return value

    def validate(self, attrs):
        candidate = User(username=attrs["username"], email=attrs.get("email", ""))
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)})
        return attrs

    def create(self, validated_data):
        # `role` and `facility` can never come from the client: any such keys were never declared above.
        return User.objects.create_user(
            username=validated_data["username"],
            password=validated_data["password"],
            email=validated_data.get("email", ""),
            governorate=validated_data["governorate"],
            preferred_dialect=validated_data.get("preferred_dialect", "ar-TN"),
            role=User.Role.COOP_MEMBER,
            facility=None,
        )


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "role", "governorate", "facility", "preferred_dialect", "wallet_address"]
        read_only_fields = fields


class AdminUserSerializer(serializers.ModelSerializer):
    """NATIONAL_ADMIN-only user management: the only place where role/facility can be assigned."""

    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "password", "role", "governorate", "facility",
            "is_active", "preferred_dialect", "wallet_address",
        ]
        read_only_fields = ["id"]

    def validate(self, attrs):
        password = attrs.get("password")
        if password:
            try:
                validate_password(password, user=User(username=attrs.get("username", "")))
            except DjangoValidationError as exc:
                raise serializers.ValidationError({"password": list(exc.messages)})
        elif self.instance is None:
            raise serializers.ValidationError({"password": "This field is required."})
        facility = attrs.get("facility")
        governorate = attrs.get("governorate", getattr(self.instance, "governorate", None))
        if facility is not None and governorate and facility.governorate != governorate:
            raise serializers.ValidationError({"facility": "Facility is not in the user's governorate."})
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        return User.objects.create_user(password=password, **validated_data)

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        instance = super().update(instance, validated_data)
        if password:
            instance.set_password(password)
            instance.save(update_fields=["password"])
        return instance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class ScopedPKField(serializers.PrimaryKeyRelatedField):
    """PK field whose queryset is limited to the caller's scope (out-of-scope ids look non-existent)."""

    def __init__(self, scope_path="facility", **kwargs):
        self.scope_path = scope_path
        super().__init__(**kwargs)

    def get_queryset(self):
        qs = super().get_queryset()
        request = self.context.get("request")
        return scoped(qs, request.user, self.scope_path) if request else qs.none()


def _user(serializer):
    return serializer.context["request"].user


# ---------------------------------------------------------------------------
# Domain serializers
# ---------------------------------------------------------------------------
class HealthcareFacilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = HealthcareFacility
        fields = [
            "id", "code", "name", "facility_type", "governorate", "latitude", "longitude",
            "daily_dispensing_capacity_units", "track_domain", "is_simulated", "created_at",
        ]
        read_only_fields = ["id", "track_domain", "created_at"]


class MedicationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Medication
        fields = [
            "id", "dci_name", "atc_code", "therapeutic_class", "dosage_form", "strength",
            "unit_of_measure", "is_chronic_care", "cnam_subsidy_status", "requires_cold_chain", "is_simulated",
        ]
        read_only_fields = ["id"]


class MedicationInventorySerializer(serializers.ModelSerializer):
    IMMUTABLE = ("facility", "medication", "node_code")

    class Meta:
        model = MedicationInventory
        fields = [
            "id", "facility", "medication", "node_code", "current_stock_units", "reorder_threshold_units",
            "safety_stock_days", "last_reading_at", "is_redistribution_hold", "is_simulated",
        ]
        # Stock level and last reading are derived from validated telemetry, never client-writable.
        read_only_fields = ["id", "current_stock_units", "last_reading_at"]

    def validate(self, attrs):
        if self.instance is not None:
            for name in self.IMMUTABLE:
                if name in attrs and attrs[name] != getattr(self.instance, name):
                    raise serializers.ValidationError({name: "This field cannot be changed."})
        return attrs


class TelemetryReadingSerializer(serializers.ModelSerializer):
    inventory = ScopedPKField(scope_path="facility", queryset=MedicationInventory.objects.all())

    class Meta:
        model = TelemetryReading
        fields = [
            "id", "inventory", "timestamp", "metric_type", "value",
            "ingestion_protocol", "is_simulated", "signature_verified",
        ]
        read_only_fields = ["id", "ingestion_protocol", "signature_verified"]

    def validate(self, attrs):
        try:
            attrs["value"] = validate_metric_value(attrs["metric_type"], attrs["value"])
            validate_timestamp(attrs["timestamp"])
        except TelemetryValidationError as exc:
            raise serializers.ValidationError({"detail": exc.code})
        if TelemetryReading.objects.filter(
            inventory=attrs["inventory"], metric_type=attrs["metric_type"], timestamp=attrs["timestamp"]
        ).exists():
            raise serializers.ValidationError({"detail": "DUPLICATE_READING"})
        return attrs


class PrescriptionSerializer(serializers.ModelSerializer):
    facility = ScopedPKField(scope_path="", queryset=HealthcareFacility.objects.all())

    class Meta:
        model = Prescription
        fields = [
            "id", "facility", "medication", "issued_at", "patient_pseudonym", "quantity_units",
            "cnam_coverage_category", "eligibility_valid_until", "is_simulated",
        ]
        read_only_fields = ["id"]


class AnomalyAndForecastEventSerializer(serializers.ModelSerializer):
    inventory = ScopedPKField(scope_path="facility", queryset=MedicationInventory.objects.all())

    class Meta:
        model = AnomalyAndForecastEvent
        fields = [
            "id", "inventory", "detected_at", "anomaly_type", "severity", "isolation_forest_score",
            "forecasted_stockout_days", "related_prescription", "details", "is_simulated",
        ]
        read_only_fields = ["id", "detected_at"]


class RedistributionRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = RedistributionRequest
        fields = [
            "id", "source_facility", "destination_facility", "medication", "units", "status",
            "proposed_by", "approved_by", "rationale", "related_event", "is_simulated", "created_at", "updated_at",
        ]
        # status is always PROPOSED at creation; transitions use RedistributionStatusSerializer.
        read_only_fields = ["id", "status", "proposed_by", "approved_by", "created_at", "updated_at"]

    def validate(self, attrs):
        src, dst = attrs["source_facility"], attrs["destination_facility"]
        if src == dst:
            raise serializers.ValidationError("Source and destination facilities must differ.")
        user = _user(self)
        if not (can_access_facility(user, src) or can_access_facility(user, dst)):
            raise PermissionDenied("You may only request redistributions involving your own scope.")
        return attrs


class RedistributionStatusSerializer(serializers.ModelSerializer):
    ALLOWED = {"PROPOSED": {"APPROVED", "REJECTED"}, "APPROVED": {"EXECUTED"}}

    class Meta:
        model = RedistributionRequest
        fields = ["id", "status", "approved_by"]
        read_only_fields = ["id", "approved_by"]

    def validate_status(self, new_status):
        current = self.instance.status
        if new_status not in self.ALLOWED.get(current, set()):
            raise serializers.ValidationError(f"Transition {current} -> {new_status} is not allowed.")
        user = _user(self)
        if new_status == "EXECUTED" and user.role != "NATIONAL_ADMIN":
            raise PermissionDenied("Only NATIONAL_ADMIN can mark a redistribution as executed.")
        if user.role == ROLE_REGIONAL_AUDITOR and not (
            can_access_facility(user, self.instance.source_facility)
            and can_access_facility(user, self.instance.destination_facility)
        ):
            raise PermissionDenied("Cross-governorate requests must be decided by NATIONAL_ADMIN.")
        return new_status

    def update(self, instance, validated_data):
        instance.status = validated_data["status"]
        if instance.status in ("APPROVED", "REJECTED"):
            instance.approved_by = _user(self)
        instance.save(update_fields=["status", "approved_by", "updated_at"])
        return instance


class OnChainZKReceiptSerializer(serializers.ModelSerializer):
    class Meta:
        model = OnChainZKReceipt
        fields = [
            "id", "agent_execution", "zk_commitment_hash", "public_inputs_json", "tx_hash",
            "block_number", "network_name", "is_verified_on_chain",
        ]
        read_only_fields = fields


class AgentLoopExecutionSerializer(serializers.ModelSerializer):
    zk_receipt = OnChainZKReceiptSerializer(read_only=True)

    class Meta:
        model = AgentLoopExecution
        fields = [
            "id", "thread_id", "governorate", "trigger_source", "darja_transcript", "parsed_intent_json",
            "mcp_tools_invoked", "skills_executed", "final_recommendation", "confidence_score",
            "created_at", "zk_receipt",
        ]
        read_only_fields = fields
