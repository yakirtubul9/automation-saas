from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from django.contrib.auth.models import User

from .models import Business, BusinessMembership


@dataclass(frozen=True)
class ActiveBusinessContext:
    business: Business
    role: str  # one of BusinessMembership.Role values


def get_active_business_context(user: User) -> ActiveBusinessContext:
    """Return an active business for the user + the highest role.

    Priority:
    1) Owned business (role=OWNER)
    2) Any membership business (highest role wins)
    3) Create a new business owned by the user (role=OWNER)
    """
    owned = Business.objects.filter(owner=user).order_by("id").first()
    if owned:
        return ActiveBusinessContext(business=owned, role=BusinessMembership.Role.OWNER)

    memberships = (
        BusinessMembership.objects.select_related("business")
        .filter(user=user)
        .order_by("id")
    )
    best = None
    precedence = {
        BusinessMembership.Role.OWNER: 3,
        BusinessMembership.Role.STAFF: 2,
        BusinessMembership.Role.PROVIDER: 1,
    }
    for m in memberships:
        if best is None or precedence.get(m.role, 0) > precedence.get(best.role, 0):
            best = m

    if best is not None:
        return ActiveBusinessContext(business=best.business, role=best.role)

    created = Business.objects.create(owner=user, name=f"{user.username} Business")
    return ActiveBusinessContext(business=created, role=BusinessMembership.Role.OWNER)


def can_view_client_details(role: str) -> bool:
    # Privacy-by-default: Staff/Owner should not see patient details by default.
    return role == BusinessMembership.Role.PROVIDER


def can_manage_calendar(role: str) -> bool:
    return role in {BusinessMembership.Role.OWNER, BusinessMembership.Role.STAFF, BusinessMembership.Role.PROVIDER}


def can_view_ops(role: str) -> bool:
    return role in {BusinessMembership.Role.OWNER, BusinessMembership.Role.STAFF}
