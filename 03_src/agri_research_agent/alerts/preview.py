"""Candidate application wiring; domain registration does not modify Core."""
import argparse
import json
from pathlib import Path

from . import basis, weather
from .core import canonical_json
from .delivery import deliver_candidate
from .gate import contained, load_ready_run
from .senders import FakeSender


DOMAINS = {"weather": weather.build, "basis": basis.build}
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def preview(*, domain, run_dir, source_run_id, mode, fixture_package_root=None,
            weather_config=None, fake_outcome="SENT"):
    run = load_ready_run(run_dir, source_run_id, mode=mode,
                         fixture_package_root=fixture_package_root)
    message = DOMAINS[domain](run, weather_config=weather_config or PROJECT_ROOT / "02_configs/soybean_weather_us.yaml")
    directory = contained(PROJECT_ROOT, f"06_outputs/push_logs/{mode}/{source_run_id}")
    delivery = deliver_candidate(message, directory, FakeSender(fake_outcome))
    payload_path = contained(directory, message.message_id + ".payload.json")
    if payload_path.exists() and payload_path.read_bytes() != message.canonical_payload:
        raise ValueError("EXISTING_PAYLOAD_CONFLICT")
    if not payload_path.exists():
        with payload_path.open("xb") as handle:
            handle.write(message.canonical_payload)
    result = {"project_id": "notification-push", "message_type": message.payload["message_type"],
              "source_run_id": source_run_id, "DATA_READY": "PASS", "data_root_mode": run.mode,
              "message_id": message.message_id, "content_sha256": message.content_sha256,
              "payload": message.payload, "canonical_payload_path": str(payload_path),
              "delivery": delivery}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only Notification preview; FakeSender only")
    parser.add_argument("domain", choices=sorted(DOMAINS))
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("formal", "fixture"), required=True)
    parser.add_argument("--fixture-package-root", type=Path)
    parser.add_argument("--weather-config", type=Path)
    parser.add_argument("--fake-outcome", choices=("SENT", "FAILED", "UNKNOWN"), default="SENT")
    args = parser.parse_args(argv)
    try:
        result = preview(**vars(args))
    except Exception as exc:
        # Do not echo file contents, provider errors, environment, or credentials.
        print(json.dumps({"project_id": "notification-push", "PREVIEW": "FAIL",
                          "error_type": type(exc).__name__, "REAL_MESSAGE_SENT": False}))
        return 1
    print(canonical_json(result).decode("utf-8"))
    return 0
