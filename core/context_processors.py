from __future__ import annotations

from .authz import get_current_context


def current_business(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    ctx = get_current_context(request.user)
    if not ctx:
        return {}
    return {
        "business": ctx.business,
        "current_role": ctx.role,
        "current_provider": ctx.provider,
    }
