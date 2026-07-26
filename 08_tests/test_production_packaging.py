from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPOSITORY = Path(__file__).resolve().parents[1]


def dockerfile_effective_instructions(dockerfile: str) -> tuple[str, ...]:
    """Return Dockerfile instructions while excluding whole-line comments.

    Docker comments are documentation, not part of the image build graph. Keep
    continued instructions together so assertions inspect Docker commands, not
    prose beside them.
    """

    instructions: list[str] = []
    continued: list[str] = []
    for raw_line in dockerfile.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        continued.append(line.rstrip("\\").rstrip())
        if line.endswith("\\"):
            continue

        instructions.append(" ".join(continued))
        continued = []

    if continued:
        instructions.append(" ".join(continued))
    return tuple(instructions)


USDA_ROOT = REPOSITORY / "11_独立应用" / "USDA平衡表"


class ProductionPackagingTests(unittest.TestCase):
    def test_windows_local_docker_is_not_a_release_gate(self) -> None:
        common_rules = (
            "Windows 本地没有 Docker、Podman 或 WSL 属于正常状态",
            "本地不负责生产镜像构建",
        )
        document_rules = {
            "AGENTS.md": (
                "不得再建议用户安装 Docker Desktop、Podman 或 WSL",
                "正式部署直接使用同一个镜像 ID",
            ),
            "07_docs/03_标准开发与生产发布规范.md": (
                "不得要求为本项目安装这些工具",
                "候选验收与正式部署必须复用完全相同的 Image ID",
            ),
        }
        for relative_path, specific_rules in document_rules.items():
            content = (REPOSITORY / relative_path).read_text(encoding="utf-8")
            for rule in (*common_rules, *specific_rules):
                self.assertIn(rule, content)

    def test_spread_image_build_context_includes_home_and_catalog(self) -> None:
        dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY 02_configs /app/02_configs", dockerfile)
        self.assertIn("COPY 05_apps /app/05_apps", dockerfile)
        self.assertTrue((REPOSITORY / "02_configs" / "report_catalog.yaml").is_file())
        self.assertTrue((REPOSITORY / "05_apps" / "home.py").is_file())

    def test_spread_compose_requires_an_explicit_release_image(self) -> None:
        compose = yaml.safe_load(
            (REPOSITORY / "docker-compose.yml").read_text(encoding="utf-8")
        )
        spread = compose["services"]["spread-dashboard"]

        self.assertEqual(compose["name"], "market-data")
        self.assertEqual(
            spread["image"],
            "${SPREAD_IMAGE:?SPREAD_IMAGE must be set to an immutable release tag}",
        )
        self.assertIn("build", spread)

    def test_spread_image_requires_explicit_dashboard_urls(self) -> None:
        compose = yaml.safe_load(
            (REPOSITORY / "docker-compose.yml").read_text(encoding="utf-8")
        )
        environment = compose["services"]["spread-dashboard"]["environment"]

        self.assertEqual(
            environment["USDA_DASHBOARD_URL"],
            "${USDA_DASHBOARD_URL:?USDA_DASHBOARD_URL must be explicitly set}",
        )
        self.assertEqual(
            environment["OIL_WORLD_DASHBOARD_URL"],
            "${OIL_WORLD_DASHBOARD_URL:?OIL_WORLD_DASHBOARD_URL must be explicitly set}",
        )

    def test_spread_config_is_the_only_excel_allowed_into_main_image(self) -> None:
        config_path = REPOSITORY / "02_configs" / "historical_spread_config.xlsx"
        self.assertTrue(config_path.is_file())

        rules = [
            line.strip()
            for line in (REPOSITORY / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertIn("*.xlsx", rules)
        self.assertIn("!02_configs/historical_spread_config.xlsx", rules)
        self.assertGreater(
            rules.index("!02_configs/historical_spread_config.xlsx"),
            rules.index("*.xlsx"),
        )
        self.assertEqual(
            [rule for rule in rules if rule.startswith("!") and rule.endswith(".xlsx")],
            ["!02_configs/historical_spread_config.xlsx"],
        )

    def test_spread_image_declares_immutable_release_identity(self) -> None:
        dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9",
            dockerfile,
        )
        for argument in (
            "MARKET_DATA_GIT_HEAD",
            "MARKET_DATA_GIT_TREE",
            "MARKET_DATA_RELEASE_ID",
            "MARKET_DATA_BUILD_TIME",
            "MARKET_DATA_SOURCE",
        ):
            self.assertIn(f"ARG {argument}", dockerfile)
        for label in (
            "org.opencontainers.image.revision",
            "org.opencontainers.image.version",
            "org.opencontainers.image.created",
            "org.opencontainers.image.source",
        ):
            self.assertIn(label, dockerfile)
        self.assertIn("/app/RELEASE.json", dockerfile)
        self.assertIn('"git_tree":tree', dockerfile)
        self.assertIn("chmod 0444 /app/RELEASE.json", dockerfile)
        for label in (
            "market-data.git.tree",
            "market-data.release.id",
            "market-data.service",
            "market-data.artifact.origin",
            "market-data.artifact.promotable",
            "market-data.deployment.role",
        ):
            self.assertIn(label, dockerfile)

    def test_spread_image_uses_only_the_production_lock_and_cleans_build_caches(self) -> None:
        dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")
        instructions = dockerfile_effective_instructions(dockerfile)
        instruction_text = "\n".join(instructions)
        copy_instructions = [
            instruction for instruction in instructions if instruction.upper().startswith("COPY ")
        ]

        self.assertIn("--require-hashes -r requirements.txt", instruction_text)
        self.assertNotIn("requirements-dev.txt", instruction_text)
        self.assertFalse(any("08_tests" in instruction for instruction in copy_instructions))
        self.assertIn("rm -rf /var/lib/apt/lists/*", instruction_text)
        self.assertIn("rm -rf /root/.cache/pip /tmp/pip-* /tmp/wheels", instruction_text)

    def test_dockerfile_instruction_parser_ignores_comment_only_development_lock_reference(self) -> None:
        instructions = dockerfile_effective_instructions(
            "# requirements-dev.txt remains outside the production image\n"
            "COPY requirements.txt /app/requirements.txt\n"
            "RUN python -m pip install --require-hashes -r requirements.txt\n"
        )

        instruction_text = "\n".join(instructions)
        self.assertNotIn("requirements-dev.txt", instruction_text)
        self.assertIn("COPY requirements.txt /app/requirements.txt", instruction_text)
        self.assertIn("RUN python -m pip install --require-hashes -r requirements.txt", instruction_text)

    def test_dockerfile_instruction_parser_keeps_real_development_lock_reference(self) -> None:
        instructions = dockerfile_effective_instructions(
            "COPY requirements-dev.txt /app/requirements-dev.txt\n"
        )

        self.assertIn("requirements-dev.txt", "\n".join(instructions))

    def test_dynamic_data_and_sensitive_files_are_not_packaged(self) -> None:
        dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")
        rules = {
            line.strip()
            for line in (REPOSITORY / ".dockerignore")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertNotIn("COPY 01_data /app/01_data", dockerfile)
        self.assertNotIn("COPY 06_outputs /app/06_outputs", dockerfile)
        self.assertIn("mkdir -p /app/01_data /app/06_outputs /app/10_logs", dockerfile)
        for rule in (
            ".env",
            ".env.*",
            "**/.env",
            "**/.env.*",
            "**/*.pem",
            "**/*.key",
            "**/id_rsa*",
            "**/id_ed25519*",
            "01_data",
            "06_outputs",
            "10_logs",
        ):
            self.assertIn(rule, rules)

    def test_usda_root_redirect_remains_relative(self) -> None:
        nginx = (USDA_ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")
        self.assertIn("absolute_redirect off;", nginx)
        self.assertIn("location = / {\n        return 302 /usda/;\n    }", nginx)
        self.assertIn("try_files $uri $uri/ /usda/index.html;", nginx)

    def test_usda_image_declares_traceability_labels(self) -> None:
        dockerfile = (USDA_ROOT / "Dockerfile").read_text(encoding="utf-8")
        for argument in ("OCI_REVISION", "OCI_SOURCE", "OCI_CREATED"):
            self.assertIn(f"ARG {argument}=", dockerfile)
        for label in (
            "org.opencontainers.image.revision=${OCI_REVISION}",
            "org.opencontainers.image.source=${OCI_SOURCE}",
            "org.opencontainers.image.created=${OCI_CREATED}",
        ):
            self.assertIn(label, dockerfile)


if __name__ == "__main__":
    unittest.main()
