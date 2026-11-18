"""
Data-isolation rules shared by REST, GraphQL and gRPC.

* NATIONAL_ADMIN / AGENT_SERVICE : every row.
* REGIONAL_AUDITOR               : rows whose facility is in the auditor's governorate.
* COOP_MEMBER                    : rows of the single facility the user is assigned to
                                   (nothing until a NATIONAL_ADMIN assigns one).
* anyone else / anonymous        : nothing.
"""
from django.db.models import Q

from .constants import (
    ROLE_AGENT_SERVICE,
    ROLE_COOP_MEMBER,
    ROLE_NATIONAL_ADMIN,
    ROLE_REGIONAL_AUDITOR,
)

GLOBAL_ROLES = (ROLE_NATIONAL_ADMIN, ROLE_AGENT_SERVICE)


def _active(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and user.is_active)


def facility_q(user, path: str = "facility") -> Q:
    """Q object restricting the facility reached through ``path`` ('' = the facility table itself)."""
    if not _active(user):
        return Q(pk__in=[])
    if user.role in GLOBAL_ROLES:
        return Q()
    if user.role == ROLE_REGIONAL_AUDITOR:
        return Q(**{f"{path}__governorate" if path else "governorate": user.governorate})
    if user.role == ROLE_COOP_MEMBER and user.facility_id:
        return Q(**{f"{path}_id" if path else "pk": user.facility_id})
    return Q(pk__in=[])


def scoped(queryset, user, path: str = "facility"):
    return queryset.filter(facility_q(user, path))


def scoped_redistributions(queryset, user):
    if not _active(user):
        return queryset.none()
    if user.role in GLOBAL_ROLES:
        return queryset
    return queryset.filter(facility_q(user, "source_facility") | facility_q(user, "destination_facility"))


def scoped_agent_executions(queryset, user):
    if not _active(user):
        return queryset.none()
    if user.role in GLOBAL_ROLES:
        return queryset
    if user.role == ROLE_REGIONAL_AUDITOR:
        return queryset.filter(governorate=user.governorate)
    return queryset.none()


def can_access_facility(user, facility) -> bool:
    """Object-level check for write validation (facility is an instance)."""
    if not _active(user):
        return False
    if user.role in GLOBAL_ROLES:
        return True
    if user.role == ROLE_REGIONAL_AUDITOR:
        return facility.governorate == user.governorate
    if user.role == ROLE_COOP_MEMBER:
        return bool(user.facility_id) and facility.pk == user.facility_id
    return False
