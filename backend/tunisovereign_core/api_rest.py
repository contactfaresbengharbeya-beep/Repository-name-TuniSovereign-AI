"""
Django 6 REST API layer (DRF + SimpleJWT) — Track 5 Tunihara-MedGuard, Week 1.

Auth     POST /api/auth/register/  (public, always COOP_MEMBER, throttled)
         POST /api/auth/login/     (custom claims: role, governorate, facility_id, wallet_address)
         POST /api/auth/refresh/
         GET  /api/auth/me/
API v1   /api/v1/{facilities,medications,inventory,telemetry,prescriptions,anomalies,
                  redistribution-requests,agent-executions,zk-receipts,users}/   + /api/v1/health/

Week 1 deliberately does NOT import or call the LangGraph agent. The starter's unauthenticated
``darja_intent_webhook`` was removed from the routes: it will return in Week 3 behind AGENT_SERVICE auth.
"""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection, transaction
from django.utils.dateparse import parse_datetime
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .constants import (
    ROLE_AGENT_SERVICE,
    ROLE_COOP_MEMBER,
    ROLE_NATIONAL_ADMIN,
    ROLE_REGIONAL_AUDITOR,
)
from .models import (
    AgentLoopExecution,
    AnomalyAndForecastEvent,
    HealthcareFacility,
    Medication,
    MedicationInventory,
    OnChainZKReceipt,
    Prescription,
    RedistributionRequest,
    StakeholderUser,
    TelemetryReading,
)
from .permissions import ADMIN_ONLY, READ_ALL, RoleMatrixPermission
from .scope import scoped, scoped_agent_executions, scoped_redistributions
from .serializers import (
    AdminUserSerializer,
    AgentLoopExecutionSerializer,
    AnomalyAndForecastEventSerializer,
    HealthcareFacilitySerializer,
    MedicationInventorySerializer,
    MedicationSerializer,
    OnChainZKReceiptSerializer,
    PrescriptionSerializer,
    RedistributionRequestSerializer,
    RedistributionStatusSerializer,
    RegisterSerializer,
    SovereignTokenObtainPairSerializer,
    TelemetryReadingSerializer,
    UserSerializer,
)
from .telemetry_ingest import refresh_inventory_snapshot
from .constants import METRIC_STOCK_LEVEL


# ============================================================================
# 1. Authentication views
# ============================================================================
class SovereignTokenObtainPairView(TokenObtainPairView):
    serializer_class = SovereignTokenObtainPairSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class ThrottledTokenRefreshView(TokenRefreshView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class RegisterView(APIView):
    permission_classes = [permissions.AllowAny]  # public self-registration only; yields a data-less COOP_MEMBER
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "governorate": user.governorate,
                "detail": "Account created as COOP_MEMBER without facility access. "
                "A NATIONAL_ADMIN must assign your facility.",
            },
            status=status.HTTP_201_CREATED,
        )


class MeView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return Response({"status": "ok", "database": "ok"})


# ============================================================================
# 2. Base ViewSet: deny-by-default role matrix + row-level scope + safe filters
# ============================================================================
class ScopedModelViewSet(viewsets.ModelViewSet):
    permission_classes = [RoleMatrixPermission]
    scope_path = "facility"  # facility lookup path used by scope.facility_q ('' = the facility table)
    filter_map = {}  # query-param -> ORM lookup

    def get_queryset(self):
        qs = super().get_queryset()
        qs = scoped(qs, self.request.user, self.scope_path)
        return self.apply_filters(qs)

    def apply_filters(self, qs):
        params = self.request.query_params
        for param, lookup in self.filter_map.items():
            if param in params:
                value = params[param]
                if param in ("from", "to"):
                    value = parse_datetime(value)
                    if value is None:
                        raise serializers.ValidationError({param: "Invalid ISO-8601 datetime."})
                try:
                    qs = qs.filter(**{lookup: value})
                except (ValueError, TypeError, DjangoValidationError):  # malformed UUID / int values
                    raise serializers.ValidationError({param: "Invalid filter value."})
        return qs


# ============================================================================
# 3. Domain ViewSets (role matrix documented in docs/uml and the completion report)
# ============================================================================
class HealthcareFacilityViewSet(ScopedModelViewSet):
    queryset = HealthcareFacility.objects.all()
    serializer_class = HealthcareFacilitySerializer
    scope_path = ""
    role_matrix = {"read": READ_ALL, "create": ADMIN_ONLY, "update": ADMIN_ONLY, "delete": ADMIN_ONLY}
    filter_map = {"governorate": "governorate", "facility_type": "facility_type"}


class MedicationViewSet(viewsets.ModelViewSet):
    queryset = Medication.objects.all()
    serializer_class = MedicationSerializer
    permission_classes = [RoleMatrixPermission]
    role_matrix = {"read": READ_ALL, "create": ADMIN_ONLY, "update": ADMIN_ONLY, "delete": ADMIN_ONLY}


class MedicationInventoryViewSet(ScopedModelViewSet):
    queryset = MedicationInventory.objects.select_related("facility", "medication")
    serializer_class = MedicationInventorySerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    role_matrix = {
        "read": READ_ALL,
        "create": ADMIN_ONLY,
        "update": (ROLE_COOP_MEMBER, ROLE_NATIONAL_ADMIN, ROLE_AGENT_SERVICE),
        "delete": ADMIN_ONLY,
    }
    filter_map = {
        "facility": "facility_id",
        "medication": "medication_id",
        "governorate": "facility__governorate",
    }


class TelemetryReadingViewSet(ScopedModelViewSet):
    queryset = TelemetryReading.objects.select_related("inventory")
    serializer_class = TelemetryReadingSerializer
    scope_path = "inventory__facility"
    http_method_names = ["get", "post", "delete", "head", "options"]  # readings are immutable
    role_matrix = {
        "read": READ_ALL,
        "create": (ROLE_AGENT_SERVICE, ROLE_COOP_MEMBER, ROLE_NATIONAL_ADMIN),
        "delete": ADMIN_ONLY,
    }
    filter_map = {
        "inventory": "inventory_id",
        "metric_type": "metric_type",
        "from": "timestamp__gte",
        "to": "timestamp__lte",
    }

    def get_queryset(self):
        qs = super().get_queryset().order_by("-timestamp")
        return qs

    @transaction.atomic
    def perform_create(self, serializer):
        reading = serializer.save(ingestion_protocol="REST", signature_verified=False)
        if reading.metric_type == METRIC_STOCK_LEVEL:
            refresh_inventory_snapshot({reading.inventory_id: (reading.timestamp, reading.value)})


class PrescriptionViewSet(ScopedModelViewSet):
    queryset = Prescription.objects.select_related("facility", "medication")
    serializer_class = PrescriptionSerializer
    scope_path = "facility"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    role_matrix = {
        "read": READ_ALL,
        "create": (ROLE_COOP_MEMBER, ROLE_NATIONAL_ADMIN),
        "update": ADMIN_ONLY,
        "delete": ADMIN_ONLY,
    }
    filter_map = {"facility": "facility_id", "medication": "medication_id", "patient_pseudonym": "patient_pseudonym"}


class AnomalyAndForecastEventViewSet(ScopedModelViewSet):
    queryset = AnomalyAndForecastEvent.objects.select_related("inventory")
    serializer_class = AnomalyAndForecastEventSerializer
    scope_path = "inventory__facility"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    role_matrix = {
        "read": READ_ALL,
        "create": (ROLE_AGENT_SERVICE, ROLE_NATIONAL_ADMIN),
        "update": ADMIN_ONLY,
        "delete": ADMIN_ONLY,
    }
    filter_map = {
        "inventory": "inventory_id",
        "severity": "severity",
        "anomaly_type": "anomaly_type",
        "governorate": "inventory__facility__governorate",
    }


class RedistributionRequestViewSet(ScopedModelViewSet):
    queryset = RedistributionRequest.objects.select_related("source_facility", "destination_facility", "medication")
    serializer_class = RedistributionRequestSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    role_matrix = {
        "read": READ_ALL,
        "create": (ROLE_COOP_MEMBER, ROLE_REGIONAL_AUDITOR, ROLE_AGENT_SERVICE, ROLE_NATIONAL_ADMIN),
        "update": (ROLE_REGIONAL_AUDITOR, ROLE_NATIONAL_ADMIN),
        "delete": ADMIN_ONLY,
    }
    filter_map = {"status": "status", "medication": "medication_id"}

    def get_queryset(self):
        qs = scoped_redistributions(super(ScopedModelViewSet, self).get_queryset(), self.request.user)
        return self.apply_filters(qs)

    def get_serializer_class(self):
        if self.action in ("update", "partial_update"):
            return RedistributionStatusSerializer
        return RedistributionRequestSerializer

    def perform_create(self, serializer):
        serializer.save(proposed_by=self.request.user, status=RedistributionRequest.Status.PROPOSED)


class AgentLoopExecutionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AgentLoopExecution.objects.select_related("zk_receipt").order_by("-created_at")
    serializer_class = AgentLoopExecutionSerializer
    permission_classes = [RoleMatrixPermission]
    role_matrix = {"read": (ROLE_REGIONAL_AUDITOR, ROLE_NATIONAL_ADMIN, ROLE_AGENT_SERVICE)}

    def get_queryset(self):
        return scoped_agent_executions(super().get_queryset(), self.request.user)


class OnChainZKReceiptViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OnChainZKReceipt.objects.select_related("agent_execution").order_by("-block_number")
    serializer_class = OnChainZKReceiptSerializer
    permission_classes = [RoleMatrixPermission]
    role_matrix = {"read": (ROLE_REGIONAL_AUDITOR, ROLE_NATIONAL_ADMIN, ROLE_AGENT_SERVICE)}

    def get_queryset(self):
        allowed = scoped_agent_executions(AgentLoopExecution.objects.all(), self.request.user)
        return super().get_queryset().filter(agent_execution__in=allowed)


class UserAdminViewSet(viewsets.ModelViewSet):
    queryset = StakeholderUser.objects.select_related("facility").order_by("username")
    serializer_class = AdminUserSerializer
    permission_classes = [RoleMatrixPermission]
    http_method_names = ["get", "post", "patch", "head", "options"]  # deactivate via is_active, never delete
    role_matrix = {"read": ADMIN_ONLY, "create": ADMIN_ONLY, "update": ADMIN_ONLY}
