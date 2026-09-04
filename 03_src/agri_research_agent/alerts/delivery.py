"""Local candidate ledger. A duplicate (including UNKNOWN) is never retried."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .core import Message, canonical_json
from .senders import FakeSender


def validate_evidence(evidence: dict) -> None:
    required = {"schema_version", "message_id", "message_type", "content_sha256",
                "source_run_id", "source_data_identity", "channel",
                "destination_identity", "attempted_at", "status", "safe_error"}
    if set(evidence) != required or evidence["schema_version"] != "notification-delivery-candidate/1":
        raise ValueError("INVALID_DELIVERY_EVIDENCE_SCHEMA")
    if evidence["status"] not in {"SENT", "FAILED", "UNKNOWN"}:
        raise ValueError("INVALID_DELIVERY_STATUS")
    for key in ("message_id", "content_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", evidence[key]):
            raise ValueError("INVALID_DELIVERY_IDENTITY")
    for key in ("message_type", "source_run_id"):
        if not isinstance(evidence[key], str) or not evidence[key]:
            raise ValueError("INVALID_DELIVERY_IDENTITY")
    if not isinstance(evidence["source_data_identity"], dict):
        raise ValueError("INVALID_SOURCE_IDENTITY")
    if evidence["channel"] != "fake" or evidence["destination_identity"] != "fake:local-candidate":
        raise ValueError("ONLY_FAKE_DELIVERY_ALLOWED")
    if evidence["safe_error"] not in {None, "FAKE_FAILED", "FAKE_UNKNOWN", "ATTEMPT_NOT_CONFIRMED"}:
        raise ValueError("UNSAFE_DELIVERY_ERROR")
    if datetime.fromisoformat(evidence["attempted_at"]).tzinfo is None:
        raise ValueError("DELIVERY_TIMESTAMP_REQUIRES_TIMEZONE")
    canonical_json(evidence)


def deliver_candidate(message: Message, directory: Path, sender: FakeSender) -> dict:
    """Reserve the identity before calling the fake sender; never resend.

    A crash between reservation and completion leaves UNKNOWN (or an unreadable
    reservation, which fails closed). This is evidence, not a network retry queue.
    The caller owns and validates this candidate-only output directory.
    """
    if type(sender) is not FakeSender:
        raise ValueError("CANDIDATE_REQUIRES_FAKE_SENDER")
    directory = Path(directory)
    if directory.is_symlink() or directory.is_junction():
        raise ValueError("DELIVERY_DIRECTORY_LINK_FORBIDDEN")
    directory.mkdir(parents=True, exist_ok=True)
    payload = message.payload
    path = directory / (message.message_id + ".json")
    evidence = {key: payload[key] for key in (
        "message_id", "message_type", "source_run_id", "source_data_identity")}
    evidence.update(schema_version="notification-delivery-candidate/1",
                    content_sha256=message.content_sha256,
                    channel=sender.channel, destination_identity=sender.destination_identity,
                    attempted_at=datetime.now(timezone.utc).isoformat(),
                    status="UNKNOWN", safe_error="ATTEMPT_NOT_CONFIRMED")
    validate_evidence(evidence)
    try:
        with path.open("xb") as handle:
            handle.write(canonical_json(evidence))
    except FileExistsError:
        if path.is_symlink():
            raise ValueError("DELIVERY_EVIDENCE_LINK_FORBIDDEN")
        previous = json.loads(path.read_text(encoding="utf-8"))
        validate_evidence(previous)
        for key in ("message_id", "message_type", "source_run_id", "source_data_identity", "content_sha256"):
            if previous[key] != evidence[key]:
                raise ValueError("DUPLICATE_MESSAGE_CONTENT_CONFLICT")
        return {"duplicate": True, "evidence_path": str(path), "evidence": previous}
    result = sender.send(payload)
    evidence.update(status=result.status, safe_error=result.safe_error)
    validate_evidence(evidence)
    path.write_bytes(canonical_json(evidence))
    return {"duplicate": False, "evidence_path": str(path), "evidence": evidence}
