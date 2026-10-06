import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "fixtures/summary"


def test_frozen_summary_samples_match_the_complete_manifest():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    actual = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.parquet")}
    expected = {item["path"] for item in manifest["files"]}
    assert len(expected) == len(manifest["files"]) == 18 and actual == expected
    for item in manifest["files"]:
        path = ROOT / item["path"]
        assert not path.is_symlink() and path.resolve().is_relative_to(ROOT.resolve())
        raw = path.read_bytes()
        assert len(raw) == item["size_bytes"] and hashlib.sha256(raw).hexdigest() == item["sha256"]
