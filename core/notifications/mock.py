import uuid
from typing import Optional, Sequence
from .base import NotificationProvider

class MockProvider(NotificationProvider):
    def send(
        self,
        *,
        to: str,
        body: str,
        template_params: Optional[Sequence[str]] = None,
        template_name: Optional[str] = None,
    ) -> str:
        msg_id = f"mock-{uuid.uuid4().hex[:10]}"
        print(f"[MOCK SEND] to={to} id={msg_id}\n{body}\n")
        if template_params is not None:
            print(f"[MOCK TEMPLATE PARAMS] {list(template_params)}\n")
        if template_name is not None:
            print(f"[MOCK TEMPLATE NAME] {template_name}\n")
        return msg_id
