from django.conf import settings
from .mock import MockProvider

def get_provider():
    p = getattr(settings, "NOTIFICATION_PROVIDER", "mock").lower()
    if p == "mock":
        return MockProvider()
    raise ValueError(f"Unknown NOTIFICATION_PROVIDER={p}")
