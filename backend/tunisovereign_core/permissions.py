"""
Healthcare role matrix for DRF.

Each ViewSet declares ``role_matrix = {"read": (...), "create": (...), "update": (...), "delete": (...)}``.
An operation that is not listed is denied for everybody (deny by default). Row-level isolation is applied
separately through ``scope.py`` (querysets) and the serializers' ``validate`` methods (write targets).
"""
from rest_framework.permissions import BasePermission

from .constants import (
    ALL_ROLES,
    ROLE_AGENT_SERVICE,
    ROLE_COOP_MEMBER,
    ROLE_NATIONAL_ADMIN,
    ROLE_REGIONAL_AUDITOR,
)

ACTION_TO_OP = {
    "list": "read",
    "retrieve": "read",
    "create": "create",
    "update": "update",
    "partial_update": "update",
    "destroy": "delete",
}
METHOD_TO_OP = {
    "GET": "read", "HEAD": "read", "OPTIONS": "read",
    "POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete",
}

READ_ALL = ALL_ROLES
ADMIN_ONLY = (ROLE_NATIONAL_ADMIN,)


class RoleMatrixPermission(BasePermission):
    message = "Your role is not allowed to perform this operation."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_active):
            return False
        # Methods the view does not serve at all are answered with 405 by DRF's dispatch (no data is touched).
        if request.method.lower() not in getattr(view, "http_method_names", ()) or (
            getattr(view, "action", "") is None and request.method not in ("GET", "HEAD", "OPTIONS")
        ):
            return True
        op = ACTION_TO_OP.get(getattr(view, "action", None)) or METHOD_TO_OP.get(request.method)
        allowed = getattr(view, "role_matrix", {}).get(op, ())
        return user.role in allowed


class IsNationalAdmin(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_active and user.role == ROLE_NATIONAL_ADMIN)


__all__ = [
    "RoleMatrixPermission",
    "IsNationalAdmin",
    "READ_ALL",
    "ADMIN_ONLY",
    "ROLE_AGENT_SERVICE",
    "ROLE_COOP_MEMBER",
    "ROLE_NATIONAL_ADMIN",
    "ROLE_REGIONAL_AUDITOR",
]
