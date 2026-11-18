from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import api_rest

router = DefaultRouter()
router.register("facilities", api_rest.HealthcareFacilityViewSet, basename="facility")
router.register("medications", api_rest.MedicationViewSet, basename="medication")
router.register("inventory", api_rest.MedicationInventoryViewSet, basename="inventory")
router.register("telemetry", api_rest.TelemetryReadingViewSet, basename="telemetry")
router.register("prescriptions", api_rest.PrescriptionViewSet, basename="prescription")
router.register("anomalies", api_rest.AnomalyAndForecastEventViewSet, basename="anomaly")
router.register("redistribution-requests", api_rest.RedistributionRequestViewSet, basename="redistribution")
router.register("agent-executions", api_rest.AgentLoopExecutionViewSet, basename="agent-execution")
router.register("zk-receipts", api_rest.OnChainZKReceiptViewSet, basename="zk-receipt")
router.register("users", api_rest.UserAdminViewSet, basename="user")

urlpatterns = [
    path("auth/register/", api_rest.RegisterView.as_view(), name="auth-register"),
    path("auth/login/", api_rest.SovereignTokenObtainPairView.as_view(), name="auth-login"),
    path("auth/refresh/", api_rest.ThrottledTokenRefreshView.as_view(), name="auth-refresh"),
    path("auth/me/", api_rest.MeView.as_view(), name="auth-me"),
    path("v1/health/", api_rest.health, name="health"),
    path("v1/", include(router.urls)),
]
