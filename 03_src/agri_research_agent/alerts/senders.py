"""Offline sender only. No credential, transport, or network dependencies."""
from dataclasses import dataclass
from typing import Any, Mapping

from .core import DeliveryResult


@dataclass(frozen=True)
class FakeSender:
    outcome: str = "SENT"
    channel: str = "fake"
    destination_identity: str = "fake:local-candidate"

    def __post_init__(self) -> None:
        DeliveryResult(self.outcome)
        if self.channel != "fake" or self.destination_identity != "fake:local-candidate":
            raise ValueError("ONLY_FAKE_DESTINATION_ALLOWED")

    def send(self, message_payload: Mapping[str, Any]) -> DeliveryResult:
        if not message_payload.get("message_id"):
            raise ValueError("MESSAGE_ID_REQUIRED")
        error = None if self.outcome == "SENT" else "FAKE_" + self.outcome
        return DeliveryResult(self.outcome, error)
