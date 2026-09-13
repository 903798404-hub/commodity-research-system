"""Trusted platform planning primitives for the staged Admission migration.

This module does not publish a check or grant main access. The active workflow
is unchanged until the separately reviewed workflow migration is installed.
"""
from __future__ import annotations
import ast
from collections import Counter
import hashlib
import json
import re

RUNNERS = {"linux": "ubuntu-24.04", "windows": "windows-2022"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identity(value):
    if (type(value) is not dict or set(value) != {"commit", "tree"}
            or any(not isinstance(v, str) or not re.fullmatch("[0-9a-f]{40}", v) for v in value.values())):
        raise ValueError("EXACT_COMMIT_TREE_REQUIRED")


def inventory(source):
    """Static identity + minimum multiplicity, without importing candidate code."""
    result = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            raise ValueError("CLASS_TESTS_REQUIRE_EXPLICIT_PLATFORM_INVENTORY")
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
            continue
        if node.name in result:
            raise ValueError("DUPLICATE_TEST_IDENTITY")
        count = 1
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "parametrize":
                if len(dec.args) < 2 or not isinstance(dec.args[1], (ast.List, ast.Tuple)) or not dec.args[1].elts:
                    raise ValueError("PARAMETERIZATION_REQUIRES_EXPLICIT_INVENTORY")
                count *= len(dec.args[1].elts)
        result[node.name] = count
    if not result:
        raise ValueError("EMPTY_TEST_COLLECTION")
    return result


def plan(required_paths, trusted_sources, candidate_sources, policy, *, base, candidate):
    """The caller must obtain all trusted inputs from independently selected main.

    Candidate changes to this policy never alter routing in the current run.
    Unknown added tests in a mixed-platform file run on BOTH platforms until
    trusted policy classifies them; removing a function or parameter case fails.
    Files without platform metadata remain required on Linux.
    """
    identity(base); identity(candidate)
    if type(policy) is not dict or set(policy) != {"schema_version", "files"} or policy["schema_version"] != "required-test-platforms/1" or type(policy["files"]) is not dict:
        raise ValueError("INVALID_PLATFORM_POLICY")
    lanes = {p: [] for p in RUNNERS}
    paths = sorted(set(required_paths))
    if not paths:
        raise ValueError("EMPTY_TEST_PLAN")
    for path in paths:
        if path not in candidate_sources:
            raise ValueError("REQUIRED_TEST_REMOVED: " + path)
        if not isinstance(path, str) or not re.fullmatch(r"08_tests/(?:[A-Za-z0-9_./-]+)\.py", path) or ".." in path.split("/"):
            raise ValueError("INVALID_TEST_PATH")
        policy_file = policy["files"].get(path)
        if policy_file is None:
            lanes["linux"].append({"selector": path, "minimum_cases": 1})
            continue
        old = inventory(trusted_sources[path])
        new = inventory(candidate_sources[path])
        if type(policy_file) is not dict or set(policy_file) != set(old):
            raise ValueError("TRUSTED_PLATFORM_INVENTORY_INCOMPLETE")
        for name, count in old.items():
            if name not in new or new[name] < count:
                raise ValueError("REQUIRED_TEST_REMOVED: " + path + "::" + name)
        for name, count in new.items():
            selected = ("linux", "windows")
            if name in policy_file:
                rule = policy_file[name]
                if type(rule) is not dict or set(rule) != {"kind", "reason"} or not isinstance(rule["reason"], str) or not rule["reason"].strip():
                    raise ValueError("INVALID_PLATFORM_RULE")
                if rule["kind"] == "CROSS_PLATFORM_TEST": selected = ("linux",)
                elif rule["kind"] == "WINDOWS_REQUIRED_TEST": selected = ("windows",)
                else: raise ValueError("UNKNOWN_REQUIRED_PLATFORM")
            for platform in selected:
                lanes[platform].append({"selector": path + "::" + name, "minimum_cases": count})
    value = {"schema_version": "platform-test-plan/1", "base": base, "candidate": candidate,
             "policy_sha256": digest(policy), "lanes": lanes, "runners": RUNNERS,
             "candidate_test_sha256": {p: hashlib.sha256(candidate_sources[p]).hexdigest() for p in paths}}
    return {**value, "plan_sha256": digest(value)}


def aggregate(plan_value, receipts, *, workflow_run_id, workflow_run_attempt, job_results):
    """Aggregate only authenticated same-run receipts on the trusted final job.

    job_results must be GitHub needs/job facts, not fields from candidate JSON.
    Per-platform receipt metadata alone is NOT publisher authentication.
    No optional Windows receipt when Windows obligations exist; no skip success.
    """
    identity(plan_value["base"]); identity(plan_value["candidate"])
    if plan_value["plan_sha256"] != digest({k: v for k, v in plan_value.items() if k != "plan_sha256"}):
        raise ValueError("PLAN_DIGEST_MISMATCH")
    required = {p for p, tests in plan_value["lanes"].items() if tests}
    errors = []
    if type(receipts) is not dict or set(receipts) != required:
        return {"result": "FAIL", "failure_codes": ["PLATFORM_RESULT_SET_MISMATCH"]}
    for platform in sorted(required):
        receipt = receipts[platform]
        fields = {"schema_version", "base", "candidate", "plan_sha256", "platform", "runner_os",
                  "runner_environment", "workflow_run_id", "workflow_run_attempt", "tests"}
        if type(receipt) is not dict or set(receipt) != fields:
            errors.append("INVALID_PLATFORM_RECEIPT"); continue
        expected_os = {"linux": "Linux", "windows": "Windows"}[platform]
        expected = dict(schema_version="platform-test-result/1", base=plan_value["base"], candidate=plan_value["candidate"],
                        plan_sha256=plan_value["plan_sha256"], platform=platform, runner_os=expected_os,
                        runner_environment="github-hosted", workflow_run_id=workflow_run_id, workflow_run_attempt=workflow_run_attempt)
        if any(receipt[k] != value for k, value in expected.items()) or job_results.get(platform) != "success":
            errors.append("PLATFORM_IDENTITY_OR_JOB_FAILED"); continue
        tests = receipt["tests"]
        if type(tests) is not list or not tests:
            errors.append("EMPTY_PLATFORM_COLLECTION"); continue
        observed = Counter()
        seen = set()
        for test in tests:
            if type(test) is not dict or set(test) != {"nodeid", "outcome"} or not isinstance(test["nodeid"], str):
                errors.append("INVALID_TEST_RESULT"); continue
            node = test["nodeid"]
            if node in seen:
                errors.append("DUPLICATE_TEST_RESULT")
            seen.add(node)
            if test["outcome"] != "passed": errors.append("REQUIRED_TEST_FAILED_OR_SKIPPED")
            matches = [v for v in plan_value["lanes"][platform] if node == v["selector"] or node.startswith(v["selector"] + "[") or ("::" not in v["selector"] and node.startswith(v["selector"] + "::"))]
            if len(matches) != 1: errors.append("UNPLANNED_PLATFORM_TEST")
            else: observed[matches[0]["selector"]] += 1
        for obligation in plan_value["lanes"][platform]:
            if observed[obligation["selector"]] < obligation["minimum_cases"]:
                errors.append("REQUIRED_CASES_MISSING")
    return {"result": "FAIL" if errors else "PASS", "failure_codes": sorted(set(errors)),
            "base": plan_value["base"], "candidate": plan_value["candidate"], "plan_sha256": plan_value["plan_sha256"]}
