"""Run with approved absolute Python -I -B from a clean detached control clone."""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "03_src/agri_research_agent/automation/production_data_delta.py"


def _bootstrap(config: dict) -> None:
    """Verify control bytes before importing any repository module."""
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise ValueError("formal entrypoint requires Python -I -B")
    if not (ROOT / ".git").is_dir() or (ROOT / ".git").is_symlink():
        raise ValueError("independent ordinary control clone required")
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "COMSPEC"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS="1")
    executable = str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/cmd/git.exe") if os.name == "nt" else "git"
    def git(*args: str) -> bytes:
        result = subprocess.run([executable, "-c", "core.fsmonitor=false", "-C", str(ROOT), *args],
                                env=env, capture_output=True, check=False, timeout=300)
        if result.returncode:
            raise ValueError("control source verification failed")
        return result.stdout
    if (git("rev-parse", "HEAD").decode().strip() != config["approved_commit"] or
        git("rev-parse", "HEAD^{tree}").decode().strip() != config["approved_tree"] or
        git("rev-parse", "--abbrev-ref", "HEAD").decode().strip() != "HEAD" or
        git("remote", "get-url", "origin").decode().strip() != config["origin"]):
        raise ValueError("control source identity differs")
    expected = {Path(__file__).resolve().relative_to(ROOT).as_posix(), MODULE.relative_to(ROOT).as_posix()}
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", "HEAD")), mode="r:") as archive:
        for member in archive:
            if member.name in expected:
                path = ROOT / member.name
                if not member.isfile() or path.is_symlink() or path.read_bytes() != archive.extractfile(member).read():
                    raise ValueError("control executable bytes differ")
                seen.add(member.name)
    if seen != expected:
        raise ValueError("control executable absent from approved archive")


def load_module():
    spec = importlib.util.spec_from_file_location("production_data_delta", MODULE)
    if not spec or not spec.loader:
        raise ValueError("approved producer unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--domain", choices=("akshare", "soybean_crop_progress", "soybean_export_sales"), required=True)
    parser.add_argument(
        "--end-date",
        help="AkShare business end date in strict YYYY-MM-DD form; defaults to today",
    )
    parser.add_argument("--run-root", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--candidate-only", action="store_true", help="Produce locally; no SSH, publication or continuation activation (default)")
    mode.add_argument("--publish", action="store_true", help="Produce, transfer, validate, publish and persist verified continuation")
    args = parser.parse_args(argv)
    try:
        def closed_pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError("duplicate configuration key")
                value[key] = item
            return value
        raw = args.config.read_bytes()
        config = json.loads(raw.decode("utf-8"), object_pairs_hook=closed_pairs)
        _bootstrap(config)
        module = load_module()
        module.validate_config(config)
        module.verify_clean_detached_clone(ROOT, config)
        if args.domain != "akshare" and args.end_date is not None:
            raise ValueError("--end-date is only valid with --domain akshare")
        end_date = (
            module.resolve_business_end_date(args.end_date)
            if args.domain == "akshare"
            else None
        )
        sys.path.insert(0, str(ROOT / "03_src"))
        result = module.run_domain(
            config,
            args.domain,
            run_root=args.run_root,
            publish=args.publish,
            end_date=end_date,
        )
        if args.config.read_bytes() != raw:
            raise ValueError("configuration changed during run")
        print(json.dumps({"PRODUCTION_DATA_DELTA": result["status"], **result}, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"PRODUCTION_DATA_DELTA": "FAIL", "reason": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
