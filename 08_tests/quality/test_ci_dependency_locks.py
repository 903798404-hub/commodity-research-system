from pathlib import Path
import re

import yaml
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[2]
LOCKS = ROOT / "04_scripts/quality/locks"


def _entries(path):
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"(?m)^([a-zA-Z0-9_.-]+)==([^ ;\\]+).*?$", text))
    assert matches and not any(token in text for token in ("git+", "file://", "--no-require-hashes"))
    entries = {}
    for i, match in enumerate(matches):
        name = match[1].lower().replace("_", "-")
        block = text[match.start():matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        hashes = set(re.findall(r"--hash=sha256:([a-f0-9]{64})(?![a-f0-9])", block))
        assert hashes and name not in entries
        entries[name] = (match[2], hashes)
    return entries


def test_platform_locks_preserve_all_existing_development_pins_and_archive_hashes():
    existing = _entries(ROOT / "requirements-dev.txt")
    windows = _entries(LOCKS / "windows-py312.txt")
    linux = _entries(LOCKS / "linux-py312.txt")
    runtime = _entries(ROOT / "requirements.txt")
    missing_runtime = {"cryptography", "pymysql"}
    assert windows == existing | {k: runtime[k] for k in missing_runtime}
    assert set(linux) == (set(existing) - {"mini-racer"}) | {"akracer", "py-mini-racer"} | missing_runtime
    assert {k: linux[k] for k in existing if k != "mini-racer"} == {k: v for k, v in existing.items() if k != "mini-racer"}
    for name in ("akracer", "py-mini-racer", "cryptography", "pymysql"):
        assert linux[name] == runtime[name]


def _direct_requirements(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("-r "):
            yield from _direct_requirements(path.parent / line[3:].strip())
        elif line:
            yield Requirement(line)


def test_platform_locks_cover_current_direct_inputs_including_selected_extras():
    for platform, system in (("linux", "Linux"), ("windows", "Windows")):
        entries = _entries(LOCKS / f"{platform}-py312.txt")
        environment = default_environment()
        environment.update(platform_system=system, sys_platform="linux" if system == "Linux" else "win32",
                           python_version="3.12", python_full_version="3.12.10", extra="")
        for requirement in _direct_requirements(ROOT / "requirements-dev.in"):
            if requirement.marker and not requirement.marker.evaluate(environment):
                continue
            name = canonicalize_name(requirement.name)
            assert name in entries, (platform, str(requirement))
            assert entries[name][0] in requirement.specifier, (platform, str(requirement), entries[name][0])
            if name == "psycopg" and "binary" in requirement.extras:
                assert entries["psycopg-binary"][0] == entries[name][0]


def test_every_ci_install_uses_an_existing_hashed_lock_and_checks_dependencies():
    workflow = yaml.safe_load((ROOT / ".github/workflows/trusted-main-admission.yml").read_text(encoding="utf-8"))
    installed = []
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            run = step.get("run", "").replace("\\\n", " ")
            if "-m pip install" not in run:
                continue
            assert "-m pip check" in run
            commands = [line for line in run.splitlines() if "-m pip install" in line]
            assert len(commands) == 1
            command = commands[0]
            assert "--require-hashes" in command and "--no-deps" not in command
            match = re.search(r"-r\s+((?:candidate/|executor/)?04_scripts/quality/locks/[a-z0-9-]+\.txt)", command)
            assert match, command
            if step.get("working-directory") != "candidate":
                assert match[1].startswith(("candidate/", "executor/")), command
            relative = re.sub(r"^(?:candidate|executor)/", "", match[1])
            _entries(ROOT / relative)
            installed.append(match[1])
    assert len(installed) == 7
    assert "executor/04_scripts/quality/locks/linux-py312.txt" in installed
    assert {Path(p).name for p in installed} == {p.name for p in LOCKS.glob("*.txt")}


def test_auxiliary_locks_pin_direct_packages_and_python310_only_dependencies():
    aggregate = _entries(LOCKS / "aggregate-py312.txt")
    assert set(aggregate) == {"pyyaml", "jsonschema", "attrs", "jsonschema-specifications", "referencing", "rpds-py", "typing-extensions"}
    assert aggregate["pyyaml"][0] == "6.0.3" and aggregate["jsonschema"][0] == "4.26.0"
    host = _entries(LOCKS / "host-tools-py310.txt")
    for name, version in (("pytest", "9.1.1"), ("cryptography", "50.0.0"), ("pyyaml", "6.0.3"), ("jsonschema", "4.26.0")):
        assert host[name][0] == version
    assert {"exceptiongroup", "tomli", "cffi", "pycparser"} <= host.keys()
    browser = _entries(LOCKS / "browser-py312.txt")
    assert browser["playwright"][0] == "1.55.0"
    assert set(browser) == {"playwright", "pyee", "greenlet", "typing-extensions"}
