from abc import ABC, abstractmethod

class NotificationProvider(ABC):
    @abstractmethod
    def send(self, *, to: str, body: str) -> str:
        """Return provider message id / sid."""
        raise NotImplementedError
