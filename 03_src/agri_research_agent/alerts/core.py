"""Channel/domain-neutral, immutable notification candidate contracts."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping, Protocol


def canonical_json(value: Any) -> bytes:
    """Canonical JSON v1: UTF-8, sorted string keys, finite JSON numbers only.

    Domain adapters must explicitly convert dates and other non-JSON objects.
    Reject sets, coerced keys, and NaN rather than silently losing information.
    """
    def check(item: Any) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("CANONICAL_KEYS_MUST_BE_STRINGS")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise ValueError("CANONICAL_VALUE_MUST_BE_JSON")
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class Message:
    canonical_payload: bytes

    @property
    def payload(self) -> dict[str, Any]:
        # Return a copy; a sender must never mutate sealed message content.
        return json.loads(self.canonical_payload)

    @property
    def content_sha256(self) -> str:
        return sha256(self.canonical_payload).hexdigest()

    @property
    def message_id(self) -> str:
        return self.payload["message_id"]


def build_message(*, message_type: str, source_run_id: str,
                  source_data_identity: dict, business: dict,
                  status: dict, metadata: dict, rendered_content: str) -> Message:
    if not message_type.strip() or not source_run_id.strip():
        raise ValueError("MESSAGE_IDENTITY_REQUIRED")
    identity = {"message_type": message_type, "source_run_id": source_run_id}
    message_id = sha256(canonical_json(identity)).hexdigest()
    return Message(canonical_json({
        "schema_version": "notification-message/1",
        "canonical_version": "notification-json/1",
        **identity, "message_id": message_id,
        "source_data_identity": source_data_identity,
        "business": business, "status": status, "metadata": metadata,
        "rendered_content": rendered_content,
    }))


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    safe_error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"SENT", "FAILED", "UNKNOWN"}:
            raise ValueError("INVALID_DELIVERY_STATUS")


class Sender(Protocol):
    channel: str
    destination_identity: str

    def send(self, message_payload: Mapping[str, Any]) -> DeliveryResult: ...
