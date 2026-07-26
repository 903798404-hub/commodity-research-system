from __future__ import annotations

import re
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml


REPOSITORY = Path(__file__).resolve().parents[2]
RELEASE_DIRECTORY = REPOSITORY / "09_deploy" / "usda_release"
PRODUCTION_COMPOSE = RELEASE_DIRECTORY / "compose.production.yml"
CANDIDATE_COMPOSE = RELEASE_DIRECTORY / "compose.candidate.yml"
ENTRYPOINT = RELEASE_DIRECTORY / "usda_compose.sh"
ENV_EXAMPLE = RELEASE_DIRECTORY / "usda-production.env.example"
CAPTURE = RELEASE_DIRECTORY / "capture_usda_runtime.py"
CANDIDATE_RUNNER = RELEASE_DIRECTORY / "run_usda_candidate.py"
SEALER = RELEASE_DIRECTORY / "seal_usda_migration.py"

SPREAD_ONLY_VARIABLES = {
    "SPREAD_IMAGE",
    "MARKET_DATA_GIT_HEAD",
    "WEATHER_DATA_DIR",
    "WEATHER_RUNTIME_CURRENT_DIR",
    "USDA_DASHBOARD_URL",
    "OIL_WORLD_DASHBOARD_URL",
}


def compose_variables(text: str) -> set[str]:
    return set(re.findall(r"\$\{([A-Z0-9_]+)(?::[^}]*)?\}", text))


def load_module(name: str, path: Path):
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


CAPTURE_MODULE = load_module("capture_usda_runtime", CAPTURE)
CANDIDATE_MODULE = load_module("run_usda_candidate", CANDIDATE_RUNNER)
SEAL_MODULE = load_module("seal_usda_migration", SEALER)


class UsdaComposeIsolationTests(unittest.TestCase):
    def test_production_compose_is_usda_only_and_does_not_import_root_compose(self) -> None:
        text = PRODUCTION_COMPOSE.read_text(encoding="utf-8")
        compose = yaml.safe_load(text)

        self.assertEqual(compose["name"], "market-data")
        self.assertEqual(set(compose["services"]), {"usda-dashboard"})
        self.assertFalse(SPREAD_ONLY_VARIABLES & compose_variables(text))
        self.assertNotIn("docker-compose.yml", text)
        self.assertNotIn("spread-dashboard", text)
        self.assertNotIn("oil-world-dashboard", text)

    def test_production_contract_matches_current_usda_runtime_shape(self) -> None:
        service = yaml.safe_load(PRODUCTION_COMPOSE.read_text(encoding="utf-8"))["services"]["usda-dashboard"]

        self.assertEqual(
            service["image"],
            "${USDA_IMAGE:?USDA_IMAGE must be set to an immutable release tag}",
        )
        self.assertEqual(service["container_name"], "usda-dashboard")
        self.assertEqual(service["command"], ["nginx", "-g", "daemon off;"])
        self.assertEqual(service["working_dir"], "/")
        self.assertEqual(service["ports"], ["${USDA_HOST_PORT:?USDA_HOST_PORT must be set}:80"])
        self.assertEqual(service["restart"], "unless-stopped")
        self.assertEqual(service["networks"], ["usda-network"])
        self.assertNotIn("build", service)
        self.assertNotIn("volumes", service)
        self.assertNotIn("environment", service)
        self.assertNotIn("healthcheck", service)

    def test_production_environment_contract_requires_only_usda_values(self) -> None:
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        names = {
            line.split("=", 1)[0]
            for line in text.splitlines()
            if line and not line.startswith("#")
        }

        self.assertEqual(names, {"USDA_IMAGE", "USDA_HOST_PORT", "USDA_NETWORK_NAME"})
        self.assertFalse(SPREAD_ONLY_VARIABLES & names)
        self.assertEqual(
            compose_variables(PRODUCTION_COMPOSE.read_text(encoding="utf-8")),
            {"USDA_IMAGE", "USDA_HOST_PORT", "USDA_NETWORK_NAME"},
        )

    def test_candidate_is_usda_only_localhost_and_never_reuses_formal_resources(self) -> None:
        text = CANDIDATE_COMPOSE.read_text(encoding="utf-8")
        compose = yaml.safe_load(text)
        service = compose["services"]["usda-dashboard"]

        self.assertEqual(compose["name"], "market-data-usda-candidate")
        self.assertEqual(set(compose["services"]), {"usda-dashboard"})
        self.assertEqual(
            service["container_name"],
            "${USDA_CANDIDATE_CONTAINER_NAME:?USDA_CANDIDATE_CONTAINER_NAME must be set}",
        )
        self.assertEqual(
            service["ports"],
            ["127.0.0.1:${USDA_CANDIDATE_HOST_PORT:?USDA_CANDIDATE_HOST_PORT must be set}:80"],
        )
        self.assertEqual(service["restart"], "no")
        self.assertNotIn("build", service)
        self.assertNotIn("depends_on", service)
        self.assertNotIn("volumes", service)
        self.assertFalse(SPREAD_ONLY_VARIABLES & compose_variables(text))
        self.assertNotIn("0.0.0.0", text)
        self.assertNotIn("8080:80", text)
        self.assertNotIn("spread-dashboard", text)
        self.assertNotIn("oil-world-dashboard", text)

    def test_root_compose_marks_usda_definition_as_legacy_only(self) -> None:
        root = (REPOSITORY / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("Historical compatibility definition only", root)
        self.assertIn("09_deploy/usda_release/compose.production.yml", root)

    def test_controlled_entrypoint_uses_only_independent_compose_files(self) -> None:
        text = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn('production_compose="${script_dir}/compose.production.yml"', text)
        self.assertIn('candidate_compose="${script_dir}/compose.candidate.yml"', text)
        self.assertNotIn("docker-compose.yml", text)
        self.assertNotIn("docker compose down", text)
        self.assertNotIn("docker compose build", text)
        self.assertNotIn("--build", text)
        for flag in ("--no-build", "--pull never", "--no-deps", "--force-recreate"):
            self.assertIn(flag, text)
        self.assertIn("candidate-remove", text)
        self.assertIn("rm --stop --force usda-dashboard", text)

    def test_candidate_environment_rejects_spread_variables_and_formal_ports(self) -> None:
        valid = {
            "USDA_IMAGE": "market-data-usda-dashboard:abc123",
            "USDA_NETWORK_NAME": "market-data_default",
            "USDA_CANDIDATE_CONTAINER_NAME": "usda-candidate-abc123",
            "USDA_CANDIDATE_HOST_PORT": "18080",
        }
        self.assertEqual(
            CANDIDATE_MODULE.validate_candidate_environment(valid),
            valid,
        )
        with self.assertRaisesRegex(ValueError, "non-USDA"):
            CANDIDATE_MODULE.validate_candidate_environment(
                {**valid, "WEATHER_DATA_DIR": "/app/runtime/weather/current"}
            )
        with self.assertRaisesRegex(ValueError, "candidate port"):
            CANDIDATE_MODULE.validate_candidate_environment(
                {**valid, "USDA_CANDIDATE_HOST_PORT": "8080"}
            )

    def test_candidate_commands_are_usda_only_and_never_build(self) -> None:
        values = {
            "USDA_IMAGE": "market-data-usda-dashboard:abc123",
            "USDA_CANDIDATE_CONTAINER_NAME": "usda-candidate-abc123",
            "USDA_CANDIDATE_HOST_PORT": "18080",
        }
        command = CANDIDATE_MODULE.compose_command(values, "up")
        self.assertEqual(command[:4], ["docker", "compose", "--project-name", "market-data-usda-candidate"])
        self.assertEqual(command[-1], "usda-dashboard")
        self.assertIn("--no-build", command)
        self.assertIn("--pull", command)
        self.assertNotIn("spread-dashboard", command)
        self.assertNotIn("oil-world-dashboard", command)
        self.assertEqual(
            CANDIDATE_MODULE.compose_command(values, "remove"),
            ["docker", "rm", "-f", "usda-candidate-abc123"],
        )

    def test_candidate_readiness_requires_html_static_asset_and_data_index(self) -> None:
        class Response:
            def __init__(self, status: int, body: str = "") -> None:
                self.status = status
                self._body = body.encode("utf-8")

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        responses = {
            "http://127.0.0.1:18080/usda/": Response(200, '<script src="/usda/assets/main.js"></script>'),
            "http://127.0.0.1:18080/usda/data/index.json": Response(200, "{}"),
            "http://127.0.0.1:18080/usda/assets/main.js": Response(200, "console.log('ok')"),
        }
        checks = CANDIDATE_MODULE.wait_for_candidate(
            port=18080,
            timeout_seconds=10,
            request=lambda url, timeout: responses[url],
        )
        self.assertEqual([item.get("http_status") for item in checks], [200, 200, 200])
        self.assertEqual(checks[-1]["kind"], "static_asset")

    def test_candidate_log_summary_rejects_runtime_errors_without_storing_log_text(self) -> None:
        def fake_runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, "Traceback: no", "")

        with self.assertRaisesRegex(RuntimeError, "failure markers"):
            CANDIDATE_MODULE.collect_log_summary("candidate", fake_runner)

        summary = CANDIDATE_MODULE.collect_log_summary(
            "candidate",
            lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "normal startup", ""),
        )
        self.assertEqual(summary["failure_markers"], [])
        self.assertNotIn("normal startup", json.dumps(summary))

    def test_runtime_snapshot_has_no_environment_values(self) -> None:
        def container(name: str) -> dict:
            return {
                "Id": f"{name}-id",
                "Name": f"/{name}",
                "Image": f"sha256:{name}",
                "Created": "2026-07-26T00:00:00Z",
                "RestartCount": 0,
                "Config": {
                    "Image": f"image:{name}",
                    "Cmd": ["nginx"],
                    "WorkingDir": "/",
                    "Env": ["SECRET_VALUE=must-not-leak", "PATH=/usr/bin"],
                    "Labels": {
                        "com.docker.compose.project": "market-data",
                        "com.docker.compose.project.working_dir": "/srv/project",
                        "com.docker.compose.project.config_files": "/srv/compose.yml",
                        "com.docker.compose.service": name,
                    },
                },
                "HostConfig": {"NetworkMode": "market-data_default", "RestartPolicy": {"Name": "unless-stopped"}},
                "State": {"Running": True, "Status": "running", "StartedAt": "2026-07-26T00:00:00Z"},
                "NetworkSettings": {"Ports": {}},
                "Mounts": [],
            }

        def fake_runner(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, json.dumps([container(name) for name in CAPTURE_MODULE.FORMAL_CONTAINERS]), "")

        snapshot = CAPTURE_MODULE.capture_snapshot(runner=fake_runner)
        rendered = json.dumps(snapshot, ensure_ascii=False)
        self.assertIn("SECRET_VALUE", rendered)
        self.assertNotIn("must-not-leak", rendered)
        self.assertNotIn("/usr/bin", rendered)

    def test_sealed_migration_requires_same_image_and_unchanged_sidecars(self) -> None:
        def identity(service: str, container_id: str) -> dict:
            return {
                "container_id": container_id,
                "config_image": "market-data-usda-dashboard:immutable" if service == "usda-dashboard" else f"{service}:immutable",
                "image_id": "sha256:usda" if service == "usda-dashboard" else f"sha256:{service}",
                "running": True,
                "status": "running",
                "restart_count": 0,
                "ports": {},
                "mounts": [],
                "compose": {"service": service, "config_files": str(PRODUCTION_COMPOSE) if service == "usda-dashboard" else "/old/compose.yml"},
            }

        before = {"containers": [identity("spread-dashboard", "spread-old"), identity("usda-dashboard", "usda-old"), identity("oil-world-dashboard", "oil-old")]}
        after = {"containers": [identity("spread-dashboard", "spread-old"), identity("usda-dashboard", "usda-new"), identity("oil-world-dashboard", "oil-old")]}
        candidate = {"status": "passed", "expected_image_id": "sha256:usda"}
        cleanup = {"candidate_container_removed": True}
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "usda-production.env"
            env_file.write_text("USDA_IMAGE=market-data-usda-dashboard:immutable\nUSDA_HOST_PORT=8080\n", encoding="utf-8")
            result = SEAL_MODULE.build_migration_result(
                git_commit="a" * 40,
                git_tree="b" * 40,
                compose=PRODUCTION_COMPOSE,
                environment=env_file,
                before=before,
                candidate=candidate,
                cleanup=cleanup,
                after=after,
            )
        self.assertEqual(result["status"], "migration_verified")
        self.assertEqual(result["formal_after"]["containers"][1]["container_id"], "usda-new")

    def test_execute_candidate_records_validation_before_real_candidate_cleanup(self) -> None:
        inspected = {
            "Id": "candidate-id",
            "Name": "/usda-candidate-abc123",
            "Image": "sha256:usda",
            "Created": "2026-07-26T00:00:00Z",
            "RestartCount": 0,
            "Config": {
                "Image": "market-data-usda-dashboard:abc123",
                "Cmd": ["nginx", "-g", "daemon off;"],
                "WorkingDir": "/",
                "Env": [],
                "Labels": {
                    "com.docker.compose.project": "market-data-usda-candidate",
                    "com.docker.compose.project.working_dir": "/candidate",
                    "com.docker.compose.project.config_files": str(CANDIDATE_COMPOSE),
                    "com.docker.compose.service": "usda-dashboard",
                },
            },
            "HostConfig": {"NetworkMode": "market-data_default", "RestartPolicy": {"Name": "no"}},
            "State": {"Running": True, "Status": "running", "StartedAt": "2026-07-26T00:00:00Z"},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]}},
            "Mounts": [],
        }
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            if command[:3] == ["docker", "inspect", "usda-candidate-abc123"]:
                seen = sum(1 for item in calls if item[:3] == ["docker", "inspect", "usda-candidate-abc123"])
                if seen == 1:
                    return subprocess.CompletedProcess(command, 0, json.dumps([inspected]), "")
                return subprocess.CompletedProcess(command, 1, "", "No such container")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / "candidate.env"
            env_file.write_text(
                "\n".join(
                    [
                        "USDA_IMAGE=market-data-usda-dashboard:abc123",
                        "USDA_CANDIDATE_CONTAINER_NAME=usda-candidate-abc123",
                        "USDA_CANDIDATE_HOST_PORT=18080",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(CANDIDATE_MODULE.subprocess, "run", side_effect=fake_run), mock.patch.object(
                CANDIDATE_MODULE, "wait_for_candidate", return_value=[{"http_status": 200}]
            ):
                exit_code = CANDIDATE_MODULE.main(
                    [
                        "--env-file",
                        str(env_file),
                        "--expected-image-id",
                        "sha256:usda",
                        "--evidence-dir",
                        str(root / "evidence"),
                        "--execute",
                    ]
                )
            candidate_result = json.loads((root / "evidence" / "candidate_result.json").read_text(encoding="utf-8"))
            cleanup = json.loads((root / "evidence" / "candidate_cleanup.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(candidate_result["status"], "passed")
        self.assertTrue(cleanup["candidate_container_removed"])
        self.assertLess(
            calls.index(["docker", "rm", "-f", "usda-candidate-abc123"]),
            len(calls) - 1,
        )
        self.assertTrue(all("spread-dashboard" not in command for command in calls))
        self.assertTrue(all("oil-world-dashboard" not in command for command in calls))


if __name__ == "__main__":
    unittest.main()
