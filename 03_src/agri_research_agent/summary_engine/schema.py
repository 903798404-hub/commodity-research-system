from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA_VERSION = "summary-v1"


def _identity_timestamp(value: Any) -> int | None:
    if isinstance(value, Mapping):
        candidates = [_identity_timestamp(item) for item in value.values()]
        return max((item for item in candidates if item is not None), default=None)
    if isinstance(value, (list, tuple)):
        candidates = [_identity_timestamp(item) for item in value]
        return max((item for item in candidates if item is not None), default=None)
    return value if isinstance(value, int) and value > 1_000_000_000_000_000 else None


@dataclass(frozen=True)
class Summary:
    schema_version: str
    module: str
    summary_id: str
    source_dataset: str
    source_identity: dict[str, Any]
    source_date: str | None
    comparison_identity: dict[str, Any] | None
    generated_at: str
    calculation_version: str
    rule_version: str
    freshness_status: str
    facts: dict[str, Any]
    classifications: tuple[str, ...]
    headline: str
    detail_text: str
    short_text: str
    missing_reason: str | None = None

    @classmethod
    def create(cls, *, module: str, source_dataset: str, source_identity: Mapping[str, Any],
        source_date: str | None, comparison_identity: Mapping[str, Any] | None,
        generated_at: datetime | None, calculation_version: str, rule_version: str,
        freshness_status: str, facts: Mapping[str, Any], classifications: list[str] | tuple[str, ...],
        headline: str, detail_text: str, short_text: str, missing_reason: str | None = None) -> "Summary":
        identity = dict(source_identity)
        payload = {"module": module, "source_dataset": source_dataset, "source_identity": identity,
            "calculation_version": calculation_version, "rule_version": rule_version}
        summary_id = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]
        identity_ns = _identity_timestamp(identity)
        if generated_at is not None:
            stamp = generated_at
        elif identity_ns is not None:
            stamp = datetime.fromtimestamp(identity_ns / 1_000_000_000, timezone.utc)
        elif source_date:
            normalized = f"{source_date}-01" if len(source_date) == 7 else source_date
            stamp = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        else:
            stamp = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return cls(SCHEMA_VERSION, module, summary_id, source_dataset, identity, source_date,
            dict(comparison_identity) if comparison_identity else None, stamp.astimezone(timezone.utc).isoformat(),
            calculation_version, rule_version, freshness_status, dict(facts), tuple(classifications),
            headline, detail_text, short_text, missing_reason)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self); payload["classifications"] = list(self.classifications); return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Summary":
        values = dict(payload); values["classifications"] = tuple(values.get("classifications", ())); return cls(**values)
