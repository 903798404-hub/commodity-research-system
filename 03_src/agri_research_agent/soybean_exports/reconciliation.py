"""Pure standard-library contract for evidence-backed monthly FAS checks.

This module validates data shape, coverage, time and baseline bindings only.
The protected host must additionally verify the immutable validation/NO_CHANGE
publication receipt chain before supplying a check to an unprivileged collector.
No check is persisted by a provider or by this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

SCHEMA = "fas-reconciliation-check/1"
FIELDS = {"schema_version", "policy_id", "policy_sha256", "delta_id",
          "baseline_sha256", "manifest_sha256", "checked_at_utc",
          "reconciliation_at_utc", "report_market_years", "retained_report_years",
          "validation_report_sha256", "publication_receipt_sha256"}
SHA = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[a-z0-9][a-z0-9-]{2,100}\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp(value):
    require(type(value) is str, "reconciliation timestamp must be text")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid reconciliation timestamp") from None
    require(result.tzinfo is not None, "reconciliation timestamp needs a timezone")
    return result.astimezone(timezone.utc)


def years(values):
    require(type(values) is list and 0 < len(values) <= 128
            and all(type(value) is int and 2000 <= value <= 9999 for value in values)
            and values == sorted(set(values)), "invalid reconciliation year coverage")
    return set(values)


def require_complete_coverage(fetched, retained):
    selected, previous = years(fetched), (set() if retained == [] else years(retained))
    require(previous <= selected, "monthly reconciliation omits retained years")
    latest = max(selected)
    require(set(range(latest - 6, latest + 1)) <= selected,
            "monthly reconciliation needs seven consecutive report years")


def validate(value, *, now):
    require(type(value) is dict and set(value) == FIELDS and value["schema_version"] == SCHEMA,
            "invalid reconciliation check schema")
    require(type(now) is datetime and now.tzinfo is not None, "reconciliation clock needs a timezone")
    for key in ("policy_id", "delta_id"):
        require(type(value[key]) is str and ID.fullmatch(value[key]), "invalid reconciliation policy/delta")
    for key in ("policy_sha256", "baseline_sha256", "manifest_sha256",
                "validation_report_sha256", "publication_receipt_sha256"):
        require(type(value[key]) is str and SHA.fullmatch(value[key]), "invalid reconciliation evidence SHA")
    checked, reconciled = timestamp(value["checked_at_utc"]), timestamp(value["reconciliation_at_utc"])
    require(reconciled <= checked <= now.astimezone(timezone.utc), "future or reversed reconciliation check")
    years(value["retained_report_years"])
    require_complete_coverage(value["report_market_years"], value["retained_report_years"])
    return value


def select(prior, check, *, baseline_sha256, manifest_sha256, retained_years, now):
    """Use a host-attested check only for this exact unchanged baseline."""
    previous = timestamp(prior)
    require(previous <= now.astimezone(timezone.utc), "future prior reconciliation watermark")
    if check is None:
        return prior
    validate(check, now=now)
    require(check["baseline_sha256"] == baseline_sha256 and check["manifest_sha256"] == manifest_sha256,
            "reconciliation check baseline identity differs")
    require(years(sorted(set(retained_years))) == set(check["retained_report_years"]),
            "reconciliation retained year identity differs")
    return check["reconciliation_at_utc"] if timestamp(check["reconciliation_at_utc"]) > previous else prior
