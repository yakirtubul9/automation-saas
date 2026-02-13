from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from django.contrib.auth.models import User
from django.http import HttpRequest

from .models import Business, BusinessMembership, Provider


@dataclass(frozen=True)
class CurrentContext:
    business: Business
    role: str
    provider: Optional[Provider]


def get_current_context(user: User) -> Optional[CurrentContext]:
    """Best-effort resolution of current business + role.

    Priority:
    1) BusinessMembership (supports Staff/Owner/Provider).
    2) Fallback to Business.owner for legacy flows.

    Provider resolution is best-effort (we do not have a strict FK to auth.User yet).
    """
    membership = (
        BusinessMembership.objects.select_related("business")
        .filter(user=user)
        .order_by("id")
        .first()
    )
    if membership:
        provider = None
        if membership.role == BusinessMembership.Role.PROVIDER:
            # Best-effort mapping: match by membership.whatsapp_number if present.
            q = Provider.objects.filter(business=membership.business, is_active=True)
            if membership.whatsapp_number:
                provider = q.filter(whatsapp_number=membership.whatsapp_number).first()
            if provider is None:
                # If there's exactly one provider in the business, assume it's the logged-in provider.
                providers = list(q[:2])
                if len(providers) == 1:
                    provider = providers[0]
        return CurrentContext(business=membership.business, role=membership.role, provider=provider)

    # Legacy fallback: owner business
    business = Business.objects.filter(owner=user).order_by("id").first()
    if business:
        return CurrentContext(business=business, role=BusinessMembership.Role.OWNER, provider=None)
    return None


def require_business_context(request: HttpRequest) -> CurrentContext:
    ctx = get_current_context(request.user)
    if not ctx:
        raise PermissionError("No business context for user")
    return ctx
