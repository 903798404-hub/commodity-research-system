"""Narrow host-tool interpreter evidence, not application/production acceptance."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import linecache
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import traceback
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = "04_scripts/runtime/pre_release_runtime.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(*args):
    return subprocess.check_output(["git", "-c", "safe.directory=" + str(ROOT), "-C", str(ROOT), *args])


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def exercise(args, receipt):
    expected = tuple(int(part) for part in args.expected_python.split("."))
    assert sys.version_info[:len(expected)] == expected, (sys.version, expected)
    fixtures = load(ROOT / "08_tests/shared/test_host_release_timestamps.py", "timestamp_fixtures")
    current = fixtures.load_runtime()
    base_raw = git("show", args.base + ":" + RUNTIME)
    baseline = ModuleType("timestamp_baseline_release")
    baseline.__file__ = str(ROOT / RUNTIME)
    # Traceback must show the baseline's actual lines, not today's candidate
    # file at the same path/line number. __file__ still supplies its source ROOT.
    baseline_label = "git:" + args.base + ":" + RUNTIME
    linecache.cache[baseline_label] = (len(base_raw), None, base_raw.decode("utf-8").splitlines(True), baseline_label)
    exec(compile(base_raw, baseline_label, "exec"), baseline.__dict__)
    grant_path = "03_src/agri_research_agent/shared/production_grant.py"
    assert git("show", args.base + ":" + grant_path) == git("show", "HEAD:" + grant_path)
    assert not git("diff", "HEAD", "--", grant_path), "grant parser must remain unchanged"
    receipt.update(candidate_commit=git("rev-parse", "HEAD").decode().strip(),
        candidate_tree=git("rev-parse", "HEAD^{tree}").decode().strip(), base_commit=args.base,
        baseline_runtime_sha256=digest(base_raw), runtime_sha256=digest((ROOT / RUNTIME).read_bytes()),
        interpreter=dict(executable=sys.executable, version=sys.version, platform=platform.platform()),
        run_id=os.environ.get("GITHUB_RUN_ID"), run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT"),
        production_acceptance="NOT_EXECUTED", production_authorized=False)
    with tempfile.TemporaryDirectory(prefix="host-timestamp-") as directory:
        recovery, host, paths = fixtures.recovery_fixture(Path(directory), current)
        baseline.TRUST = current.TRUST
        before = {str(path): digest(path.read_bytes()) for path in paths}
        try:
            baseline.verify_recovery_observation(recovery, host)
        except Exception as exc:
            receipt["original_baseline"] = dict(status="FAIL", exception_type=type(exc).__name__,
                message=str(exc), traceback=traceback.format_exc())
            assert sys.version_info[:2] == (3, 10)
            assert type(exc) is ValueError
            assert str(exc) == "Invalid isoformat string: '2026-09-29T10:10:52.479486984+00:00'"
            receipt["original_failure_reproduced"] = "PASS"
        else:
            assert sys.version_info[:2] == (3, 12)
            receipt["original_baseline"] = dict(status="PASS", note="3.12 accepts fractions but datetime truncates nanoseconds")
            receipt["original_failure_reproduced"] = "NOT_APPLICABLE_ON_3_12"
        current.verify_recovery_observation(recovery, host)
        assert before == {str(path): digest(path.read_bytes()) for path in paths}
        receipt["original_input_formal_recovery_validation"] = "PASS"
        receipt["original_input"] = dict(created_at=fixtures.ORIGINAL_CREATED, started_at=fixtures.ORIGINAL_STARTED)
        receipt["fixture_raw_hashes_preserved"] = before
    selectors = ["08_tests/shared/test_host_release_timestamps.py", "08_tests/test_release_refresh.py",
                 "08_tests/test_high_risk_execution.py::test_prepare_entry_seals_only_after_live_consumers_and_never_executes",
                 "08_tests/test_high_risk_execution.py::test_existing_plan_cli_calls_preparation_not_execution"]
    # Preserve the original application's fixture regression on its supported
    # version; host-only fixtures above cover the same formal verifier negatives
    # on 3.10 without importing the application's StrEnum-based identity module.
    if sys.version_info[:2] == (3, 12):
        selectors.append("08_tests/test_pre_release_runtime.py::test_recovery_requires_fresh_signed_grant_and_bound_real_probes")
    receipt["host_test_selectors"] = selectors
    result = subprocess.run([sys.executable, "-I", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", *selectors,
        "--junitxml=" + str(args.output.with_suffix(".junit.xml"))], cwd=ROOT, check=False)
    assert result.returncode == 0, "host compatibility regressions failed"
    receipt["formal_boundary_regressions"] = "PASS"
    if args.docker_evidence is not None:
        # Reuse unchanged real Docker observations from the existing Hosted E2E.
        # Remaining legacy probe envelopes are clearly labeled test fixtures:
        # this proves the timestamp/signature/identity verifier, not a complete
        # production-domain recovery rehearsal or release authorization.
        raw = args.docker_evidence.read_bytes()
        evidence = json.loads(raw)
        assert evidence["status"] == "PASS"
        observations = evidence["raw_evidence"]
        created, running = observations["recovery_created"], observations["recovery_running"]
        assert created["Id"] == running["Id"]
        work = Path(created["Config"]["Labels"]["com.docker.compose.project.working_dir"])
        trust = json.loads((work / "test-source/02_configs/production_runtime_trust.json").read_bytes())
        assert sys.platform == "linux" and os.geteuid() == 0
        with tempfile.TemporaryDirectory(prefix="docker-time-", dir="/root") as directory:
            recovery, host, paths = fixtures.recovery_fixture(Path(directory), current,
                created=created["Created"], started=running["State"]["StartedAt"],
                envelope=observations["signed_grant"], trust=trust,
                observed=datetime.now(timezone.utc).isoformat())
            host = load(ROOT / "09_deploy/runtime_identity/host_authorization.py", "timestamp_formal_host")
            instance = json.loads(paths[1].read_bytes())
            assert instance["container_id"] == running["Id"]
            assert instance["image_id"] == running["Image"]
            assert instance["hostname"] == running["Config"]["Hostname"]
            before = {str(path): digest(path.read_bytes()) for path in paths}
            current.verify_recovery_observation(recovery, host)
            assert before == {str(path): digest(path.read_bytes()) for path in paths}
        assert raw == args.docker_evidence.read_bytes()
        receipt["real_docker_formal_timestamp_validation"] = dict(status="PASS", evidence_sha256=digest(raw),
            evidence_class="HOSTED_REAL_DOCKER_TIMESTAMPS_WITH_TEST_TRUST_AND_LEGACY_PROBE_FIXTURES",
            created_at=created["Created"], started_at=running["State"]["StartedAt"],
            container_id=running["Id"], image_id=running["Image"],
            signed_envelope_sha256=digest(json.dumps(observations["signed_grant"], sort_keys=True).encode()),
            source_evidence_and_signature_unchanged=True, production_recovery="NOT_EXECUTED")
    else:
        receipt["real_docker_formal_timestamp_validation"] = "NOT_EXECUTED"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--expected-python", required=True)
    parser.add_argument("--docker-evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    receipt = dict(status="STARTED")
    try:
        exercise(args, receipt)
        receipt["status"] = "PASS"
    except Exception as exc:
        receipt.update(status="FAIL", exception_type=type(exc).__name__, message=str(exc), traceback=traceback.format_exc())
        raise
    finally:
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
