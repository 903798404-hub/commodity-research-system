"""Pure asynchronous dataset contract; no I/O, calendar guesses or producer rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Mapping


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    policy_version: str
    freshness_threshold: int | None = None
    stale_is_blocking: bool = True
    threshold_approved: bool = False
    age_basis: str = "calendar_days"

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version.strip() or type(self.threshold_approved) is not bool:
            raise ValueError("Freshness policy identity/approval is invalid")
        if self.age_basis != "calendar_days":
            raise ValueError("Unsupported freshness calendar; explicit implementation required")
        if self.freshness_threshold is not None and (type(self.freshness_threshold) is not int or self.freshness_threshold < 0):
            raise ValueError("Freshness threshold must be a non-negative integer or null")
        if type(self.stale_is_blocking) is not bool:
            raise ValueError("Freshness stale_is_blocking must be boolean")
        if self.threshold_approved and self.freshness_threshold is None:
            raise ValueError("Approved freshness threshold is missing")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FreshnessPolicy:
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class SeriesUpdate:
    previous_latest_date: date | None
    source_latest_date: date | None
    next_latest_date: date | None
    new_row_count: int = 0
    revision_row_count: int = 0
    source_window_row_count: int = 0
    errors: tuple[str, ...] = ()


def validate_update_summary(report: Mapping[str, object], *, required: set[str]) -> None:
    """Reconcile all three aggregate dimensions against unique required series.

    Validate at consumer boundaries, not just where the report is constructed.
    Never repair a malformed report or infer missing statuses from its summary.
    """
    try:
        details, summary = report["series"], report["summary"]
        if not isinstance(details, list) or not isinstance(summary, Mapping):
            raise ValueError
        identities = [item["identity"] for item in details]
        if not required or len(identities) != len(required) or set(identities) != required:
            raise ValueError
        if type(summary["TOTAL_REQUIRED"]) is not int or summary["TOTAL_REQUIRED"] != len(required):
            raise ValueError
        for dimension, field, statuses in (
            ("coverage", "coverage_status", ("PRESENT", "MISSING", "ERROR")),
            ("updates", "update_status", ("UPDATED", "NO_CHANGE", "ERROR")),
            ("freshness", "freshness_status", ("FRESH", "STALE", "UNASSESSED")),
        ):
            actual = dict.fromkeys(statuses, 0)
            for item in details:
                actual[item[field]] += 1
            claimed = summary[dimension]
            if (not isinstance(claimed, Mapping) or set(claimed) != set(statuses)
                    or any(type(value) is not int or value < 0 for value in claimed.values())
                    or dict(claimed) != actual):
                raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ASYNC_SUMMARY_SERIES_MISMATCH") from exc


def evaluate_update(
    *, dataset_id: str, required: set[str], series: Mapping[str, SeriesUpdate],
    next_identities: set[str], as_of_date: date, policy: FreshnessPolicy,
) -> dict[str, object]:
    """Coverage is about complete next state, never the incremental window.

    Adapters must prove source/normalization/assembly lineage before calling.
    Coverage, update and freshness are independent dimensions. An absent or
    unapproved threshold means UNASSESSED, never FRESH and not a data failure.
    """
    if not required or type(as_of_date) is not date:
        raise ValueError("Async update requires identities and an exact as-of date")
    missing, unexpected = required - next_identities, next_identities - required
    counts = {
        "TOTAL_REQUIRED": len(required),
        "coverage": {"PRESENT": 0, "MISSING": 0, "ERROR": 0},
        "updates": {"UPDATED": 0, "NO_CHANGE": 0, "ERROR": 0},
        "freshness": {"FRESH": 0, "STALE": 0, "UNASSESSED": 0},
    }
    threshold = policy.freshness_threshold if policy.threshold_approved else None
    details = []
    for identity in sorted(required):
        evidence = series.get(identity, SeriesUpdate(None, None, None, errors=("SOURCE_EVIDENCE_MISSING",)))
        errors = list(evidence.errors)
        previous, source, following = evidence.previous_latest_date, evidence.source_latest_date, evidence.next_latest_date
        identity_missing = identity in missing or following is None
        if identity_missing:
            errors.append("REQUIRED_IDENTITY_MISSING")
        if source is None:
            errors.append("SOURCE_IDENTITY_UNVERIFIED")
        if any(type(n) is not int or n < 0 for n in (evidence.new_row_count, evidence.revision_row_count, evidence.source_window_row_count)):
            errors.append("INVALID_ROW_COUNTS")
        if following is not None and previous is not None and following < previous:
            errors.append("LATEST_DATE_REGRESSED")
        if source is not None and following is not None and following > source:
            errors.append("NEXT_DATE_NOT_SUPPORTED_BY_SOURCE")
        if any(value is not None and value > as_of_date for value in (previous, source, following)):
            errors.append("FUTURE_BUSINESS_DATE")
        advanced = following is not None and (previous is None or following > previous)
        if advanced != (evidence.new_row_count > 0):
            errors.append("UPDATE_DATE_COUNT_MISMATCH")
        age = None if following is None else (as_of_date - following).days
        coverage_status = "MISSING" if identity_missing else "ERROR" if errors else "PRESENT"
        if errors:
            update_status, reason = "ERROR", ";".join(sorted(set(errors)))
        elif advanced:
            update_status, reason = "UPDATED", "VALID_NEW_OBSERVATIONS_APPLIED"
        else:
            update_status = "NO_CHANGE"
            reason = (
                "HISTORICAL_REVISION_APPLIED_WITHOUT_DATE_ADVANCE" if evidence.revision_row_count else
                "SOURCE_PRESENT_OUTSIDE_WINDOW" if evidence.source_window_row_count == 0 else
                "NO_VALID_NEW_OBSERVATIONS"
            )
        if errors or age is None:
            freshness_status, freshness_reason = "UNASSESSED", "DATA_INTEGRITY_NOT_VERIFIED"
        elif threshold is None:
            freshness_status, freshness_reason = "UNASSESSED", "NO_APPROVED_FRESHNESS_THRESHOLD"
        elif age > threshold:
            freshness_status, freshness_reason = "STALE", "LATEST_BUSINESS_DATE_EXCEEDS_POLICY"
        else:
            freshness_status, freshness_reason = "FRESH", "LATEST_BUSINESS_DATE_WITHIN_POLICY"
        counts["coverage"][coverage_status] += 1
        counts["updates"][update_status] += 1
        counts["freshness"][freshness_status] += 1
        details.append({
            "identity": identity, **{
                key: value.isoformat() if isinstance(value, date) else value
                for key, value in asdict(evidence).items() if key != "errors"
            }, "coverage_status": coverage_status, "update_status": update_status,
            "freshness_status": freshness_status, "reason": reason, "freshness_reason": freshness_reason,
            "latest_date": None if following is None else following.isoformat(),
            "new_rows": evidence.new_row_count, "age_days": age, "age_business_days": None,
            "threshold": threshold,
        })
    blocking_reasons = []
    if missing or unexpected:
        blocking_reasons.append("IDENTITY_COVERAGE_FAILED")
    if counts["updates"]["ERROR"] or counts["coverage"]["ERROR"] or counts["coverage"]["MISSING"]:
        blocking_reasons.append("SERIES_ERROR")
    if counts["freshness"]["STALE"] and policy.stale_is_blocking:
        blocking_reasons.append("BLOCKING_STALE")
    changed = any(item.new_row_count or item.revision_row_count for item in series.values())
    dataset_status = (
        "FAILED" if blocking_reasons else "WARNING" if counts["freshness"]["STALE"] else
        "UPDATED" if changed else "NO_CHANGE"
    )
    return {
        "schema_version": "async-data-update/2", "dataset_id": dataset_id,
        "as_of_date": as_of_date.isoformat(), "policy": asdict(policy),
        "freshness_assessment_available": threshold is not None, "summary": counts, "series": details,
        "identity_coverage": {
            "status": "FAIL" if missing or unexpected else "PASS",
            "expected_count": len(required), "actual_required_count": len(required & next_identities),
            "missing": sorted(missing), "unexpected": sorted(unexpected),
        },
        "incremental_window_defines_completeness": False,
        "dataset_status": dataset_status, "blocking_reasons": blocking_reasons,
        "promotion_allowed": not blocking_reasons,
    }
