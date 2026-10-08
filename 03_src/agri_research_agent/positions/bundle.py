"""Export digest-checked immutable inputs, without writing any runtime."""
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from agri_research_agent.positions.workspace import DOMAINS, local_root, validate_domain
from agri_research_agent.sugar_positions.storage import digest, read_snapshot


def verified_files(root: Path, project_root: Path, domain: str) -> tuple[dict, dict]:
    current = (root / "current.json").read_bytes()
    pointer = json.loads(current.decode("utf-8"))
    snapshot = validate_domain(read_snapshot(root), project_root, domain)
    if snapshot.get("release_id") != pointer["release_id"]:
        raise ValueError("快照在导出期间发生变化")
    if not snapshot["foreign"] and not snapshot["domestic"]:
        raise ValueError("不能导出没有已验证数据的板块")
    release = Path("releases") / snapshot["release_id"]
    files = {"current.json": current}
    for rel in [release / "manifest.json", release / "snapshot.json"]:
        source = root / rel
        if source.resolve() != root.resolve() / rel:
            raise ValueError("导出文件不能重定向")
        files[rel.as_posix()] = source.read_bytes()
    if digest(files[(release / "snapshot.json").as_posix()]) != json.loads(
            files[(release / "manifest.json").as_posix()])["snapshot_sha256"]:
        raise ValueError("导出期间快照内容发生变化")
    if digest(files[(release / "manifest.json").as_posix()]) != pointer["manifest_sha256"]:
        raise ValueError("导出期间清单内容发生变化")
    for source in snapshot["sources"].values():
        rel = Path(source["raw_file"])
        if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 2 or rel.parts[0] != "raw":
            raise ValueError("原始来源路径无效")
        path = root / rel
        if path.resolve() != root.resolve() / rel:
            raise ValueError("原始来源不能重定向")
        raw = path.read_bytes()
        if digest(raw) != source["sha256"]:
            raise ValueError("原始来源 SHA 校验失败")
        files[rel.as_posix()] = raw
    attempt = root / "last_attempt.json"
    if attempt.is_file():
        raw = attempt.read_bytes()
        json.loads(raw.decode("utf-8"))
        files["last_attempt.json"] = raw
    if (root / "current.json").read_bytes() != current:
        raise ValueError("快照在导出期间发生变化，请重试")
    return snapshot, files


def export_bundle(project_root: Path, domains: list[str]) -> Path:
    if not domains or len(domains) != len(set(domains)) or set(domains) - set(DOMAINS):
        raise ValueError("导出板块必须有效且不重复")
    prepared = {domain: verified_files(local_root(project_root, domain), project_root, domain)
                for domain in domains}
    output = project_root.resolve() / "06_outputs/commodity_positions/bundles"
    if output.resolve() != output:
        raise ValueError("导出目录不能指向其他位置")
    destination = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8])
    destination.mkdir(parents=True)
    manifest = {"schema_version": "commodity-positions-bundle/1", "domains": {}, "files": {}}
    for domain, (snapshot, files) in prepared.items():
        manifest["domains"][domain] = {"release_id": snapshot["release_id"],
            "foreign_rows": len(snapshot["foreign"]), "domestic_rows": len(snapshot["domestic"])}
        for rel, raw in files.items():
            target = destination / "domains" / domain / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            if target.read_bytes() != raw:
                raise ValueError("导出写入校验失败")
            manifest["files"][target.relative_to(destination).as_posix()] = digest(raw)
    (destination / "bundle.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination
