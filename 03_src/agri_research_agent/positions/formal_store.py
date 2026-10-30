"""Immutable positioning objects with an atomic, four-board status pointer.

The existing archive and board releases remain untouched as the legacy rollback
view. New formal versions contain only indexes, appended snapshot segments and
new source objects. Readers reconstruct the familiar archive in memory.
"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import uuid

from . import delivery as codec

POINTER_SCHEMA = "commodity-positions-current/1"
INDEX_SCHEMA = "commodity-positions-object-index/1"
DIRECTORY = ".position-store"
MAX_INDEX = 8 * 1024 * 1024
MAX_SEGMENTS = 4096


def status_path(formal):
    return Path(formal).parent.parent / "update_status/commodity_positions.json"


def identity(raw):
    return {"sha256": codec.digest(raw), "size_bytes": len(raw)}


def _identity(value, limit=codec.MAX_FILE):
    codec.exact(value, ("sha256", "size_bytes"), "positions object identity")
    codec.require(isinstance(value["sha256"], str) and codec.SHA.fullmatch(value["sha256"])
                  and type(value["size_bytes"]) is int and 0 < value["size_bytes"] <= limit,
                  "positions object identity invalid")
    return value


def _object(formal, item, cache):
    _identity(item)
    sha = item["sha256"]
    if sha not in cache:
        path = codec.unlinked(Path(formal) / DIRECTORY / "objects" / sha)
        codec.require(path.is_file() and path.stat().st_nlink == 1
                      and path.stat().st_size == item["size_bytes"], "positions object missing or aliased")
        cache[sha] = path.read_bytes()
    raw = cache[sha]
    codec.require(identity(raw) == item, "positions object bytes differ")
    return raw


def pointer(formal):
    path = codec.unlinked(status_path(formal))
    if not path.exists():
        return None
    status = codec.read_json(path)
    codec.require(type(status) is dict, "positions status invalid")
    value = status.get("position_store")
    if value is None and "position_store" not in status:
        return None
    return _pointer(value)


def _pointer(value):
    codec.exact(value, ("schema_version", "index_sha256", "index_size_bytes", "archive_sha256"),
                "positions current pointer")
    codec.require(value["schema_version"] == POINTER_SCHEMA
                  and isinstance(value["archive_sha256"], str) and codec.SHA.fullmatch(value["archive_sha256"]),
                  "positions current pointer invalid")
    _identity({"sha256": value["index_sha256"], "size_bytes": value["index_size_bytes"]}, MAX_INDEX)
    return value


def index(formal, selected=None):
    selected = pointer(formal) if selected is None else selected
    if selected is None:
        return None
    selected = _pointer(selected)
    path = codec.unlinked(Path(formal) / DIRECTORY / "indexes" / (selected["index_sha256"] + ".json"))
    codec.require(path.is_file() and path.stat().st_nlink == 1
                  and path.stat().st_size == selected["index_size_bytes"] <= MAX_INDEX,
                  "positions index missing or aliased")
    raw = path.read_bytes()
    codec.require(codec.digest(raw) == selected["index_sha256"], "positions index SHA differs")
    value = codec.strict_json(raw)
    codec.exact(value, ("schema_version", "archive_sha256", "files"), "positions object index")
    codec.require(value["schema_version"] == INDEX_SCHEMA
                  and value["archive_sha256"] == selected["archive_sha256"]
                  and type(value["files"]) is dict and 0 < len(value["files"]) <= codec.MAX_FILES,
                  "positions object index invalid")
    for name, item in value["files"].items():
        parts = codec.relative(name).parts
        codec.require(len(parts) >= 2 and parts[0] in codec.DOMAINS, "positions index path invalid")
        codec.require(type(item) is dict and item.get("kind") in {"object", "snapshot"},
                      "positions index file kind invalid")
        keys = ("kind", "sha256", "size_bytes")
        codec.exact(item, (*keys, "base", "updates") if item["kind"] == "snapshot" else keys,
                    "positions index file")
        _identity({k: item[k] for k in ("sha256", "size_bytes")})
        if item["kind"] == "snapshot":
            codec.require(len(parts) == 4 and parts[1] == "releases" and parts[-1] == "snapshot.json"
                          and codec.RELEASE.fullmatch(parts[2]), "positions snapshot index path invalid")
            _identity(item["base"])
            codec.require(type(item["updates"]) is list and 0 < len(item["updates"]) <= MAX_SEGMENTS,
                          "positions segment list invalid")
            for update in item["updates"]:
                _identity(update)
            codec.require(len({u["sha256"] for u in item["updates"]}) == len(item["updates"]),
                          "duplicate positions segment")
    return value


def _file(formal, item, cache):
    expected = {k: item[k] for k in ("sha256", "size_bytes")}
    if item["kind"] == "object":
        return _object(formal, expected, cache)
    payload = codec.strict_json(_object(formal, item["base"], cache))
    known = {kind: set(codec._groups(payload[kind], kind)) for kind in ("foreign", "domestic")}
    for segment in item["updates"]:
        update = codec.strict_json(_object(formal, segment, cache))
        codec.exact(update, ("release_id", "published_at", "foreign", "domestic", "sources", "attempts"),
                    "positions stored update")
        codec.require(type(update["sources"]) is dict and type(update["attempts"]) is list,
                      "positions stored update metadata invalid")
        rows = {}
        for kind in ("foreign", "domestic"):
            codec.require(type(update[kind]) is list, "positions stored update rows invalid")
            groups = set(codec._groups(update[kind], kind))
            codec.require(not (known[kind] & groups),
                          "positions stored update rewrites history")
            known[kind].update(groups)
            rows[kind] = payload[kind] + update[kind]
        payload = {**payload, **update, **rows, "sources": {**payload["sources"], **update["sources"]}}
    raw = codec.canonical(payload)
    codec.require(identity(raw) == expected, "positions virtual snapshot identity differs")
    return raw


def archive_files(formal, selected=None, domain=None):
    value = index(formal, selected)
    if value is None:
        return None
    cache = {}
    files = {name: _file(formal, item, cache) for name, item in value["files"].items()
             if domain is None or name.startswith(domain + "/")}
    if domain is None:
        codec.require(codec.canonical_sha256(codec.encode(files)) == value["archive_sha256"],
                      "positions logical archive identity differs")
    return files


def read_archive(formal, selected=None):
    files = archive_files(formal, selected)
    return None if files is None else codec.encode(files)


def read_snapshot(root):
    root = Path(root)
    if root.name not in codec.DOMAINS or root.parent.name != "commodity_positions":
        return None
    selected = pointer(root.parent)
    if selected is None:
        return None
    value = index(root.parent, selected)
    cache = {}
    def get(name):
        return _file(root.parent, value["files"][root.name + "/" + name], cache)
    current = codec.strict_json(get("current.json"))
    release = current["release_id"]
    codec.require(isinstance(release, str) and codec.RELEASE.fullmatch(release), "positions release invalid")
    manifest_raw = get("releases/" + release + "/manifest.json")
    manifest = codec.strict_json(manifest_raw)
    raw = get("releases/" + release + "/snapshot.json")
    codec.require(codec.digest(manifest_raw) == current["manifest_sha256"]
                  and codec.digest(raw) == manifest["snapshot_sha256"], "positions current release identity differs")
    payload = codec.strict_json(raw)
    codec.require(payload["schema_version"] == 1 and payload["release_id"] == manifest["release_id"] == release,
                  "positions current snapshot invalid")
    codec.require(pointer(root.parent) == selected, "positions Current moved during read")
    return payload


def _seal(path, raw):
    path = codec.unlinked(path)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir(mode=0o755)
        parent.chmod(0o755)
        _sync_directory(parent.parent)
    if path.exists():
        codec.require(path.is_file() and path.stat().st_nlink == 1
                      and path.stat().st_size == len(raw) and path.read_bytes() == raw
                      and not path.stat().st_mode & 0o222, "immutable positions object changed")
        return False
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as out:
            out.write(raw); out.flush(); os.fsync(out.fileno())
        if os.name == "nt":
            # Windows rename refuses an existing destination; no shared inode
            # is created and read-only attributes do not obstruct temp cleanup.
            os.rename(temporary, path)
            path.chmod(0o444)
        else:
            temporary.chmod(0o444)
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    _sync_directory(path.parent)
    codec.require(path.read_bytes() == raw and path.stat().st_nlink == 1, "positions sealed object differs")
    return True


def _sync_directory(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def prepare(formal, baseline, document, resolved):
    """Append objects/indexes only. Caller verifies CAS and atomically writes status."""
    formal = codec.unlinked(formal)
    metrics = {"new_object_bytes": 0, "new_objects": 0, "bootstrap_object_bytes": 0,
               "index_bytes_written": 0, "full_archive_written": False, "full_history_copies": 0}
    def put(raw):
        item = identity(raw)
        if _seal(formal / DIRECTORY / "objects" / item["sha256"], raw):
            metrics["new_objects"] += 1; metrics["new_object_bytes"] += len(raw)
        return item
    def save(value):
        raw = codec.canonical(value); item = identity(raw)
        codec.require(len(raw) <= MAX_INDEX, "positions index exceeds bound")
        if _seal(formal / DIRECTORY / "indexes" / (item["sha256"] + ".json"), raw):
            metrics["index_bytes_written"] += len(raw)
        return {"schema_version": POINTER_SCHEMA, "index_sha256": item["sha256"],
                "index_size_bytes": item["size_bytes"], "archive_sha256": value["archive_sha256"]}
    def seed(archive):
        return {"schema_version": INDEX_SCHEMA, "archive_sha256": codec.canonical_sha256(archive),
                "files": {name: {"kind": "object", **put(raw)} for name, raw in codec.decode(archive).items()}}
    previous = index(formal)
    if previous is None and baseline is not None:
        previous = seed(baseline)
        save(previous)
        metrics["bootstrap_object_bytes"] = metrics["new_object_bytes"]
    if previous is not None:
        codec.require(baseline is not None and previous["archive_sha256"] == codec.canonical_sha256(baseline),
                      "positions store baseline differs")
    files, cache = {}, {}
    for name, entry in resolved["files"].items():
        expected = {k: entry[k] for k in ("sha256", "size_bytes")}
        old = previous["files"].get(name) if previous else None
        if old is not None and {k: old[k] for k in expected} == expected:
            files[name] = deepcopy(old)
            continue
        domain = name.split("/", 1)[0]
        if (previous is not None and document["schema_version"] == codec.INCREMENT_SCHEMA
                and name.endswith("/snapshot.json") and domain in document["updates"]):
            current = codec.strict_json(_file(formal, previous["files"][domain + "/current.json"], cache))
            old_snapshot = previous["files"][domain + "/releases/" + current["release_id"] + "/snapshot.json"]
            base = (old_snapshot["base"] if old_snapshot["kind"] == "snapshot" else
                    {k: old_snapshot[k] for k in ("sha256", "size_bytes")})
            updates = old_snapshot["updates"] if old_snapshot["kind"] == "snapshot" else []
            files[name] = {"kind": "snapshot", **expected, "base": deepcopy(base),
                           "updates": [*deepcopy(updates), put(codec.canonical(document["updates"][domain]))]}
            codec.require(identity(_file(formal, files[name], {})) == expected, "positions append projection differs")
        else:
            raw = codec.decode({"schema_version": codec.SCHEMA, "files": {name: entry}})[name]
            files[name] = {"kind": "object", **put(raw)}
    value = {"schema_version": INDEX_SCHEMA, "archive_sha256": codec.canonical_sha256(resolved), "files": files}
    selected = save(value)
    codec.require(read_archive(formal, selected) == resolved, "positions stored archive differs")
    metrics["increment_object_bytes"] = metrics["new_object_bytes"] - metrics["bootstrap_object_bytes"]
    return selected, metrics
