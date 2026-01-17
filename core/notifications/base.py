# core/notifications/base.py
from abc import ABC, abstractmethod
from typing import Optional, Sequence

class NotificationProvider(ABC):
    @abstractmethod
    def send(
        self,
        *,
        to: str,
        body: str,
        template_params: Optional[Sequence[str]] = None,
        template_name: Optional[str] = None,
    ) -> str:
        """Return provider message id / sid."""
        raise NotImplementedError
