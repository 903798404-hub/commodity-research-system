"""Bounded application acceptance and sealing while the candidate is alive.

Only committed hooks are executed. No CLI accepts caller-authored PASS evidence.
The Docker engine owns create/grant/start/cleanup; this collector cannot deploy.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


def now():
    return datetime.now(timezone.utc).isoformat()


class Lifecycle:
    def __init__(self, engine, root, project, directory, seal):
        self.engine, self.root, self.directory, self.seal = engine, root, directory, seal
        self.codec = engine._load(root / "09_deploy/runtime_identity/candidate_evidence.py", "_candidate_bundle")
        plan_path = f"02_configs/runtime_acceptance/{project['project_id']}.json"
        self.plan = self.codec.strict_json(engine._exact_source(root, plan_path).read_bytes())
        self.codec.fields(self.plan, "schema_version port timeout_seconds hooks")
        if self.plan["schema_version"] != "candidate-acceptance-plan/1" or type(self.plan["port"]) is not int or not 1024 <= self.plan["port"] <= 65535:
            raise ValueError("invalid candidate endpoint plan")
        if type(self.plan["timeout_seconds"]) is not int or not 1 <= self.plan["timeout_seconds"] <= 600:
            raise ValueError("invalid acceptance deadline")
        if type(self.plan["hooks"]) is not list or not self.plan["hooks"]:
            raise ValueError("acceptance hooks required")
        categories = set()
        for hook in self.plan["hooks"]:
            self.codec.fields(hook, "category script config")
            if hook["category"] not in {"browser", "fixture", "consumer"} or hook["category"] in categories:
                raise ValueError("invalid/duplicate acceptance category")
            categories.add(hook["category"])
            engine._exact_source(root, hook["script"])
            engine._exact_source(root, hook["config"])
        if categories != {"browser", "fixture", "consumer"}:
            raise ValueError("required application acceptance missing")
        self.directory.mkdir(mode=0o700)  # exclusive, outside source, protected parent
        self.files, self.identity, self.sealed = [], None, False
        self.production_before = self.containers()
        self.put("acceptance-plan.json", self.plan, "plan")

    def containers(self):
        ids = self.engine._docker("ps", "-q", "--no-trunc").stdout.decode().split()
        return {i: {**{k: c[k] for k in ("Id", "Image", "Created")},
                    "StartedAt": c["State"]["StartedAt"], "Running": c["State"]["Running"]}
                for i in ids for c in [self.engine.inspect_one("container", i)]}

    def put(self, name, value, kind):
        raw = self.codec.canonical(value)
        self.engine._write_new(self.directory / name, raw)
        self.files.append(dict(name=name, sha256=self.codec.digest(raw), type=kind))

    def configure(self, document):
        # Internal bridge and loopback-only listener; no production network.
        service = next(iter(document["services"].values()))
        service.pop("network_mode")
        service["ports"] = [{"target": 8501, "published": str(self.plan["port"]), "host_ip": "127.0.0.1", "protocol": "tcp"}]
        document["networks"] = {"default": {"internal": True}}
        return document

    def data_identity(self, mounts):
        result = {}
        for mount in mounts:
            if not mount["read_only"]:
                continue
            root = Path(mount["source"])
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise ValueError("candidate data symlink")
                if path.is_file():
                    result[mount["target"] + "/" + path.relative_to(root).as_posix()] = self.codec.digest(path.read_bytes())
        return result

    def created(self, container, image, release, scope):
        self.identity = dict(commit=release["git_commit"], tree=release["git_tree"],
                             image_id=image["Id"], release_id=release["release_id"],
                             container_id=container["Id"], nonce=container["Config"]["Hostname"])
        self.mounts = scope["mounts"]
        self.data_before = self.data_identity(self.mounts)
        self.put("instance.json", self.identity, "instance")
        self.put("image.json", dict(image_id=image["Id"], labels=image["Config"]["Labels"],
                                    repo_digests=image.get("RepoDigests", [])), "build")

    def authorized(self, grant_raw):
        # Signature, nonce, image and unstarted instance were verified by issuer.
        grant = self.codec.strict_json(grant_raw)["payload"]
        for key, expected in (("approved_commit", self.identity["commit"]), ("approved_tree", self.identity["tree"]),
                              ("image_id", self.identity["image_id"]), ("container_id", self.identity["container_id"]),
                              ("hostname_nonce", self.identity["nonce"]), ("role", "candidate_validation")):
            if grant[key] != expected:
                raise ValueError("fresh candidate grant identity mismatch")
        if not datetime.fromisoformat(grant["issued_at"]) <= datetime.now(timezone.utc) < datetime.fromisoformat(grant["expires_at"]):
            raise ValueError("candidate grant is not valid at start")
        self.grant = {k: grant[k] for k in ("grant_id", "issued_at", "expires_at")}
        self.grant["sha256"] = self.codec.digest(grant_raw)
        self.put("grant.json", self.codec.strict_json(grant_raw), "grant")
        self.started_at = now()

    def started(self, container):
        if container["Id"] != self.identity["container_id"]:
            raise ValueError("started another candidate instance")
        started = container["State"]["StartedAt"]
        if not datetime.fromisoformat(self.grant["issued_at"]) <= datetime.fromisoformat(started.replace("Z", "+00:00")) < datetime.fromisoformat(self.grant["expires_at"]):
            raise ValueError("grant not valid at actual start")
        self.started_at = started

    def finish(self, evidence):
        deadline = time.monotonic() + self.plan["timeout_seconds"]
        endpoint = f"http://127.0.0.1:{self.plan['port']}"
        self.put("runtime.json", evidence, "runtime")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while True:
            try:
                with opener.open(endpoint + "/_stcore/health", timeout=5) as response:
                    if response.status != 200 or response.read(1024).strip() != b"ok":
                        raise ValueError("candidate health failed")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("candidate health deadline")
                time.sleep(0.5)
        self.put("health.json", dict(identity=self.identity, status="PASS", timestamp=now()), "health")
        for hook in self.plan["hooks"]:
            path = self.engine._exact_source(self.root, hook["script"])
            request = dict(identity=self.identity, endpoint=endpoint, timestamp=now(),
                           context="CANDIDATE_FIXTURE", source_root=str(self.root), category=hook["category"],
                           config=self.codec.strict_json(self.engine._exact_source(self.root, hook["config"]).read_bytes()))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("application acceptance deadline")
            completed = subprocess.run([sys.executable, "-I", "-B", str(path)], input=self.codec.canonical(request),
                                       capture_output=True, timeout=remaining, cwd=self.root,
                                       env={k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}})
            result = self.codec.strict_json(completed.stdout)
            self.put(hook["category"] + ".json", result, hook["category"])
            self.codec.fields(result, "identity status context timestamp observations")
            if (completed.returncode or result["status"] != "PASS" or result["identity"] != self.identity or
                    result["context"] != "CANDIDATE_FIXTURE" or not result["observations"]):
                raise ValueError("required application acceptance failed: " + hook["category"])
            observed_at = datetime.fromisoformat(result["timestamp"].replace("Z", "+00:00"))
            if not datetime.fromisoformat(self.started_at.replace("Z", "+00:00")) <= observed_at <= datetime.now(timezone.utc):
                raise ValueError("stale application acceptance timestamp")
        data_after = self.data_identity(self.mounts)
        after = self.containers()
        candidate = self.engine.inspect_one("container", self.identity["container_id"])
        if (candidate["Image"] != self.identity["image_id"] or candidate["Config"]["Hostname"] != self.identity["nonce"]
                or candidate["State"]["Running"] is not True):
            raise ValueError("candidate changed during application acceptance")
        if self.data_before != data_after or any(after.get(k) != v for k, v in self.production_before.items()):
            raise ValueError("production/container/data identity changed")
        self.put("data.json", dict(before=self.data_before, after=data_after,
                                  production_before=self.production_before,
                                  production_after={k: after[k] for k in self.production_before}), "data")
        bundle = dict(schema_version="candidate-evidence-bundle/1", identity=self.identity,
                      grant=self.grant, started_at=self.started_at, completed_at=now(),
                      results={k: "PASS" for k in ("runtime", "health", "application", "browser", "fixture", "consumer", "production_data_unchanged")},
                      files=self.files)
        raw = self.codec.canonical(bundle)
        self.engine._write_new(self.directory / "bundle.json", raw)
        self.seal(evidence, self.identity["release_id"], self.codec.digest(raw), self.directory)
        self.sealed = True

    def before_cleanup(self):
        # Also preserved for timeout, hook/seal failure and partial create failure.
        self.engine._write_new(self.directory / "lifecycle-result.json", self.codec.canonical(
            dict(status="PASS" if self.sealed else "FAIL", identity=self.identity,
                 sealed=self.sealed, timestamp=now(),
                 failure_type=type(sys.exc_info()[1]).__name__ if sys.exc_info()[1] else None)))

    def after_cleanup(self, removed):
        if not (self.directory / "lifecycle-result.json").is_file():
            raise ValueError("cleanup before seal/failure evidence is forbidden")
        self.engine._write_new(self.directory / "cleanup.json", self.codec.canonical(
            dict(status="PASS" if removed else "FAIL", identity=self.identity,
                 image_retained=True, timestamp=now())))
        if not removed:
            raise ValueError("candidate cleanup failed")
