from django.conf import settings

from .mock import MockProvider

# אם עדיין אין לך קובץ כזה, תיצור אותו (אני מצרף לך בהמשך)
from .whatsapp_cloud import WhatsAppCloudProvider


def get_provider():
    p = (getattr(settings, "NOTIFICATION_PROVIDER", None) or "mock").strip().lower()

    if p in ("mock", "print"):
        return MockProvider()

    if p in ("whatsapp_cloud", "whatsapp", "meta_whatsapp"):
        return WhatsAppCloudProvider()

    raise ValueError(f"Unknown NOTIFICATION_PROVIDER={p}")
