"""Host-only release Gate tests; synthetic signing keys, no business imports."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[2]
ORIGINAL_CREATED = "2026-09-29T10:10:52.479486984Z"
ORIGINAL_STARTED = "2026-09-29T10:11:33.933194404Z"


def load_runtime():
    spec = importlib.util.spec_from_file_location("host_timestamp_release", ROOT / "04_scripts/runtime/pre_release_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def recovery_fixture(root, runtime, *, created=ORIGINAL_CREATED, started=ORIGINAL_STARTED,
                     issued="2026-09-29T10:11:00+00:00", expires="2026-09-29T11:11:00+00:00",
                     observed="2026-09-29T10:13:21.949559+00:00", envelope=None, trust=None):
    """Generate bound test probes; optionally retain a Hosted test grant verbatim.

    Probe bodies are explicitly fixtures, not claims of production acceptance.
    The formal signature/parser/hash/time verifier itself is never mocked.
    """
    root.mkdir(parents=True, exist_ok=True)
    if envelope is None:
        key = Ed25519PrivateKey.generate()
        payload = dict(grant_id="a" * 32, issued_at=issued, expires_at=expires,
            identity_kind="oci_container", authorization_mode="production", artifact_origin="candidate", role="production",
            project_id="identity-fixture", module_id="shared-runtime", service_id="fixture-service", runtime_id="formal-runtime",
            approved_commit="a" * 40, approved_tree="b" * 40, release_commit="a" * 40, release_tree="b" * 40,
            image_id="sha256:" + "c" * 64, release_sha256="1" * 64, runtime_manifest_sha256="2" * 64,
            runtime_marker_sha256="3" * 64, runtime_root="/runtime", writable_roots=["/runtime/data"],
            protected_mounts=["/app", "/runtime/.market-data-runtime.json"], rendered_compose_sha256="4" * 64,
            mount_contract_sha256="5" * 64, actual_config_sha256="6" * 64, container_id="7" * 64, hostname_nonce="8" * 32)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        envelope = dict(schema_version="production-execution-grant/1", algorithm="ed25519", key_id="hosted-test-production",
                        payload=payload, signature=base64.b64encode(key.sign(canonical)).decode())
        trust = dict(schema_version="production-runtime-trust/1", keys=[dict(key_id=envelope["key_id"], domain="production",
            algorithm="ed25519", public_key_base64=base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode())],
            revoked_key_ids=[], revoked_grant_ids=[])
    payload = envelope["payload"]
    paths = []
    def ref(name, value):
        path = root / name
        raw = json.dumps(value, sort_keys=True).encode()
        path.write_bytes(raw)
        paths.append(path)
        return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest())
    trust_ref = ref("test-trust.json", trust)
    runtime.TRUST = trust_ref["path"]
    instance = dict(container_id=payload["container_id"], image_id=payload["image_id"], hostname=payload["hostname_nonce"],
                    created_at=created, started_at=started)
    evidence = dict(instance=ref("instance.json", instance), grant=ref("signed-test-grant.json", envelope))
    for name in ("preflight", "health", "consumer", "data_unchanged"):
        raw = ref(name + "-raw.json", {"fixture": "HOST_TIMESTAMP_CONTRACT_ONLY_NOT_PRODUCTION_REHEARSAL"})
        evidence[name] = ref(name + ".json", dict(container_id=payload["container_id"], commit=payload["approved_commit"],
            tree=payload["approved_tree"], image_id=payload["image_id"], exit_code=0, status="PASS", raw=raw))
    recovery = dict(base=dict(commit=payload["approved_commit"], tree=payload["approved_tree"]), old_image_id=payload["image_id"],
                    observed_at=observed, evidence=evidence)
    host = SimpleNamespace(_json=lambda raw: json.loads(raw), _protected_path=lambda path, **kw: Path(path))
    return recovery, host, paths


def test_original_observations_pass_formal_entry_and_preserve_all_bytes(tmp_path):
    runtime = load_runtime()
    recovery, host, paths = recovery_fixture(tmp_path, runtime)
    before = {str(path): path.read_bytes() for path in paths}
    runtime.verify_recovery_observation(recovery, host)
    assert {str(path): path.read_bytes() for path in paths} == before
    assert json.loads(paths[1].read_bytes())["created_at"] == ORIGINAL_CREATED


@pytest.mark.parametrize("fraction", ["", ".1", ".12", ".123", ".1234", ".12345", ".123456", ".1234567", ".12345678", ".123456789"])
@pytest.mark.parametrize("zone", ["Z", "+00:00", "+08:00", "-03:30"])
def test_legal_fraction_lengths_and_offsets_use_formal_entry(tmp_path, fraction, zone):
    runtime = load_runtime()
    # Equivalent UTC 10:10/10:12, including minute offsets.
    hour, minute = {"Z": (10, 0), "+00:00": (10, 0), "+08:00": (18, 0), "-03:30": (6, 30)}[zone]
    created = f"2026-09-29T{hour:02d}:{minute+10:02d}:00{fraction}{zone}"
    started = f"2026-09-29T{hour:02d}:{minute+12:02d}:00{fraction}{zone}"
    recovery, host, _ = recovery_fixture(tmp_path, runtime, created=created, started=started)
    runtime.verify_recovery_observation(recovery, host)
    equivalent = f"2026-09-29T10:10:00{fraction}Z"
    assert runtime._docker_timestamp_nanoseconds(created, "created_at") == runtime._docker_timestamp_nanoseconds(equivalent, "created_at")


@pytest.mark.parametrize("field", ["created", "started"])
@pytest.mark.parametrize("value", [None, 1, True, {}, "", "2026-09-29", "2026-09-29T10:10:00", "2026-02-30T10:10:00Z",
    "2026-09-29T24:00:00Z", "2026-09-29T10:10:60Z", "2026-09-29T10:10:00+24:00", "2026-09-29T10:10:00+00:60",
    "2026-09-29T10:10:00+0800", "2026-09-29T10:10:00.Z", "2026-09-29T10:10:00.1234567890Z",
    "2026-09-29T10:10:00Z\n", "0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000000000+00:00"])
def test_invalid_docker_fields_and_unset_start_rejected_at_formal_entry(tmp_path, field, value):
    runtime = load_runtime()
    recovery, host, _ = recovery_fixture(tmp_path, runtime, **{field: value})
    with pytest.raises(runtime.PreReleaseError):
        runtime.verify_recovery_observation(recovery, host)


@pytest.mark.parametrize("changes,accepted", [
    ({"created": "2026-09-29T10:11:00.000000001Z"}, False),
    ({"created": "2026-09-29T10:10:59.999999999Z"}, True),
    ({"started": "2026-09-29T10:10:59.999999999Z"}, False),
    ({"started": "2026-09-29T10:11:00.000000001Z"}, True),
    ({"started": "2026-09-29T10:13:21.949559001Z"}, False),
    ({"started": "2026-09-29T10:13:21.949558999Z"}, True),
    ({"started": "2026-09-29T11:11:00Z"}, False),
    ({"observed": "2026-09-29T11:11:00+00:00"}, False),
    ({"expires": "2026-09-29T10:12:00+00:00"}, False),
    ({"expires": "2026-09-29T11:11:00.000001+00:00"}, False),
    ({"created": "2026-09-29T10:12:01Z", "started": "2026-09-29T10:12:00Z"}, False),
])
def test_exact_nanosecond_order_expiry_and_one_hour_boundaries(tmp_path, changes, accepted):
    runtime = load_runtime()
    recovery, host, _ = recovery_fixture(tmp_path, runtime, **changes)
    if accepted:
        runtime.verify_recovery_observation(recovery, host)
    else:
        with pytest.raises(runtime.PreReleaseError):
            runtime.verify_recovery_observation(recovery, host)


def test_offset_equivalence_and_integer_precision_before_epoch():
    runtime = load_runtime()
    parse = runtime._docker_timestamp_nanoseconds
    assert parse("1969-12-31T23:59:59.999999999Z", "created_at") == -1
    assert parse("2026-09-29T10:11:00.000000001Z", "created_at") - parse("2026-09-29T18:11:00+08:00", "created_at") == 1
