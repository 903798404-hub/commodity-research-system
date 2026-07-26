from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import yaml


REPOSITORY = Path(__file__).resolve().parents[2]
RELEASE_DIRECTORY = REPOSITORY / "09_deploy" / "oil_world_release"
if str(RELEASE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(RELEASE_DIRECTORY))

import oil_world_release_contract as contract
import prepare_oil_world_candidate as candidate
import deploy_oil_world_release as deployment
import rollback_oil_world_release as rollback


COMMIT = "a" * 40
TREE = "b" * 40
IMAGE_ID = "sha256:" + "c" * 64
ROLLBACK_IMAGE_ID = "sha256:" + "d" * 64
RELEASE_ID = "oil-20260726-aaaaaaaaaaaa-b01"


def response(status: int, body: str):
    class Response:
        def __init__(self) -> None:
            self.status = status
            self._body = body.encode("utf-8")

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return Response()


def raw_container(name: str, *, candidate_container: bool = False) -> dict:
    image = IMAGE_ID if candidate_container else "sha256:" + ("e" if name == "spread-dashboard" else "f") * 64
    labels = {
        "com.docker.compose.project": "market-data-oil-world-candidate-20260726-aaaaaaaaaaaa-b01" if candidate_container else ("market-data-oil-world" if name == "oil-world-dashboard" else "market-data"),
        "com.docker.compose.service": "oil-world-dashboard" if candidate_container else name,
        "com.docker.compose.project.config_files": str(contract.CANDIDATE_COMPOSE if candidate_container else contract.PRODUCTION_COMPOSE),
        "com.docker.compose.project.working_dir": "/runtime/oil-world/deploy",
    }
    if candidate_container or name == "oil-world-dashboard":
        labels["org.opencontainers.image.revision"] = COMMIT
    return {
        "Id": f"{name}-id",
        "Name": f"/{name}",
        "Image": image,
        "RestartCount": 0,
        "Config": {
            "Image": f"market-data-{name}:immutable" if not candidate_container else f"market-data-oil-world-dashboard:{RELEASE_ID}",
            "Env": [f"MARKET_DATA_GIT_HEAD={COMMIT}"] if candidate_container else [],
            "Labels": labels,
        },
        "HostConfig": {"RestartPolicy": {"Name": "no" if candidate_container else "unless-stopped"}},
        "State": {"Running": True, "Status": "running", "Health": {"Status": "healthy"}},
        "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18081"}]} if candidate_container else {}},
        "Mounts": ([{"Type": "bind", "Source": "/data/oil", "Destination": contract.DATA_CONTAINER_PATH, "RW": False}] if candidate_container else []),
    }


class OilWorldReleaseContractTests(unittest.TestCase):
    def test_compose_entries_are_independent_and_candidate_is_loopback_only(self) -> None:
        production = yaml.safe_load(contract.PRODUCTION_COMPOSE.read_text(encoding="utf-8"))
        candidate_compose = yaml.safe_load(contract.CANDIDATE_COMPOSE.read_text(encoding="utf-8"))

        self.assertEqual(production["name"], contract.COMPOSE_PROJECT)
        self.assertEqual(set(production["services"]), {contract.SERVICE})
        self.assertEqual(set(candidate_compose["services"]), {contract.SERVICE})
        self.assertEqual(candidate_compose["services"][contract.SERVICE]["restart"], "no")
        for compose in (production, candidate_compose):
            mount = compose["services"][contract.SERVICE]["volumes"][0]
            self.assertEqual(mount["target"], contract.DATA_CONTAINER_PATH)
            self.assertIs(mount["read_only"], True)
        self.assertEqual(
            candidate_compose["services"][contract.SERVICE]["ports"],
            ["127.0.0.1:${OIL_WORLD_CANDIDATE_HOST_PORT:?OIL_WORLD_CANDIDATE_HOST_PORT must be set}:80"],
        )
        rendered = contract.CANDIDATE_COMPOSE.read_text(encoding="utf-8")
        self.assertNotIn("spread-dashboard", rendered)
        self.assertNotIn("usda-dashboard", rendered)
        self.assertNotIn("0.0.0.0", rendered)

    def test_image_contract_requires_and_exposes_release_identity_without_copying_data(self) -> None:
        dockerfile = (REPOSITORY / "11_独立应用" / "OilWorld平衡表" / "Dockerfile").read_text(encoding="utf-8")
        for required in ("ARG MARKET_DATA_GIT_HEAD", "ARG MARKET_DATA_GIT_TREE", "ARG RELEASE_ID", "org.opencontainers.image.revision", "MARKET_DATA_GIT_HEAD", "RELEASE.json"):
            self.assertIn(required, dockerfile)
        self.assertIn("test ! -e dist/data/oil_world", dockerfile)
        self.assertNotIn("COPY public/data/oil_world", dockerfile)

        nginx = (REPOSITORY / "11_独立应用" / "OilWorld平衡表" / "deploy" / "nginx.conf").read_text(encoding="utf-8")
        self.assertIn("location = /oil-world/RELEASE.json", nginx)
        self.assertIn("alias /usr/share/nginx/html/oil-world/RELEASE.json;", nginx)
        self.assertIn("default_type application/json;", nginx)
        self.assertLess(nginx.index("location = /oil-world/RELEASE.json"), nginx.index("location /oil-world/"))

    def test_candidate_environment_rejects_formal_ports_and_preserves_read_only_data_root(self) -> None:
        production = contract.validate_production_environment(
            {"OIL_WORLD_IMAGE": "market-data-oil-world-dashboard:immutable", "OIL_WORLD_PORT": "8081", "OIL_WORLD_DATA_ROOT": "/data/oil"}
        )
        values = contract.candidate_environment(production, release_id=RELEASE_ID, candidate_port=18081)
        self.assertEqual(values["OIL_WORLD_DATA_ROOT"], "/data/oil")
        self.assertEqual(values["OIL_WORLD_CANDIDATE_HOST_PORT"], "18081")
        with self.assertRaisesRegex(contract.ContractError, "candidate port"):
            contract.candidate_environment(production, release_id=RELEASE_ID, candidate_port=8081)

    def test_dirty_temporary_git_repository_and_bound_candidate_port_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "candidate-repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True, capture_output=True, text=True)
            (repository / "tracked.txt").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True, capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repository), "-c", "user.name=Oil Test", "-c", "user.email=oil-test@example.invalid", "commit", "-qm", "initial"], check=True, capture_output=True, text=True)
            commit, tree = contract.git_identity(repository)
            self.assertRegex(commit, r"^[0-9a-f]{40}$")
            self.assertRegex(tree, r"^[0-9a-f]{40}$")
            (repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(contract.ContractError, "clean"):
                contract.git_identity(repository)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            port = occupied.getsockname()[1]
            with self.assertRaisesRegex(contract.ContractError, "unavailable"):
                candidate.ensure_candidate_port_available(port)

    def test_release_artifact_is_exclusive_and_manifest_is_verified(self) -> None:
        payload = {
            "schema_version": "1.0.0", "application": contract.APPLICATION, "release_id": RELEASE_ID,
            "git_commit": COMMIT, "git_tree": TREE, "image_ref": "market-data-oil-world-dashboard:oil-20260726-aaaaaaaaaaaa-b01", "image_id": IMAGE_ID, "oci_revision": COMMIT,
            "build_time": "2026-07-26T00:00:00Z", "compose_sha256": "1" * 64,
            "data_identity": {"host_path": "/data/oil", "read_only": True, "file_count": 2, "tree_sha256": "2" * 64},
            "formal_containers": {"before_phase": "before-candidate", "containers": [{"name": name} for name in contract.FORMAL_CONTAINERS]},
            "rollback": {"image_ref": "market-data-oil-world-dashboard:old", "image_id": ROLLBACK_IMAGE_ID, "git_commit": "e" * 40},
            "candidate": {"container_name": "oil-world-dashboard-candidate-20260726-aaaaaaaaaaaa-b01", "project_name": "market-data-oil-world-candidate-20260726-aaaaaaaaaaaa-b01", "port": 18081},
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target, manifest = contract.write_artifact(directory, "release", payload, release_id=RELEASE_ID, git_commit=COMMIT, git_tree=TREE, image_id=IMAGE_ID)
            self.assertTrue(target.exists())
            self.assertTrue(manifest.exists())
            verified, _ = contract.verify_artifact(directory, "release", expected_git_commit=COMMIT, expected_git_tree=TREE, expected_image_id=IMAGE_ID)
            self.assertEqual(verified["release_id"], RELEASE_ID)
            with self.assertRaisesRegex(contract.ContractError, "already exists"):
                contract.write_artifact(directory, "release", payload, release_id=RELEASE_ID, git_commit=COMMIT, git_tree=TREE, image_id=IMAGE_ID)

    def test_execute_orders_snapshot_build_bundle_validation_result_cleanup_and_plan(self) -> None:
        events: list[list[str]] = []
        candidate_name = "oil-world-dashboard-candidate-20260726-aaaaaaaaaaaa-b01"

        def runner(command, **_kwargs):
            events.append(list(command))
            if command[:4] == ["git", "-C", str(REPOSITORY), "status"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[-1:] == ["HEAD"]:
                return subprocess.CompletedProcess(command, 0, COMMIT + "\n", "")
            if command[-1:] == ["HEAD^{tree}"]:
                return subprocess.CompletedProcess(command, 0, TREE + "\n", "")
            if command[:3] == ["docker", "image", "inspect"]:
                image = {"Id": IMAGE_ID, "Config": {"Labels": {"org.opencontainers.image.revision": COMMIT, "org.opencontainers.image.version": RELEASE_ID}}}
                return subprocess.CompletedProcess(command, 0, json.dumps([image]), "")
            if command[:3] == ["docker", "inspect", candidate_name]:
                if any(call[:3] == ["docker", "rm", "--force"] for call in events):
                    return subprocess.CompletedProcess(command, 1, "", "not found")
                raw = raw_container(candidate_name, candidate_container=True)
                raw["Mounts"][0]["Source"] = str(data)
                return subprocess.CompletedProcess(command, 0, json.dumps([raw]), "")
            if command[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(command, 0, json.dumps([raw_container(command[2])]), "")
            return subprocess.CompletedProcess(command, 0, "", "")

        def request(url, timeout):
            bodies = {
                "http://127.0.0.1:18081/oil-world/": '<script src="/oil-world/assets/app.js"></script>',
                "http://127.0.0.1:18081/oil-world/presentation": "presentation",
                "http://127.0.0.1:18081/oil-world/data/oil_world/latest.json": '{"release":"2026-06"}',
                "http://127.0.0.1:18081/oil-world/data/oil_world/releases.json": '{"releases":[{"release":"2026-03","previous_release":null},{"release":"2026-06","previous_release":"2026-03"}]}',
                "http://127.0.0.1:18081/oil-world/data/oil_world/releases/2026-06/index.json": "{}",
                "http://127.0.0.1:18081/oil-world/data/oil_world/comparisons/2026-03_to_2026-06/index.json": "{}",
                "http://127.0.0.1:18081/oil-world/assets/app.js": "ok",
                "http://127.0.0.1:18081/oil-world/RELEASE.json": json.dumps({"git_commit": COMMIT, "git_tree": TREE}),
            }
            return response(200, bodies[url])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            (data / "latest.json").write_text('{"release":"2026-06"}', encoding="utf-8")
            (data / "releases.json").write_text('{"releases":[{"release":"2026-03","previous_release":null},{"release":"2026-06","previous_release":"2026-03"}]}', encoding="utf-8")
            (data / "releases" / "2026-06").mkdir(parents=True)
            (data / "releases" / "2026-06" / "index.json").write_text("{}", encoding="utf-8")
            (data / "comparisons" / "2026-03_to_2026-06").mkdir(parents=True)
            (data / "comparisons" / "2026-03_to_2026-06" / "index.json").write_text("{}", encoding="utf-8")
            env = root / "oil.env"
            env.write_text(f"OIL_WORLD_IMAGE=market-data-oil-world-dashboard:old\nOIL_WORLD_PORT=8081\nOIL_WORLD_DATA_ROOT={data}\n", encoding="utf-8")
            args = Namespace(repository=REPOSITORY, production_compose=contract.PRODUCTION_COMPOSE, production_project_dir=root, production_env=env, output_dir=root / "release", release_id=RELEASE_ID, git_commit=COMMIT, git_tree=TREE, rollback_image_ref="market-data-oil-world-dashboard:old", rollback_image_id=ROLLBACK_IMAGE_ID, rollback_git_commit="e" * 40, formal_image_ref="market-data-oil-world-dashboard:oil-20260726-aaaaaaaaaaaa-b01-formal", candidate_port=18081, timeout_seconds=10, execute=True)
            result = candidate.prepare(args, runner=runner, request=request, sleep=lambda _seconds: None)
            order = [item["name"] for item in result["events"]]
            self.assertEqual(order, ["formal_snapshot", "docker_build", "release_bundle", "compose_up", "validate", "candidate_result", "candidate_container_cleanup", "deployment_plan"])
            self.assertTrue((root / "release" / "candidate_result.json").exists())
            self.assertTrue((root / "release" / "deployment_plan.json").exists())
            self.assertLess(order.index("formal_snapshot"), order.index("docker_build"))
            self.assertLess(order.index("candidate_result"), order.index("candidate_container_cleanup"))
            self.assertLess(order.index("candidate_container_cleanup"), order.index("deployment_plan"))
            self.assertTrue(all("spread-dashboard" not in command for command in events if command and command[0] == "docker" and command[1] == "compose"))
            candidate_environment = contract.parse_environment(root / "release" / "candidate.env")
            self.assertEqual(candidate_environment["OIL_WORLD_IMAGE"], f"market-data-oil-world-dashboard:{RELEASE_ID}")

            docker_calls_before_preview = len([command for command in events if command[:1] == ["docker"]])
            preview = deployment.deploy(
                Namespace(release_dir=root / "release", timeout_seconds=10, dry_run=True),
                runner=runner,
            )
            self.assertEqual(preview["status"], "dry_run")
            self.assertEqual(
                len([command for command in events if command[:1] == ["docker"]]),
                docker_calls_before_preview,
                "deployment preview must not contact Docker",
            )

            def fail_if_called(*_args, **_kwargs):
                self.fail("rollback dry-run must not contact Docker")

            rollback_preview = rollback.rollback(
                Namespace(release_dir=root / "release", timeout_seconds=10, dry_run=True),
                runner=fail_if_called,
            )
            self.assertEqual(rollback_preview["status"], "dry_run")

            before_deploy, _ = contract.verify_artifact(root / "release", "deployment_plan")
            pre_deploy = before_deploy["formal_containers"]
            post_deploy = json.loads(json.dumps(pre_deploy))
            oil = next(item for item in post_deploy["containers"] if item["name"] == "oil-world-dashboard")
            oil.update({
                "image_id": IMAGE_ID,
                "config_image": before_deploy["formal_image_ref"],
                "oci_revision": COMMIT,
                "runtime_git_commit": COMMIT,
            })
            deploy_calls: list[list[str]] = []

            def deploy_runner(command, **_kwargs):
                deploy_calls.append(list(command))
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch.object(deployment, "git_identity", return_value=(COMMIT, TREE)),
                patch.object(deployment, "formal_snapshot", side_effect=[{"before_phase": "pre-deploy", "containers": pre_deploy["containers"]}, {"before_phase": "after-deploy", "containers": post_deploy["containers"]}]),
                patch.object(deployment, "_image_id", return_value=IMAGE_ID),
                patch.object(deployment, "validate_candidate_http", return_value={"http": {"root": 200, "presentation": 200}}),
            ):
                deployed = deployment.deploy(
                    Namespace(release_dir=root / "release", timeout_seconds=10, dry_run=False),
                    runner=deploy_runner,
                )
            self.assertEqual(deployed["status"], "deployed")
            self.assertTrue((root / "release" / "deployment_result.json").exists())
            self.assertEqual(deploy_calls[0][:2], ["docker", "tag"])
            self.assertEqual(deploy_calls[1], before_deploy["execute_argv"])
            self.assertEqual(deploy_calls[2][:3], ["docker", "image", "rm"])
            self.assertEqual(deploy_calls[1][-1], "oil-world-dashboard")
            self.assertTrue(all("spread-dashboard" not in command and "usda-dashboard" not in command for command in deploy_calls))

    def test_validation_failure_cleans_only_the_named_candidate_container(self) -> None:
        events: list[list[str]] = []
        candidate_name = "oil-world-dashboard-candidate-20260726-aaaaaaaaaaaa-b01"

        def runner(command, **_kwargs):
            events.append(list(command))
            if command[:4] == ["git", "-C", str(REPOSITORY), "status"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[-1:] == ["HEAD"]:
                return subprocess.CompletedProcess(command, 0, COMMIT + "\n", "")
            if command[-1:] == ["HEAD^{tree}"]:
                return subprocess.CompletedProcess(command, 0, TREE + "\n", "")
            if command[:3] == ["docker", "image", "inspect"]:
                image = {"Id": IMAGE_ID, "Config": {"Labels": {"org.opencontainers.image.revision": COMMIT, "org.opencontainers.image.version": RELEASE_ID}}}
                return subprocess.CompletedProcess(command, 0, json.dumps([image]), "")
            if command[:3] == ["docker", "inspect", candidate_name]:
                raw = raw_container(candidate_name, candidate_container=True)
                raw["Mounts"][0]["Source"] = str(data)
                return subprocess.CompletedProcess(command, 0, json.dumps([raw]), "")
            if command[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(command, 0, json.dumps([raw_container(command[2])]), "")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            (data / "latest.json").write_text('{"release":"2026-06"}', encoding="utf-8")
            (data / "releases.json").write_text('{"releases":[{"release":"2026-06","previous_release":null}]}', encoding="utf-8")
            (data / "releases" / "2026-06").mkdir(parents=True)
            (data / "releases" / "2026-06" / "index.json").write_text("{}", encoding="utf-8")
            env = root / "oil.env"
            env.write_text(f"OIL_WORLD_IMAGE=market-data-oil-world-dashboard:old\nOIL_WORLD_PORT=8081\nOIL_WORLD_DATA_ROOT={data}\n", encoding="utf-8")
            args = Namespace(repository=REPOSITORY, production_compose=contract.PRODUCTION_COMPOSE, production_project_dir=root, production_env=env, output_dir=root / "release", release_id=RELEASE_ID, git_commit=COMMIT, git_tree=TREE, rollback_image_ref="market-data-oil-world-dashboard:old", rollback_image_id=ROLLBACK_IMAGE_ID, rollback_git_commit="e" * 40, formal_image_ref="market-data-oil-world-dashboard:oil-20260726-aaaaaaaaaaaa-b01-formal", candidate_port=18081, timeout_seconds=10, execute=True)
            with patch.object(candidate, "validate_candidate_http", side_effect=contract.ContractError("forced readiness failure")):
                with self.assertRaisesRegex(contract.ContractError, "forced readiness failure"):
                    candidate.prepare(args, runner=runner)
            self.assertIn(["docker", "rm", "--force", candidate_name], events)
            self.assertFalse((root / "release" / "candidate_result.json").exists())
            self.assertFalse((root / "release" / "deployment_plan.json").exists())
            self.assertFalse(any(command[:3] == ["docker", "image", "rm"] for command in events))
            self.assertFalse(any("spread-dashboard" in command or "usda-dashboard" in command for command in events if command[:2] == ["docker", "compose"]))


if __name__ == "__main__":
    unittest.main()
