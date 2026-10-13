"""Build lossless gzip sidecars for the pinned Streamlit frontend, once per image."""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path


def precompress_assets(root: Path) -> dict[str, int]:
    root = root.resolve(strict=True)
    count = original_bytes = compressed_bytes = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Static asset symlinks are not supported")
        if not path.is_file() or path.suffix.lower() not in {".js", ".css", ".svg", ".json"}:
            continue
        source = path.read_bytes()
        if len(source) < 1024:
            continue
        compressed = gzip.compress(source, compresslevel=6, mtime=0)
        if len(compressed) >= len(source):
            continue
        assert gzip.decompress(compressed) == source
        sidecar = path.with_name(path.name + ".gz")
        # Build-only artifacts: no request-time writes or cache generation.
        sidecar.write_bytes(compressed)
        os.utime(sidecar, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns))
        count += 1
        original_bytes += len(source)
        compressed_bytes += len(compressed)
    return dict(files=count, original_bytes=original_bytes, compressed_bytes=compressed_bytes)


def main() -> None:
    import streamlit

    assets = Path(streamlit.__file__).resolve().parent / "static" / "static"
    expected = Path("/usr/local/lib/python3.12/site-packages/streamlit/static/static")
    if assets != expected:
        raise RuntimeError("Pinned frontend location differs from the nginx static root")
    result = precompress_assets(assets)
    if not result["files"]:
        raise RuntimeError("No frontend assets were precompressed")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
