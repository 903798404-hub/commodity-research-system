"""Read fresh, pinned production baselines; never write into an input tree."""
from __future__ import annotations

import base64
from pathlib import Path, PurePosixPath

from agri_research_agent.automation import production_data_delta as delivery
from agri_research_agent.data_sources.nutstore_basis import assert_external_output


def domain_baseline(config, work, domain, schema):
    policy = config["policy"][domain]
    delivery.require(delivery._remote_hash(config, config["publisher"]) == config["publisher_sha256"]
                     and delivery._remote_hash(config, policy) == config["policy_sha256"][domain],
                     "scheduled host pins differ")
    result = delivery.strict_json(delivery._ssh(config, ["sudo", "-n", "python3", "-I",
        config["publisher"], "snapshot-baseline", "--policy", policy]))
    delivery.require(set(result) == {"schema_version", "domain", "policy_sha256", "producer",
        "image_id", "allocation_root", "files", "contents"}
        and result["schema_version"] == "production-domain-baseline-snapshot/1"
        and result["domain"] == domain and result["producer"] == {
            "commit": config["approved_commit"], "tree": config["approved_tree"], "origin": config["origin"]}
        and result["policy_sha256"] == config["policy_sha256"][domain]
        and result["image_id"] == config["image_id"]
        and result["allocation_root"] == config["remote_allocation"], "scheduled baseline identity differs")
    host = delivery._host_contract()
    expected = set(host.DOMAIN_CONTRACTS[domain]["baseline"])
    delivery.require(set(result["files"]) == set(result["contents"]) == expected
                     and all(v is not None for v in result["files"].values()),
                     "scheduled updates require an initialized complete baseline")
    root = assert_external_output(work / "baseline")
    total = 0
    decoded = {}
    for relative, identity in result["files"].items():
        host._file_identity(identity)
        total += identity["size_bytes"]
        delivery.require(total <= 384 * 1024 * 1024 and identity["size_bytes"] <= 384 * 1024 * 1024,
                         "scheduled baseline size bound exceeded")
        encoded = result["contents"][relative]
        delivery.require(type(encoded) is str and len(encoded) == 4 * ((identity["size_bytes"] + 2) // 3),
                         "scheduled baseline encoded size differs")
        raw = base64.b64decode(encoded, validate=True)
        delivery.require(len(raw) == identity["size_bytes"]
                         and delivery.hashlib.sha256(raw).hexdigest() == identity["sha256"],
                         "scheduled baseline transfer differs")
        decoded[relative] = raw
    root.mkdir()
    for relative, raw in decoded.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (root / "baseline_manifest.json").write_bytes(delivery.canonical_json_bytes({
        "schema_version": schema, "source_root": config["remote_allocation"], "files": result["files"]}))
    return {**config, "baseline_root": str(root),
            "baseline_manifest_sha256": delivery.sha256_file(root / "baseline_manifest.json")}


def public_baseline(config, destination):
    """Download exactly the active immutable package; confirm hashes and pointer twice."""
    from agri_research_agent.pipelines.public_data_delivery import validate_production_package
    destination = assert_external_output(destination)
    pointer = delivery._public_pointer(config)
    remote = config["remote_store_root"] + "/releases/" + pointer["package_id"]
    raw = delivery._run(["ssh", *delivery.SSH_OPTIONS, "-T", config["ssh_target"],
                        "cat -- " + remote + "/manifest.json"], binary=True, timeout=120).stdout
    delivery.require(len(raw) <= 4 * 1024 * 1024, "public baseline manifest too large")
    manifest = delivery.strict_json(raw)
    delivery._check_public_pointer(pointer, manifest)
    delivery.require(delivery._remote_hash(config, remote + "/manifest.json") ==
                     delivery.hashlib.sha256(raw).hexdigest(), "public baseline manifest identity differs")
    entries = manifest["files"]
    delivery.require(type(entries) is list and 0 < len(entries) <= 4096, "public baseline file count invalid")
    seen, total = set(), 0
    for item in entries:
        rel = PurePosixPath(item["path"])
        delivery.require(str(rel) == item["path"] and not rel.is_absolute() and ".." not in rel.parts
                         and delivery.re.fullmatch(r"[A-Za-z0-9_./-]+", item["path"])
                         and item["path"] not in seen, "public baseline file path invalid")
        delivery.require(type(item["size_bytes"]) is int and item["size_bytes"] > 0
                         and type(item["sha256"]) is str and delivery.HEX64.fullmatch(item["sha256"]),
                         "public baseline file identity invalid")
        seen.add(item["path"])
        total += item["size_bytes"]
    delivery.require(total <= 1024 * 1024 * 1024, "public baseline total size exceeded")
    root = destination / pointer["package_id"]
    root.mkdir(parents=True)
    (root / "manifest.json").write_bytes(raw)
    for item in entries:
        target = root / "data" / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        delivery._run(["scp", *delivery.SSH_OPTIONS, "--", config["ssh_target"] + ":" + remote +
                       "/data/" + item["path"], str(target)], timeout=180)
        delivery.require(delivery._identity(target) == {k: item[k] for k in ("sha256", "size_bytes")},
                         "public baseline transferred file differs")
    delivery.require(delivery._public_pointer(config) == pointer, "public baseline moved while copying")
    validate_production_package(root)
    return root
