import uuid
from .base import NotificationProvider

class MockProvider(NotificationProvider):
    def send(self, *, to: str, body: str) -> str:
        msg_id = f"mock-{uuid.uuid4().hex[:10]}"
        print(f"[MOCK SEND] to={to} id={msg_id}\n{body}\n")
        return msg_id
