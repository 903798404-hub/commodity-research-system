"""Stable projection of consumer evidence, without execution-time freshness."""
from dataclasses import asdict
from datetime import date, datetime


def json_values(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_values(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_values(child) for child in value]
    return value


def public_identity(identity):
    return json_values(asdict(identity))


def summary_view(summary):
    value = summary.to_dict()
    # This field is recalculated against wall-clock today by upstream rules.
    # Notification freshness is exclusively the sealed Async run evidence.
    value.pop("freshness_status")
    return json_values(value)


def async_view(report):
    fields = ("identity", "coverage_status", "update_status", "freshness_status",
              "freshness_reason", "reason", "blocking", "latest_date",
              "source_latest_date", "previous_latest_date", "next_latest_date",
              "next_valid_through", "previous_valid_through", "threshold")
    rows = [{key: row[key] for key in fields if key in row}
            for row in sorted(report["series"], key=lambda row: row["identity"])]
    return {"dataset_status": report["dataset_status"], "summary": report["summary"],
            "required_identities": [row["identity"] for row in rows],
            "series": rows,
            "nonblocking_missing": [row for row in rows if row["coverage_status"] == "MISSING"],
            "promotion_allowed": report["promotion_allowed"]}


def status_line(label, status):
    counts = status["summary"]
    coverage = counts["coverage"]
    freshness = ", ".join(f"{key}={value}" for key, value in sorted(counts["freshness"].items()))
    return (f"{label}: {status['dataset_status']}；required={counts['TOTAL_REQUIRED']} / "
            f"PRESENT={coverage['PRESENT']} / MISSING={coverage['MISSING']} / ERROR={coverage['ERROR']}；"
            f"freshness: {freshness}")
