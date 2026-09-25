"""S1 host-only, fixed-policy ephemeral Docker task launcher.

This module is not an authorization service for untrusted callers. Only the
protected host launcher may read its policy/approval files or access Docker.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from typing import Callable


SHA = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
REGISTRY_DIGEST = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
BUSINESS_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
APPROVAL_KIND = "LOCAL_DOCKER_IMAGE_ID"
STATUSES = frozenset({"SUCCESS", "LAUNCH_FAILED", "TASK_FAILED", "RUNTIME_FAILED", "POLICY_REJECTED", "IMAGE_NOT_APPROVED"})


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ApprovedImage:
    approved_commit: str
    approved_tree: str
    image_identity_kind: str
    image_identity: str

    @classmethod
    def from_record(cls, record: dict) -> "ApprovedImage":
        if set(record) != {"schema_version", "approved_commit", "approved_tree", "image_identity_kind", "image_identity"}:
            raise PolicyError("approved image record fields differ")
        if record["schema_version"] != "approved-production-image/1":
            raise PolicyError("approved image schema differs")
        result = cls(*(record[key] for key in ("approved_commit", "approved_tree", "image_identity_kind", "image_identity")))
        if not SHA.fullmatch(result.approved_commit) or not SHA.fullmatch(result.approved_tree):
            raise PolicyError("approved Git identity is invalid")
        if result.image_identity_kind == "LOCAL_DOCKER_IMAGE_ID":
            valid = IMAGE_ID.fullmatch(result.image_identity)
        elif result.image_identity_kind == "REGISTRY_DIGEST":
            valid = REGISTRY_DIGEST.fullmatch(result.image_identity)
        else:
            raise PolicyError("unknown image identity kind")
        if not valid:
            raise PolicyError("image identity does not match its kind")
        return result


@dataclass(frozen=True)
class Mount:
    source: Path
    target: str
    read_only: bool


@dataclass(frozen=True)
class TaskPolicy:
    task_type: str
    image_identity_kind: str
    image_identity: str
    command: tuple[str, ...]
    uid: int
    gid: int
    mounts: tuple[Mount, ...]
    secret: Mount | None
    network: str
    tmpfs: tuple[str, ...] = ()

    def validate(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.task_type):
            raise PolicyError("invalid registered task type")
        if type(self.uid) is not int or type(self.gid) is not int or self.uid <= 0 or self.gid <= 0:
            raise PolicyError("task must have a non-root numeric identity")
        if len(self.command) < 2 or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in self.command):
            raise PolicyError("fixed entrypoint and explicit command argument required")
        if not self.network or not re.fullmatch(r"[a-zA-Z0-9_.-]+", self.network):
            raise PolicyError("fixed network required")
        if self.image_identity_kind == "LOCAL_DOCKER_IMAGE_ID":
            valid = IMAGE_ID.fullmatch(self.image_identity)
        elif self.image_identity_kind == "REGISTRY_DIGEST":
            valid = REGISTRY_DIGEST.fullmatch(self.image_identity)
        else:
            valid = None
        if not valid:
            raise PolicyError("fixed immutable image required")
        targets = set()
        for mount in (*self.mounts, *((self.secret,) if self.secret else ())):
            if (not mount.source.is_absolute() or not mount.target.startswith("/")
                    or ".." in Path(mount.target).parts or "," in str(mount.source)
                    or "," in mount.target or "\x00" in str(mount.source) + mount.target):
                raise PolicyError("mount paths must be explicit absolute paths")
            if mount.target in targets or mount.target in {"/", "/runtime", "/app", "/var/run/docker.sock"}:
                raise PolicyError("broad or duplicate mount")
            if mount.source.as_posix().endswith("/docker.sock") or mount.source.as_posix() in {"/", "/runtime"}:
                raise PolicyError("Docker socket or broad host root prohibited")
            targets.add(mount.target)
        if self.secret and (not self.secret.read_only or self.secret.target != "/run/secrets/tankan.env"):
            raise PolicyError("secret must be a fixed read-only file")
        if any(a != b and (a.startswith(b + "/") or b.startswith(a + "/")) for a in targets for b in targets):
            raise PolicyError("nested mounts are prohibited")
        for spec in self.tmpfs:
            if not re.fullmatch(r"/[a-zA-Z0-9_/-]+:rw,size=[1-9][0-9]*[kKmM]", spec):
                raise PolicyError("tmpfs must be bounded")
            tmp_target = spec.split(":", 1)[0]
            if any(tmp_target == target or tmp_target.startswith(target + "/") or target.startswith(tmp_target + "/")
                   for target in targets):
                raise PolicyError("tmpfs cannot overlap a bind mount")


def read_host_record(path: Path) -> ApprovedImage:
    """Read the protected promotion output; never create or update it here."""
    if path.is_symlink() or not path.is_file():
        raise PolicyError("approval must be a regular non-symlink file")
    if os.name == "posix":
        st = path.stat()
        parent = path.parent.stat()
        if st.st_uid != 0 or st.st_mode & 0o022 or parent.st_uid != 0 or parent.st_mode & 0o022:
            raise PolicyError("approval file and parent must be root-controlled")
    return ApprovedImage.from_record(json.loads(path.read_text(encoding="utf-8")))


def read_host_policies(path: Path) -> dict[str, TaskPolicy]:
    if path.is_symlink() or not path.is_file():
        raise PolicyError("policy must be a regular non-symlink file")
    if os.name == "posix":
        st, parent = path.stat(), path.parent.stat()
        if st.st_uid != 0 or st.st_mode & 0o022 or parent.st_uid != 0 or parent.st_mode & 0o022:
            raise PolicyError("policy file and parent must be root-controlled")
    document = json.loads(path.read_text(encoding="utf-8"))
    if set(document) != {"schema_version", "tasks"} or document["schema_version"] != "host-task-policies/1":
        raise PolicyError("host policy schema differs")
    policies = {}
    for name, row in document["tasks"].items():
        if type(row) is not dict:
            raise PolicyError("task policy must be an object")
        if set(row) != {"image_identity_kind", "image_identity", "command", "uid", "gid", "mounts", "secret", "network", "tmpfs"}:
            raise PolicyError("task policy fields differ")
        def mount(value: dict) -> Mount:
            if (type(value) is not dict or set(value) != {"source", "target", "read_only"}
                    or type(value["read_only"]) is not bool or type(value["source"]) is not str
                    or type(value["target"]) is not str):
                raise PolicyError("mount fields differ")
            return Mount(Path(value["source"]), value["target"], value["read_only"])
        if type(row["command"]) is not list or type(row["mounts"]) is not list or type(row["tmpfs"]) is not list:
            raise PolicyError("policy sequences must be lists")
        policy = TaskPolicy(name, row["image_identity_kind"], row["image_identity"],
                            tuple(row["command"]), row["uid"], row["gid"],
                            tuple(mount(value) for value in row["mounts"]),
                            mount(row["secret"]) if row["secret"] is not None else None,
                            row["network"], tuple(row["tmpfs"]))
        policy.validate()
        policies[name] = policy
    return policies


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _docker(args: list[str]) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=300)
    return result.stdout.strip()


class HostLauncher:
    """Instantiate only in the Docker-authorized host process.

    Business callers have exactly run_task(task_type, business_identity).
    Approval, policies, receipts and Docker transport are host-owned inputs.
    """

    def __init__(self, *, approval_path: Path, policies: dict[str, TaskPolicy], receipt_root: Path,
                 docker: Callable[[list[str]], str] = _docker):
        self._approval_path = approval_path
        self._policies = dict(policies)
        self._receipt_root = receipt_root
        self._docker = docker

    def _inspect_image(self, approved: ApprovedImage) -> None:
        raw = json.loads(self._docker(["image", "inspect", approved.image_identity]))
        if not isinstance(raw, list) or len(raw) != 1:
            raise PolicyError("approved image inspect failed")
        image = raw[0]
        if approved.image_identity_kind == "LOCAL_DOCKER_IMAGE_ID":
            if image.get("Id") != approved.image_identity:
                raise PolicyError("local Image ID differs")
        elif approved.image_identity not in image.get("RepoDigests", []):
            raise PolicyError("registry digest differs")
        labels = (image.get("Config") or {}).get("Labels") or {}
        if labels.get("org.opencontainers.image.revision") != approved.approved_commit or labels.get("market-data.git.tree") != approved.approved_tree:
            raise PolicyError("image Commit/Tree labels differ")
        if (image.get("Config") or {}).get("Volumes"):
            raise PolicyError("implicit image volumes are prohibited")

    def _verify_created_container(self, container_id: str, approved: ApprovedImage,
                                  policy: TaskPolicy) -> None:
        observed = json.loads(self._docker(["inspect", container_id]))[0]
        if observed.get("Image") != self._docker(["image", "inspect", "--format", "{{.Id}}", approved.image_identity]):
            raise RuntimeError("container image differs from approved image")
        host = observed.get("HostConfig") or {}
        config = observed.get("Config") or {}
        if (host.get("ReadonlyRootfs") is not True or "ALL" not in host.get("CapDrop", [])
                or "no-new-privileges" not in host.get("SecurityOpt", [])
                or config.get("User") != f"{policy.uid}:{policy.gid}"
                or host.get("NetworkMode") != policy.network
                or config.get("Entrypoint") != [policy.command[0]]
                or config.get("Cmd") != list(policy.command[1:])
                or (host.get("Tmpfs") or {}) != dict(spec.split(":", 1) for spec in policy.tmpfs)):
            raise RuntimeError("created container security or command differs")
        expected = {(str(m.source), m.target, not m.read_only)
                    for m in (*policy.mounts, *((policy.secret,) if policy.secret else ()))}
        actual = {(m.get("Source"), m.get("Destination"), m.get("RW")) for m in observed.get("Mounts", [])}
        if actual != expected:
            raise RuntimeError("created container mounts differ from fixed policy")

    def _write_receipt(self, receipt: dict) -> None:
        self._receipt_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._receipt_root.is_symlink():
            raise PolicyError("receipt directory cannot be a symlink")
        if os.name == "posix":
            st = self._receipt_root.stat()
            if st.st_uid != os.geteuid() or st.st_mode & 0o022:
                raise PolicyError("receipt directory must be controlled by the host launcher")
        destination = self._receipt_root / (receipt["task_id"] + ".json")
        temporary = self._receipt_root / ("." + receipt["task_id"] + ".tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)

    def run_task(self, task_type: str, business_identity: dict[str, str]) -> dict:
        task_id = uuid.uuid4().hex
        receipt = dict(schema_version="task-execution-receipt/1", task_id=task_id,
                       task_type=None, business_identity=None,
                       started_at=_now(), finished_at=None, approved_commit=None,
                       approved_tree=None, image_identity_kind=None, image_identity=None,
                       container_id=None, runtime_uid=None, runtime_gid=None,
                       exit_code=None, input_identity=None, output_identity=None,
                       result_status="POLICY_REJECTED", error_category=None)
        container_id = None
        try:
            if type(business_identity) is not dict or set(business_identity) != {"business_date", "session"}:
                raise PolicyError("business identity must contain only date and session")
            if not isinstance(business_identity["business_date"], str) or not BUSINESS_DATE.fullmatch(business_identity["business_date"]):
                raise PolicyError("invalid business date")
            datetime.strptime(business_identity["business_date"], "%Y-%m-%d")
            if business_identity["session"] not in ("AM", "PM"):
                raise PolicyError("invalid session")
            receipt["business_identity"] = dict(business_identity)
            policy = self._policies.get(task_type)
            if policy is None:
                raise PolicyError("unregistered task type")
            policy.validate()
            receipt["task_type"] = policy.task_type
            approved = read_host_record(self._approval_path)
            receipt.update(approved_commit=approved.approved_commit, approved_tree=approved.approved_tree,
                           image_identity_kind=approved.image_identity_kind, image_identity=approved.image_identity,
                           runtime_uid=policy.uid, runtime_gid=policy.gid,
                           input_identity=_json_digest(business_identity))
            if (policy.image_identity_kind, policy.image_identity) != (approved.image_identity_kind, approved.image_identity):
                receipt["result_status"] = "IMAGE_NOT_APPROVED"
                raise PolicyError("policy image is not the current approved image")
            receipt["result_status"] = "IMAGE_NOT_APPROVED"
            self._inspect_image(approved)
            args = ["create", "--pull=never", "--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges", "--user", f"{policy.uid}:{policy.gid}",
                    "--network", policy.network, "--env", f"BUSINESS_DATE={business_identity['business_date']}",
                    "--env", f"SESSION={business_identity['session']}"]
            for mount in (*policy.mounts, *((policy.secret,) if policy.secret else ())):
                if not mount.source.exists():
                    raise PolicyError("configured mount source absent")
                if mount.source.is_symlink():
                    raise PolicyError("configured mount cannot be a symlink")
                if mount is policy.secret and not mount.source.is_file():
                    raise PolicyError("secret source must be a file")
                spec = f"type=bind,src={mount.source},dst={mount.target}"
                if mount.read_only:
                    spec += ",readonly"
                args.extend(["--mount", spec])
            for spec in policy.tmpfs:
                args.extend(["--tmpfs", spec])
            args.extend(["--entrypoint", policy.command[0], approved.image_identity, *policy.command[1:]])
            receipt["result_status"] = "LAUNCH_FAILED"
            container_id = self._docker(args).strip()
            if not CONTAINER_ID.fullmatch(container_id):
                raise RuntimeError("Docker returned invalid container ID")
            receipt["container_id"] = container_id
            self._verify_created_container(container_id, approved, policy)
            receipt["result_status"] = "RUNTIME_FAILED"
            self._docker(["start", container_id])
            receipt["exit_code"] = int(self._docker(["wait", container_id]))
            final_state = json.loads(self._docker(["inspect", container_id]))[0].get("State") or {}
            if final_state.get("OOMKilled") or final_state.get("Error"):
                receipt["result_status"] = "RUNTIME_FAILED"
                receipt["error_category"] = "ContainerRuntimeError"
            else:
                receipt["result_status"] = "SUCCESS" if receipt["exit_code"] == 0 else "TASK_FAILED"
        except PolicyError as exc:
            receipt["error_category"] = type(exc).__name__
        except subprocess.CalledProcessError:
            receipt["error_category"] = "DockerCommandFailed"
            if container_id and receipt["result_status"] != "LAUNCH_FAILED":
                receipt["result_status"] = "RUNTIME_FAILED"
        except Exception as exc:
            receipt["error_category"] = type(exc).__name__
            if container_id:
                receipt["result_status"] = "RUNTIME_FAILED"
        finally:
            if container_id and CONTAINER_ID.fullmatch(container_id):
                try:
                    self._docker(["rm", "-f", container_id])
                except Exception:
                    receipt["result_status"] = "RUNTIME_FAILED"
                    receipt["error_category"] = "ContainerCleanupFailed"
            receipt["finished_at"] = _now()
            self._write_receipt(receipt)
        return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("task_type", choices=("soybean_capture",))
    parser.add_argument("--business-date", required=True)
    parser.add_argument("--session", choices=("AM", "PM"), required=True)
    args = parser.parse_args(argv)
    launcher = HostLauncher(
        approval_path=Path("/etc/market-data/task-runtime/approved-image.json"),
        policies=read_host_policies(Path("/etc/market-data/task-runtime/policies.json")),
        receipt_root=Path("/var/lib/market-data/task-runtime/receipts"),
    )
    receipt = launcher.run_task(args.task_type, {"business_date": args.business_date, "session": args.session})
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["result_status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
