"""Root-controlled candidate validation records for same-image release validation.

This entrypoint accepts a registered project, never caller-provided probe results
or raw evidence. Candidate records attest validation, not production approval.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[2]
ENGINE = "04_scripts/runtime/validate_target_runtime.py"
RECORD = "09_deploy/runtime_identity/candidate_validation_record.py"
HOST = "09_deploy/runtime_identity/host_authorization.py"
TRUST = "02_configs/production_runtime_trust.json"
KEY_DIRECTORY = Path("/etc/market-data/runtime-identity")


class PreReleaseError(ValueError):
    """Candidate validation or release identity could not be established."""


class ValidationBlocked(PreReleaseError):
    """The authorized Linux builder is unavailable."""


def _load(relative: str, name: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PreReleaseError("trusted source cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        # Never execute ignored bytecode, even if it has a matching timestamp.
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


def require_source(host, engine, *, source_root: Path | None = None) -> tuple[str, str]:
    """Require this exact detached source and its Git database to be protected."""
    root = ROOT if source_root is None else source_root
    host._require_linux_root()
    if any(key.startswith("GIT_") for key in os.environ):
        raise PreReleaseError("caller Git environment is forbidden")
    host._protected_path(root, directory=True)
    host._protected_path(root / ".git", directory=True)
    # Check all files, including Git config/hooks and ignored files, before any
    # Git subprocess. Execution contracts below additionally use committed bytes.
    for path in root.rglob("*"):
        host._protected_path(path, directory=path.is_dir())
    if engine._git(root, "rev-parse", "--show-toplevel") != str(root):
        raise PreReleaseError("source repository root differs")
    if engine._git(root, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
        raise PreReleaseError("release source must be detached")
    if engine._git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise PreReleaseError("release source is dirty")
    if engine._git(root, "for-each-ref", "refs/replace"):
        raise PreReleaseError("Git replacement refs are forbidden")
    flags = engine._git(root, "ls-files", "-v", "-z", binary=True).split(b"\0")
    if any(row and not row.startswith(b"H ") for row in flags):
        raise PreReleaseError("hidden Git index changes are forbidden")
    tracked = {row[2:].decode("utf-8", "strict") for row in flags if row}
    archived = set()
    raw = engine._git(root, "archive", "--format=tar", "HEAD", binary=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile() or member.name not in tracked or member.name in archived:
                raise PreReleaseError("source archive contains an undeclared or aliased file")
            archived.add(member.name)
            stream = archive.extractfile(member)
            if stream is None or (root / member.name).read_bytes() != stream.read():
                raise PreReleaseError("source bytes differ from Approved Git object")
    if archived != tracked:
        raise PreReleaseError("source archive omits tracked files")
    return engine._git(root, "rev-parse", "HEAD"), engine._git(root, "rev-parse", "HEAD^{tree}")


def _new_output(host, destination: Path) -> None:
    if not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise PreReleaseError("record output must be a new absolute file")
    host._protected_path(destination.parent, directory=True)
    if destination == ROOT or ROOT in destination.parents:
        raise PreReleaseError("record output must be outside source repository")


def _write_new(host, destination: Path, raw: bytes) -> None:
    _new_output(host, destination)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    host._fsync_directory(destination.parent)


def _execute_validation(project: dict, output: Path) -> int:
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME"}}
    return subprocess.run(
        [sys.executable, "-I", "-B", str(ROOT / ENGINE), "--project", project["project_id"],
         "--runtime-contract", project["runtime_contract"], "--evidence-output", str(output)],
        cwd=ROOT, env=env, check=False, timeout=3900,
    ).returncode


def validate_candidate(project_id: str, destination: Path, key_path: Path,
                       *, ttl_seconds: int = 86400) -> dict:
    """Run the actual engine, verify its result, and exclusively seal a record."""
    if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ValidationBlocked("LINUX_BUILDER_UNAVAILABLE")
    host = _load(HOST, "_pre_release_host")
    host.require_protected_authority_source()
    for name in (ENGINE, RECORD, "04_scripts/runtime/pre_release_runtime.py"):
        host._protected_path(ROOT / name)
    engine = _load(ENGINE, "_pre_release_engine")
    before = require_source(host, engine)
    _new_output(host, destination)
    if type(ttl_seconds) is not int or not 0 < ttl_seconds <= 7 * 86400:
        raise PreReleaseError("invalid candidate record lifetime")
    if key_path.parent != KEY_DIRECTORY:
        raise PreReleaseError("candidate signing key must use the fixed host key directory")
    key = host._load_private_key(key_path)
    trust_raw = (ROOT / TRUST).read_bytes()
    trust = host._json(trust_raw)
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii")
    matches = [item for item in trust.get("keys", []) if item.get("domain") == "candidate_validation"
               and item.get("algorithm") == "ed25519" and item.get("public_key_base64") == public]
    if len(matches) != 1 or matches[0]["key_id"] in trust.get("revoked_key_ids", []):
        raise PreReleaseError("candidate key is untrusted or revoked")
    project = engine._project(ROOT, project_id)
    _, _, binding = engine.source_contract(ROOT, project_id, project["runtime_contract"])
    if (binding["commit"], binding["tree"]) != before:
        raise PreReleaseError("candidate identity changed before validation")
    record = _load(RECORD, "_pre_release_record")
    # Root-private, uniquely allocated evidence location: the CLI never imports
    # an externally supplied evidence file and does not expose this path as input.
    with tempfile.TemporaryDirectory(prefix="candidate-validation-", dir=destination.parent) as folder:
        output = Path(folder) / "evidence.json"
        code = _execute_validation(project, output)
        if code == 3:
            raise ValidationBlocked("LINUX_BUILDER_UNAVAILABLE")
        if code != 0 or not output.is_file() or output.is_symlink():
            raise PreReleaseError("candidate engine did not complete successfully")
        evidence = host._json(output.read_bytes())
    if evidence.get("binding") != binding:
        raise PreReleaseError("candidate evidence binding differs")
    now = datetime.now(timezone.utc)
    payload = {"record_id": os.urandom(16).hex(), "purpose": "target-runtime-validation",
               "authorization_role": "candidate_validation", "issued_at": now.isoformat(),
               "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
               "evidence_sha256": hashlib.sha256(record.canonical(evidence)).hexdigest(),
               "evidence": evidence}
    record.validate_payload(payload)
    envelope = {"schema_version": "candidate-validation-record/1", "algorithm": "ed25519",
                "key_id": matches[0]["key_id"], "payload": payload}
    envelope["signature"] = base64.b64encode(key.sign(record.canonical(envelope))).decode("ascii")
    raw = record.canonical(envelope)
    record.verify_record(raw, trust, now=now)
    if require_source(host, engine) != before or (ROOT / TRUST).read_bytes() != trust_raw:
        raise PreReleaseError("candidate source changed during validation")
    if engine.source_contract(ROOT, project_id, project["runtime_contract"])[2] != binding:
        raise PreReleaseError("candidate binding changed during validation")
    _write_new(host, destination, raw)
    return payload


def _validate_production_revalidation_report(report: object, container_id: str) -> dict:
    """Apply the common strict report contract for production policy v3/v5."""
    if not isinstance(report, dict):
        raise PreReleaseError("host returned an invalid revalidation report")
    required = {"schema_version", "PRE_RELEASE_VALIDATION", "production_write_granted",
                "container_started", "container_id"}
    if (not required.issubset(report) or report["schema_version"] != "production-pre-release-validation/1"
            or report["PRE_RELEASE_VALIDATION"] != "PASS"
            or report["production_write_granted"] is not False
            or report["container_started"] is not False
            or report["container_id"] != container_id):
        raise PreReleaseError("host returned an invalid revalidation report")
    return report


def revalidate_production(container_id: str, policy_path: Path, destination: Path) -> dict:
    """Revalidate a fresh, unstarted production instance using host authority.

    This deliberately has no signing-key or grant-writing path.  The host owns
    policy parsing, Docker observation, and report generation; this wrapper only
    establishes protected source state and atomically publishes the host report.
    """
    if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ValidationBlocked("LINUX_BUILDER_UNAVAILABLE")
    if type(container_id) is not str or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise PreReleaseError("invalid container id")
    if not isinstance(policy_path, Path):
        policy_path = Path(policy_path)
    if not isinstance(destination, Path):
        destination = Path(destination)
    host = _load(HOST, "_pre_release_host_revalidation")
    host.require_protected_authority_source()
    for name in (ENGINE, RECORD, "04_scripts/runtime/pre_release_runtime.py"):
        host._protected_path(ROOT / name)
    # A caller-supplied policy is data only; it must be an existing protected
    # root-owned file and is subsequently re-read/validated by the host.
    engine = _load(ENGINE, "_pre_release_engine_revalidation")
    before = require_source(host, engine)
    _new_output(host, destination)
    report = host.revalidate_production(container_id, expected_policy_path=policy_path)
    report = _validate_production_revalidation_report(report, container_id)
    digest = hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    result = {"report": report, "report_sha256": digest}
    raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if require_source(host, engine) != before:
        raise PreReleaseError("production source changed during revalidation")
    _write_new(host, destination, raw)
    return result



# Release planning is not execution authorization. These helpers never issue a
# grant or start a container; the existing production mode remains unchanged.
RISK_SCHEMA = "production-release-risk/1"


def _risk_git(repo: Path, *args: str) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return subprocess.check_output(["git", "-C", str(repo), *args], env=env, timeout=30)


def _risk_identity(repo: Path, commit: str) -> dict:
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PreReleaseError("risk assessment requires exact commits")
    actual = _risk_git(repo, "rev-parse", commit + "^{commit}").decode().strip()
    if actual != commit:
        raise PreReleaseError("commit identity differs")
    return {"commit": commit, "tree": _risk_git(repo, "rev-parse", commit + "^{tree}").decode().strip()}


def _runtime_graph(sources: dict[str, bytes], contract: dict) -> dict:
    """Conservative local import/file-reference closure, never execute imports.

    Includes function-local imports and both branches. Literal module/file loads
    are followed; unresolved dynamic loaders make exclusion evidence incomplete.
    source_inputs is a packaging inventory, not an execution root list.
    """
    import ast
    roots = ('', '03_src/', '04_scripts/', '05_apps/')
    modules = {}
    for path in sources:
        if path.endswith('.py'):
            for prefix in roots:
                if path.startswith(prefix):
                    name = path[len(prefix):-3].replace('/', '.')
                    modules.setdefault(name.removesuffix('.__init__'), set()).add(path)
    entry = contract.get('entrypoint', [])
    commands = [entry, *(c['argv'] for c in contract.get('initialization_commands', []))]
    starts = set()
    for argv in commands:
        for item in argv:
            path = item.removeprefix('/app/').removeprefix('./')
            if path in sources and path.endswith('.py'):
                starts.add(path)
        if '-m' in argv:
            starts.update(modules.get(argv[argv.index('-m')+1], ()))
    active, reasons, uncertain = set(), {}, []
    queue = [(p, 'runtime command') for p in sorted(starts)]
    for compose in contract.get('build', {}).get('compose_sources', []):
        raw = sources.get(compose, b'').decode('utf-8')
        # Compose command/env-file/environment paths are runtime inputs too.
        queue.extend((p, compose + ': runtime configuration reference') for p in sources
                     if p in raw or '/app/' + p in raw)
    basename = {}
    for path in sources:
        basename.setdefault(path.rsplit('/', 1)[-1], set()).add(path)
    while queue:
        path, why = queue.pop()
        if path in active:
            continue
        active.add(path)
        reasons[path] = why
        if not path.endswith('.py'):
            continue
        try:
            node = ast.parse(sources[path])
        except SyntaxError:
            uncertain.append(path + ': syntax unavailable')
            continue
        def follow_module(name):
            for end in range(1, len(name.split('.'))+1):
                partial = '.'.join(name.split('.')[:end])
                queue.extend((p, path + ': import ' + name) for p in modules.get(partial, ()))
        package = path.rsplit('/', 1)[0].replace('/', '.') if '/' in path else ''
        for prefix in ('03_src.', '04_scripts.', '05_apps.'):
            package = package.removeprefix(prefix)
        for value in ast.walk(node):
            if isinstance(value, ast.Import):
                for alias in value.names:
                    follow_module(alias.name)
            elif isinstance(value, ast.ImportFrom):
                parent = package.split('.')
                if value.level:
                    parent = parent[:len(parent)-value.level+1]
                    name = '.'.join([*parent, *([value.module] if value.module else [])])
                else:
                    name = value.module or ''
                follow_module(name)
                for alias in value.names:
                    follow_module(name + '.' + alias.name)
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                # Includes Path(root)/'config.json', literal script argv and
                # data/config loader references. Ambiguous basenames include all.
                literal = value.value.removeprefix('/app/').removeprefix('./')
                if literal in sources:
                    queue.append((literal, path + ': literal file reference'))
                elif literal in basename:
                    queue.extend((p, path + ': basename reference') for p in basename[literal])
            elif isinstance(value, ast.Call):
                name = ast.unparse(value.func)
                if name.endswith(('import_module', '__import__', 'run_module')):
                    if value.args and isinstance(value.args[0], ast.Constant) and isinstance(value.args[0].value, str):
                        follow_module(value.args[0].value)
                    else:
                        uncertain.append(path + ': dynamic module loader')
                elif name.endswith(('spec_from_file_location', 'run_path')) or name in ('eval', 'exec'):
                    uncertain.append(path + ': dynamic code loader')
    return dict(roots=sorted(starts), active_paths=sorted(active), evidence=reasons,
                complete=bool(starts) and not uncertain, unresolved=sorted(set(uncertain)))


def _stateless_presentation_change(old: bytes, new: bytes, sources: dict[str, bytes]) -> bool:
    """Review effect *delta*, retaining unchanged operations, not a purity theorem.

    Allows local month/list/dict shaping and clock reads. New opaque calls,
    import-side effects, argument/global mutation and writes remain high risk.
    """
    import ast
    before, after = ast.parse(old), ast.parse(new)
    dump = lambda node: ast.dump(node, include_attributes=False)
    old_functions = {n.name: n for n in before.body if isinstance(n, ast.FunctionDef)}
    new_functions = {n.name: n for n in after.body if isinstance(n, ast.FunctionDef)}
    if old_functions.keys() - new_functions.keys():
        return False
    old_imports = {dump(n) for n in before.body if isinstance(n, (ast.Import, ast.ImportFrom))}
    for n in after.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)) and dump(n) not in old_imports:
            if not isinstance(n, ast.ImportFrom) or n.module not in ('datetime', 'zoneinfo') or any(a.asname for a in n.names):
                return False
    nondefs = lambda tree: [dump(n) for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.Import, ast.ImportFrom))]
    if nondefs(before) != nondefs(after):
        return False
    changed = {k: n for k, n in new_functions.items() if k not in old_functions or dump(n) != dump(old_functions[k])}
    imported = {}
    for n in after.body:
        if isinstance(n, ast.ImportFrom) and n.module:
            path = '03_src/' + n.module.replace('.', '/') + '.py'
            if path in sources:
                for fn in ast.parse(sources[path]).body:
                    if isinstance(fn, ast.FunctionDef):
                        for alias in n.names:
                            if alias.name == fn.name:
                                imported[alias.asname or alias.name] = fn
    visiting = set()
    def safe(fn, prior=None):
        if fn.name in visiting:
            return False
        visiting.add(fn.name)
        try:
            previous_calls = {dump(n) for n in ast.walk(prior) if isinstance(n, ast.Call)} if prior else set()
            previous_nodes = {dump(n) for n in ast.walk(prior)} if prior else set()
            for expression in [*fn.decorator_list, *fn.args.defaults, *(x for x in fn.args.kw_defaults if x)]:
                if any(isinstance(n, ast.Call) for n in ast.walk(expression)):
                    return False
            local_containers = {n.targets[0].id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                                and len(n.targets)==1 and isinstance(n.targets[0], ast.Name)
                                and isinstance(n.value, (ast.List, ast.Dict, ast.ListComp, ast.DictComp))}
            for n in ast.walk(fn):
                if isinstance(n, (ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith, ast.Await, ast.Yield, ast.Delete)):
                    if dump(n) not in previous_nodes:
                        return False
                if isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, ast.Store):
                    return False
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    return False
                if not isinstance(n, ast.Call) or dump(n) in previous_calls:
                    continue
                name = ast.unparse(n.func)
                if isinstance(n.func, ast.Name):
                    if name in ('str','int','float','bool','len','range','sorted','min','max','tuple','list','dict','set','isinstance','type','enumerate','zip','abs','round','ZoneInfo'):
                        continue
                    called = changed.get(name) or imported.get(name)
                    if called and safe(called, old_functions.get(name)):
                        continue
                    if name.endswith('Error') and isinstance(n, ast.Call):
                        # Exception construction itself does not mutate persistent state.
                        continue
                    return False
                if isinstance(n.func, ast.Attribute):
                    if n.func.attr in ('get','isoformat','lower','upper','date'):
                        continue
                    if name == 'datetime.now':
                        continue
                    if n.func.attr == 'append' and isinstance(n.func.value, ast.Name) and n.func.value.id in local_containers:
                        continue
                return False
            return True
        finally:
            visiting.remove(fn.name)
    return bool(changed) and all(safe(fn, old_functions.get(name)) for name, fn in changed.items())


def classify_release(repo: Path, base: str, target: str, project_id: str) -> dict:
    """Read both Git trees and the BASE-owned runtime build contract.

    Unknown executable changes are high risk, including a page with new writes.
    Packaging and runtime reachability are separate observations. Unresolved
    executable effects remain high risk. No caller low-risk override exists.
    """
    import ast
    import shlex
    import fnmatch
    before, after = _risk_identity(repo, base), _risk_identity(repo, target)
    snapshots = {}
    for revision in (base, target):
        with tarfile.open(fileobj=io.BytesIO(_risk_git(repo, 'archive', revision))) as archive:
            snapshots[revision] = {m.name: archive.extractfile(m).read() for m in archive if m.isfile()}
    def read(revision, path):
        if path not in snapshots[revision]:
            raise KeyError(path)
        return snapshots[revision][path]
    registry = json.loads(read(base, "02_configs/project_registry.json"))
    projects = [p for p in registry["projects"] if p["project_id"] == project_id]
    if len(projects) != 1 or not projects[0].get("runtime_contract"):
        raise PreReleaseError("registered production build contract required")
    contract_path = projects[0]["runtime_contract"]
    contract = json.loads(read(base, contract_path))
    graphs = {rev: _runtime_graph(snapshots[rev], json.loads(read(rev, contract_path))) for rev in (base, target)}
    active = set(graphs[base]['active_paths']) | set(graphs[target]['active_paths'])
    graph_complete = all(g['complete'] for g in graphs.values())
    build = contract["build"]
    dockerfile = build["dockerfile"]
    sources, unsupported = [], False
    for line in read(base, dockerfile).decode("utf-8").replace("\\\n", " ").splitlines():
        line = line.strip()
        if not re.match(r"(?:COPY|ADD)\s", line, re.I):
            continue
        if not line.startswith("COPY "):
            unsupported = True
            continue
        payload = line[5:].strip()
        try:
            tokens = json.loads(payload) if payload.startswith("[") else shlex.split(payload)
            if len(tokens) < 2 or any(not isinstance(t, str) for t in tokens):
                raise ValueError()
            if any(t.startswith("--") or "$" in t or "\\" in t or ".." in t.split("/") for t in tokens):
                raise ValueError()
            sources.extend(t.removeprefix("./").rstrip("/") for t in tokens[:-1])
        except (ValueError, TypeError):
            unsupported = True
    if not sources:
        unsupported = True
    def included(path):
        return unsupported or any(s in ("", ".") or path == s or path.startswith(s + "/")
                                  or fnmatch.fnmatchcase(path, s) for s in sources)
    paths = sorted(filter(None, _risk_git(repo, "diff", "--no-renames", "--name-only", "-z", base, target).decode("utf-8").split("\0")))
    infrastructure = {contract_path, dockerfile, ".dockerignore", *build["compose_sources"]}
    findings = []
    if any(p in paths for p in infrastructure):
        unsupported = True
    for path in paths:
        reason, risk = "UNKNOWN_EXECUTABLE_CHANGE", True
        # Storage/migration files are never excluded merely by Docker COPY.
        if re.search(r"(?i)(^|/)(migrations?|schema|storage)(/|[_.])|\.(sql|db|sqlite|parquet)$", path) or path.startswith(("01_data/", "06_outputs/", "10_logs/")):
            reason = "STATE_OR_STORAGE_CHANGE"
        elif path in infrastructure or path in build.get('dependency_contracts', []):
            reason = "DEPLOYED_INFRASTRUCTURE_CHANGE"
        elif path.startswith(("07_docs/", "08_tests/")) or path.endswith(".md"):
            reason, risk = "DOCUMENTATION_OR_TEST_ONLY", False
        elif not included(path):
            reason, risk = "OUTSIDE_UNCHANGED_IMAGE_BUILD", False
        elif graph_complete and path not in active:
            reason, risk = "PACKAGED_INACTIVE_CHANGE", False
        elif path.startswith(("09_deploy/", "04_scripts/", "02_configs/", "03_src/agri_research_agent/shared/", "03_src/agri_research_agent/automation/")):
            reason = "RUNTIME_ACTIVE_INFRASTRUCTURE_CHANGE"
        elif path.endswith(".py"):
            try:
                old_ast = ast.dump(ast.parse(read(base, path)), include_attributes=False)
                new_ast = ast.dump(ast.parse(read(target, path)), include_attributes=False)
                if old_ast == new_ast:
                    reason, risk = "NO_EXECUTABLE_AST_CHANGE", False
                elif _stateless_presentation_change(read(base, path), read(target, path), snapshots[target]):
                    reason, risk = "STATELESS_PRESENTATION_EFFECT_DELTA", False
            except (SyntaxError, KeyError, subprocess.SubprocessError):
                pass
        findings.append({"path": path, "reason": reason, "high_risk": risk, "image_input": included(path),
                         "runtime_effect": 'RUNTIME_ACTIVE_CHANGE' if path in active or path in infrastructure else
                         'PACKAGED_INACTIVE_CHANGE' if graph_complete and included(path) else 'UNPROVEN_OR_OUTSIDE_IMAGE'})
    high = unsupported or any(f["high_risk"] for f in findings)
    return {"schema_version": RISK_SCHEMA, "base": before, "target": after, "project_id": project_id,
            "build_contract_sha256": hashlib.sha256(read(base, contract_path)).hexdigest(),
            "build_projection_complete": not unsupported, "findings": findings,
            "RUNTIME_EFFECTIVE_DELTA": {"analysis": "static-import-and-file-reference/1", "base": graphs[base], "target": graphs[target]},
            "RELEASE_RISK_CLASS": "STATEFUL_OR_INFRA" if high else "ROUTINE_STATELESS",
            "ROLLBACK_REHEARSAL_REQUIRED": high}


def _risk_fields(value, fields, name):
    if type(value) is not dict or set(value) != set(fields):
        raise PreReleaseError("invalid " + name + " fields")


def _risk_file(reference: dict, host) -> bytes:
    _risk_fields(reference, ("path", "sha256"), "artifact reference")
    if not isinstance(reference["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]):
        raise PreReleaseError("invalid artifact digest")
    path = Path(reference["path"])
    if not path.is_absolute():
        raise PreReleaseError("artifact path must be absolute")
    raw = host._protected_path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise PreReleaseError("rollback artifact digest differs")
    return raw


def verify_rollback_assets(repo: Path, assets: dict, host) -> dict:
    """Observe retained image and verify hash-bound manifests; no old grant read."""
    _risk_fields(assets, ("commit", "tree", "image_id", "image_location", "registry_digest",
                          "release", "runtime_config", "data_schema", "procedure"), "rollback assets")
    if _risk_identity(repo, assets["commit"])["tree"] != assets["tree"]:
        raise PreReleaseError("rollback tree mismatch")
    if not isinstance(assets["image_id"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", assets["image_id"]):
        raise PreReleaseError("immutable image identity required")
    image = json.loads(host._run_docker(["image", "inspect", assets["image_id"]]))
    if len(image) != 1 or image[0]["Id"] != assets["image_id"]:
        raise PreReleaseError("rollback image unavailable")
    labels = image[0]["Config"]["Labels"]
    if labels.get("org.opencontainers.image.revision") != assets["commit"] or labels.get("market-data.git.tree") != assets["tree"]:
        raise PreReleaseError("rollback image source mismatch")
    location, digest = assets["image_location"], assets["registry_digest"]
    if location == "LOCAL_IMAGE_ONLY":
        if digest is not None:
            raise PreReleaseError("local image cannot claim registry identity")
    elif location == "REGISTRY_IMMUTABLE_IMAGE":
        if not isinstance(digest, str) or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", digest) or digest not in image[0].get("RepoDigests", []):
            raise PreReleaseError("registry digest not observed for image")
    else:
        raise PreReleaseError("unknown image retention mode")
    release = host._json(_risk_file(assets["release"], host))
    if release.get("git_commit", release.get("approved_commit")) != assets["commit"] or release.get("git_tree", release.get("approved_tree")) != assets["tree"]:
        raise PreReleaseError("rollback release manifest identity mismatch")
    if release.get("image_id") != assets["image_id"] or not isinstance(release.get("release_id"), str) or not release["release_id"]:
        raise PreReleaseError("deployed release manifest image/release identity missing")
    config = host._json(_risk_file(assets["runtime_config"], host))
    _risk_fields(config, ("artifact", "namespace", "compose", "environment"), "retained runtime configuration")
    if config["artifact"] != {"commit": assets["commit"], "tree": assets["tree"], "image_id": assets["image_id"]}:
        raise PreReleaseError("runtime configuration artifact mismatch")
    _risk_fields(config["namespace"], ("compose_project", "container_name", "host_ports"), "runtime namespace")
    if any(not config["namespace"][k] for k in config["namespace"]):
        raise PreReleaseError("runtime namespace is incomplete")
    for key in ("compose", "environment"):
        if not _risk_file(config[key], host).strip():
            raise PreReleaseError("retained configuration is empty")
    data = host._json(_risk_file(assets["data_schema"], host))
    _risk_fields(data, ("schema_identity", "assets"), "retained data")
    if not isinstance(data["schema_identity"], str) or not data["schema_identity"] or type(data["assets"]) is not list:
        raise PreReleaseError("data/schema identity missing")
    for ref in data["assets"]:
        _risk_file(ref, host)
    procedure = host._json(_risk_file(assets["procedure"], host))
    _risk_fields(procedure, ("artifact", "fresh_instance_required", "fresh_grant_required", "steps"), "recovery procedure")
    if (procedure["artifact"] != config["artifact"] or procedure["fresh_instance_required"] is not True
            or procedure["fresh_grant_required"] is not True or type(procedure["steps"]) is not list
            or not procedure["steps"] or any(not isinstance(v, str) or not v.strip() for v in procedure["steps"])):
        raise PreReleaseError("fresh recovery procedure missing")
    return {"commit": assets["commit"], "tree": assets["tree"], "image_id": assets["image_id"],
            "image_location": location, "registry_digest": digest,
            "retention_risk": "HOST_LOSS_NOT_COVERED" if location == "LOCAL_IMAGE_ONLY" else "REGISTRY_AVAILABILITY_RECHECK_AT_RECOVERY",
            "ROLLBACK_ASSETS_READY": True}


def evaluate_release_gate(risk: dict, *, assets_ready: bool, candidate_validated: bool,
                          irreversible_state_change: str, compatibility: bool,
                          acceptance_plan_ready: bool, rehearsal_validated: bool,
                          old_grant_expired: bool) -> dict:
    """Pure aggregation of trusted observations, never an authorization API.

    Unknown state/compatibility fails closed. An expired historical grant is
    explicitly informational, not an asset readiness predicate.
    """
    flags = (assets_ready, candidate_validated, compatibility, acceptance_plan_ready,
             rehearsal_validated, old_grant_expired)
    if any(type(v) is not bool for v in flags) or irreversible_state_change not in ("YES", "NO", "UNKNOWN"):
        raise PreReleaseError("invalid release observations")
    if risk.get("RELEASE_RISK_CLASS") not in ("ROUTINE_STATELESS", "STATEFUL_OR_INFRA"):
        raise PreReleaseError("unknown release risk class")
    high = risk["RELEASE_RISK_CLASS"] == "STATEFUL_OR_INFRA" or irreversible_state_change != "NO"
    failures = []
    for ok, code in ((assets_ready, "ROLLBACK_ASSETS_INCOMPLETE"), (candidate_validated, "NEW_CANDIDATE_NOT_VALIDATED"),
                     (compatibility, "DATA_SCHEMA_COMPATIBILITY_UNPROVEN"), (acceptance_plan_ready, "ACCEPTANCE_PLAN_MISSING"),
                     (irreversible_state_change == "NO", "IRREVERSIBLE_STATE_REQUIRES_SEPARATE_MIGRATION_GATE"),
                     (not high or rehearsal_validated, "RECOVERY_REHEARSAL_REQUIRED")):
        if not ok:
            failures.append(code)
    return {"RELEASE_RISK_CLASS": "STATEFUL_OR_INFRA" if high else "ROUTINE_STATELESS",
            "ROLLBACK_ASSETS_READY": assets_ready, "IRREVERSIBLE_STATE_CHANGE": irreversible_state_change,
            "ROLLBACK_REHEARSAL_REQUIRED": high, "OLD_GRANT_EXPIRED": old_grant_expired,
            "EXECUTION_AUTHORIZATION": "REQUIRED_AT_FRESH_INSTANCE_START",
            "PRODUCTION_RELEASE_PREFLIGHT": "FAIL" if failures else "PASS", "failure_codes": failures,
            "production_authorized": False}




def verify_recovery_observation(recovery: dict, host) -> None:
    """Verify sealed runtime observations, including fresh grant at actual start.

    This is retrospective evidence verification, not permission to start now.
    Protected collector provenance remains required for Docker/HTTP observations.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    observations = {name: host._json(_risk_file(ref, host)) for name, ref in recovery["evidence"].items()}
    envelope = observations["grant"]
    parser = _load("03_src/agri_research_agent/shared/production_grant.py", "_release_recovery_grant")
    parser.validate_execution_grant_envelope(envelope)
    grant = envelope["payload"]
    trust = host._json((ROOT / TRUST).read_bytes())
    keys = [k for k in trust["keys"] if k["key_id"] == envelope["key_id"] and k["domain"] == "production"]
    if len(keys) != 1 or envelope["key_id"] in trust["revoked_key_ids"] or grant["grant_id"] in trust["revoked_grant_ids"]:
        raise PreReleaseError("recovery grant signing identity is untrusted")
    canonical = json.dumps(grant, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    from cryptography.exceptions import InvalidSignature
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(keys[0]["public_key_base64"], validate=True)).verify(
            base64.b64decode(envelope["signature"], validate=True), canonical)
    except (ValueError, InvalidSignature) as exc:
        raise PreReleaseError("recovery grant signature is invalid") from exc
    instance = observations["instance"]
    _risk_fields(instance, ("container_id", "image_id", "hostname", "created_at", "started_at"), "recovery instance")
    def timestamp(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise PreReleaseError("recovery timestamp lacks timezone")
        return parsed
    created, started = timestamp(instance["created_at"]), timestamp(instance["started_at"])
    issued, expires = timestamp(grant["issued_at"]), timestamp(grant["expires_at"])
    observed = timestamp(recovery["observed_at"])
    if not created <= issued <= started < expires or not started <= observed < expires or expires - issued > timedelta(hours=1):
        raise PreReleaseError("fresh recovery grant was not valid at start/acceptance")
    if (grant["role"] != "production" or grant["approved_commit"] != recovery["base"]["commit"]
            or grant["approved_tree"] != recovery["base"]["tree"] or grant["image_id"] != recovery["old_image_id"]
            or grant["container_id"] != instance["container_id"] or grant["image_id"] != instance["image_id"]
            or grant["hostname_nonce"] != instance["hostname"]):
        raise PreReleaseError("recovery grant and instance identity differ")
    for name in ("preflight", "health", "consumer", "data_unchanged"):
        result = observations[name]
        # Collector envelopes bind each raw probe output, its exit status and
        # the same runtime identity. Raw output remains a hashed file reference.
        _risk_fields(result, ("container_id", "commit", "tree", "image_id", "exit_code", "status", "raw"), name)
        if (result["container_id"] != instance["container_id"] or result["commit"] != grant["approved_commit"]
                or result["tree"] != grant["approved_tree"] or result["image_id"] != grant["image_id"]
                or type(result["exit_code"]) is not int or result["exit_code"] != 0 or result["status"] != "PASS"
                or not _risk_file(result["raw"], host).strip()):
            raise PreReleaseError("recovery probe failed or identity differs")


def assess_release(request_path: Path, destination: Path) -> dict:
    """Protected, read-only planning entry; no instance or grant mutations.

    The root-owned request is a sealed release-plan input, not a business caller
    approval. Source, assets and candidate signatures are independently observed.
    Recovery evidence remains the separately approved release operator's sealed
    observation, just as controlled-runtime deployment results are today.
    """
    host = _load(HOST, "_release_risk_host")
    host.require_protected_authority_source()
    engine = _load(ENGINE, "_release_risk_engine")
    source_identity = require_source(host, engine)
    request_raw = host._protected_path(request_path, private=True).read_bytes()
    request = host._json(request_raw)
    _risk_fields(request, ("schema_version", "project_id", "current", "previous", "target_commit",
                           "candidate_record", "state_plan", "acceptance_plan", "recovery_evidence", "target_source_root"), "release request")
    if request["schema_version"] != "production-release-request/1":
        raise PreReleaseError("release request schema differs")
    target_source = Path(request["target_source_root"])
    if not target_source.is_absolute():
        raise PreReleaseError("target source must be absolute")
    if destination == target_source or target_source in destination.parents:
        raise PreReleaseError("release report must be outside target source")
    target_identity = require_source(host, engine, source_root=target_source)
    if target_identity[0] != request["target_commit"]:
        raise PreReleaseError("target source identity differs")
    risk = classify_release(ROOT, request["current"]["commit"], request["target_commit"], request["project_id"])
    retained = [verify_rollback_assets(ROOT, request[k], host) for k in ("current", "previous")]
    record = _load(RECORD, "_release_risk_record")
    raw = _risk_file(request["candidate_record"], host)
    candidate = record.verify_record(raw, host._json((ROOT / TRUST).read_bytes()))["evidence"]
    _, manifest, binding = engine.source_contract(target_source, request["project_id"], engine._project(target_source, request["project_id"])["runtime_contract"])
    if candidate["binding"] != binding or binding["commit"] != risk["target"]["commit"] or binding["tree"] != risk["target"]["tree"]:
        raise PreReleaseError("candidate record does not bind target")
    target_image = host._json(host._run_docker(["image", "inspect", candidate["image_id"]]))
    if (len(target_image) != 1 or target_image[0]["Id"] != candidate["image_id"]
            or target_image[0]["Config"]["Labels"].get("org.opencontainers.image.revision") != risk["target"]["commit"]
            or target_image[0]["Config"]["Labels"].get("market-data.git.tree") != risk["target"]["tree"]):
        raise PreReleaseError("validated target image is not available with exact source identity")
    # The sealed state plan is an operator-reviewed fact, never a low-risk switch.
    state = host._json(_risk_file(request["state_plan"], host))
    _risk_fields(state, ("base", "target", "data_schema_sha256", "irreversible", "compatible",
                         "database_migration", "production_data_mutation", "storage_format_change"), "state plan")
    if state["base"] != risk["base"] or state["target"] != risk["target"] or state["data_schema_sha256"] != request["current"]["data_schema"]["sha256"]:
        raise PreReleaseError("state plan identity differs")
    if any(type(state[k]) is not bool for k in ("compatible", "database_migration", "production_data_mutation", "storage_format_change")):
        raise PreReleaseError("state plan requires boolean facts")
    if any(state[k] for k in ("database_migration", "production_data_mutation", "storage_format_change")):
        risk.update(RELEASE_RISK_CLASS="STATEFUL_OR_INFRA", ROLLBACK_REHEARSAL_REQUIRED=True)
    acceptance = host._json(_risk_file(request["acceptance_plan"], host))
    _risk_fields(acceptance, ("target", "image_id", "checks"), "acceptance plan")
    if acceptance["target"] != risk["target"] or acceptance["image_id"] != candidate["image_id"] or type(acceptance["checks"]) is not list or not acceptance["checks"] or any(not isinstance(c, str) or not c.strip() for c in acceptance["checks"]):
        raise PreReleaseError("acceptance plan identity/checks missing")
    rehearsal = False
    if request["recovery_evidence"] is not None:
        recovery = host._json(_risk_file(request["recovery_evidence"], host))
        _risk_fields(recovery, ("schema_version", "base", "target", "old_image_id", "runtime_config_sha256",
                               "data_schema_sha256", "method", "observed_at", "result", "evidence"), "recovery observation")
        if (recovery["schema_version"] != "production-recovery-observation/1" or recovery["base"] != risk["base"]
                or recovery["target"] != risk["target"] or recovery["old_image_id"] != retained[0]["image_id"]
                or recovery["runtime_config_sha256"] != request["current"]["runtime_config"]["sha256"]
                or recovery["data_schema_sha256"] != request["current"]["data_schema"]["sha256"]
                or recovery["method"] not in ("fresh-recovery", "blue-green", "canary")):
            raise PreReleaseError("recovery observation identity differs")
        observed = datetime.fromisoformat(recovery["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None or not timedelta(0) <= datetime.now(timezone.utc) - observed <= timedelta(hours=24):
            raise PreReleaseError("recovery observation expired")
        _risk_fields(recovery["evidence"], ("instance", "grant", "preflight", "health", "consumer", "data_unchanged"), "recovery evidence")
        verify_recovery_observation(recovery, host)
        rehearsal = recovery["result"] == "PASS"
    result = {**risk, **evaluate_release_gate(risk, assets_ready=True, candidate_validated=True,
              irreversible_state_change=state["irreversible"], compatibility=state["compatible"],
              acceptance_plan_ready=True, rehearsal_validated=rehearsal, old_grant_expired=False),
              "OLD_GRANT_EXPIRED": "NOT_READ_NOT_A_GATE", "retained_assets": retained,
              "request_sha256": hashlib.sha256(request_raw).hexdigest(), "candidate_image_id": candidate["image_id"],
              "governance_identity": {"commit": source_identity[0], "tree": source_identity[1]}}
    if (host._protected_path(request_path, private=True).read_bytes() != request_raw
            or require_source(host, engine) != source_identity
            or require_source(host, engine, source_root=target_source) != target_identity):
        raise PreReleaseError("release assessment inputs changed")
    for name in ("current", "previous"):
        verify_rollback_assets(ROOT, request[name], host)
    for name in ("candidate_record", "state_plan", "acceptance_plan"):
        _risk_file(request[name], host)
    if request["recovery_evidence"] is not None:
        _risk_file(request["recovery_evidence"], host)
        verify_recovery_observation(recovery, host)
    _write_new(host, destination, json.dumps(result, sort_keys=True).encode())
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--project")
    parser.add_argument("--release-request", type=Path)
    parser.add_argument("--classify-release", nargs=3, metavar=("BASE", "TARGET", "PROJECT"))
    parser.add_argument("--record-output", type=Path)
    parser.add_argument("--candidate-key", type=Path)
    parser.add_argument("--ttl-seconds", type=int, default=86400)
    parser.add_argument("--production-policy", type=Path)
    parser.add_argument("--container-id")
    parser.add_argument("--report-output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.release_request is not None or args.classify_release is not None:
            if (args.production_policy is not None or args.project is not None or args.container_id is not None
                    or args.record_output is not None or args.candidate_key is not None or args.ttl_seconds != 86400
                    or (args.release_request is not None and args.classify_release is not None)):
                parser.error("release assessment cannot be combined with execution/candidate options")
            if args.classify_release is not None:
                if args.report_output is not None:
                    parser.error("classification prints non-authorizing evidence only")
                result = classify_release(ROOT, *args.classify_release)
            else:
                if args.report_output is None:
                    parser.error("release request requires a new protected report output")
                result = assess_release(args.release_request, args.report_output)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 1 if result.get("PRODUCTION_RELEASE_PREFLIGHT") == "FAIL" else 0
        production = args.production_policy is not None
        if production:
            if (args.container_id is None or args.report_output is None or args.project is not None
                    or args.record_output is not None or args.candidate_key is not None
                    or args.ttl_seconds != 86400):
                parser.error("production mode cannot be combined with candidate options")
            result = revalidate_production(args.container_id, args.production_policy, args.report_output)
            print(json.dumps({"PRODUCTION_REVALIDATION": "PASS", **result}, sort_keys=True))
        else:
            if (args.project is None or args.record_output is None or args.candidate_key is None
                    or args.container_id is not None or args.report_output is not None):
                parser.error("candidate mode requires candidate options")
            result = validate_candidate(args.project, args.record_output, args.candidate_key,
                                        ttl_seconds=args.ttl_seconds)
            print(json.dumps({"CANDIDATE_VALIDATION_RECORD": "PASS", "record_id": result["record_id"],
                              "image_id": result["evidence"]["image_id"]}))
        return 0
    except ValidationBlocked:
        print(json.dumps({"CANDIDATE_VALIDATION_RECORD": "BLOCKED", "reason": "LINUX_BUILDER_UNAVAILABLE"}))
        return 3
    except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        key = "PRODUCTION_RELEASE_PREFLIGHT" if args.release_request is not None or args.classify_release is not None else "CANDIDATE_VALIDATION_RECORD"
        print(json.dumps({key: "FAIL", "reason": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
