"""Read-only, evidence-first classification for spread release images.

An OCI image records where it was built; it does not by itself say whether the
same Image ID was later promoted.  This module makes that distinction explicit
for reports and cleanup dry-runs.  It intentionally has no Docker mutation
code: callers must make a separately approved deletion decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


ACTIVE_PRODUCTION = "active_production"
ROLLBACK_PROTECTED = "rollback_protected"
SEALED_CANDIDATE = "sealed_candidate"
UNSEALED_CANDIDATE = "unsealed_candidate"
VALIDATION_ONLY = "validation_only"
ORPHAN_CANDIDATE = "orphan_candidate"
UNKNOWN_PROTECTED = "unknown_protected"

RETAIN = "retain"
REMOVE_CANDIDATE_TAGS_ONLY = "remove_candidate_tags_only"
ELIGIBLE_FOR_IMAGE_DELETE = "eligible_for_image_delete"


def _strings(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {value for value in values if isinstance(value, str) and value}


def _image_id(record: Mapping[str, Any]) -> str:
    value = record.get("id") or record.get("image_id")
    return value if isinstance(value, str) else ""


def _labels(record: Mapping[str, Any]) -> dict[str, str]:
    raw = record.get("labels")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): str(value) for key, value in raw.items() if isinstance(value, (str, int, bool))}


def _tags(record: Mapping[str, Any]) -> list[str]:
    tags = record.get("repo_tags")
    if tags is None:
        tags = record.get("tags")
    return sorted(_strings(tags))


def _artifact_origin(labels: Mapping[str, str]) -> str:
    origin = labels.get("market-data.artifact.origin")
    if origin:
        return origin
    # Legacy candidate images retain their historic build-origin marker.  It is
    # deliberately not treated as a deployment-status assertion.
    if labels.get("market-data.release.type") == "candidate":
        return "candidate"
    return "unknown"


def _artifact_origin_source(labels: Mapping[str, str]) -> str:
    if labels.get("market-data.artifact.origin"):
        return "market-data.artifact.origin"
    if labels.get("market-data.release.type") == "candidate":
        return "legacy market-data.release.type"
    return "unlabelled"


def _formal_tags(tags: Iterable[str]) -> list[str]:
    return sorted(tag for tag in tags if ":spread-" in tag and "-b" in tag)


def _candidate_tags(tags: Iterable[str]) -> list[str]:
    # A Git prefix often begins with ``c``; matching ``-c`` would therefore
    # misclassify ordinary immutable release tags as candidate aliases.
    return sorted(tag for tag in tags if "candidate" in tag.lower())


@dataclass(frozen=True)
class GovernanceEvidence:
    """Cross-artifact references needed to classify one or more images."""

    active_image_ids: frozenset[str] = frozenset()
    production_image_ids: frozenset[str] = frozenset()
    production_image_refs: frozenset[str] = frozenset()
    rollback_image_ids: frozenset[str] = frozenset()
    rollback_image_refs: frozenset[str] = frozenset()
    release_bundle_image_ids: frozenset[str] = frozenset()
    release_bundle_image_refs: frozenset[str] = frozenset()
    sealed_candidate_image_ids: frozenset[str] = frozenset()
    candidate_image_refs: frozenset[str] = frozenset()
    validation_image_ids: frozenset[str] = frozenset()


def evidence_from_artifacts(
    *,
    containers: Iterable[Mapping[str, Any]] = (),
    deployment_results: Iterable[Mapping[str, Any]] = (),
    deployment_plans: Iterable[Mapping[str, Any]] = (),
    candidate_results: Iterable[Mapping[str, Any]] = (),
    release_manifests: Iterable[Mapping[str, Any]] = (),
) -> GovernanceEvidence:
    """Extract only identity references from sealed-artifact-like mappings."""

    active_ids: set[str] = set()
    for container in containers:
        if container.get("name") == "spread-dashboard" and container.get("running") is not False:
            active_ids.add(_image_id(container))

    production_ids: set[str] = set()
    production_refs: set[str] = set()
    for result in deployment_results:
        if result.get("status") != "production_verified":
            continue
        production_ids.update(
            value
            for value in (result.get("actual_image_id"), result.get("candidate_image_id"))
            if isinstance(value, str) and value
        )
        production_refs.update(
            value
            for value in (result.get("image_ref"), result.get("config_image"))
            if isinstance(value, str) and value
        )

    rollback_ids: set[str] = set()
    rollback_refs: set[str] = set()
    for plan in deployment_plans:
        rollback_ids.update(
            value
            for value in (plan.get("rollback_image_id"),)
            if isinstance(value, str) and value
        )
        rollback_refs.update(
            value
            for value in (plan.get("rollback_image_ref"),)
            if isinstance(value, str) and value
        )

    release_ids: set[str] = set()
    release_refs: set[str] = set()
    for manifest in release_manifests:
        release_ids.update(
            value for value in (manifest.get("image_id"),) if isinstance(value, str) and value
        )
        release_refs.update(
            value for value in (manifest.get("image_ref"),) if isinstance(value, str) and value
        )

    sealed_ids: set[str] = set()
    validation_ids: set[str] = set()
    candidate_refs: set[str] = set()
    for result in candidate_results:
        candidate_id = result.get("candidate_image_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            continue
        validation_ids.add(candidate_id)
        image_ref = result.get("image_ref")
        if isinstance(image_ref, str) and image_ref:
            candidate_refs.add(image_ref)
        if result.get("status") == "candidate-validated":
            sealed_ids.add(candidate_id)

    return GovernanceEvidence(
        active_image_ids=frozenset(active_ids),
        production_image_ids=frozenset(production_ids),
        production_image_refs=frozenset(production_refs),
        rollback_image_ids=frozenset(rollback_ids),
        rollback_image_refs=frozenset(rollback_refs),
        release_bundle_image_ids=frozenset(release_ids),
        release_bundle_image_refs=frozenset(release_refs),
        sealed_candidate_image_ids=frozenset(sealed_ids),
        candidate_image_refs=frozenset(candidate_refs),
        validation_image_ids=frozenset(validation_ids),
    )


def classify_image(record: Mapping[str, Any], evidence: GovernanceEvidence) -> dict[str, Any]:
    """Classify an image with precedence that protects promoted Image IDs."""

    image_id = _image_id(record)
    tags = _tags(record)
    labels = _labels(record)
    artifact_origin = _artifact_origin(labels)
    formal_tags = _formal_tags(tags)
    # Candidate manifests can use an immutable bNN tag.  Treat it as a
    # candidate alias only while it is not also named by a production result.
    candidate_tags = sorted(
        (set(_candidate_tags(tags)) | (set(tags) & set(evidence.candidate_image_refs)))
        - set(evidence.production_image_refs)
    )
    active = image_id in evidence.active_image_ids
    production = image_id in evidence.production_image_ids or bool(
        set(tags) & set(evidence.production_image_refs)
    )
    rollback = image_id in evidence.rollback_image_ids or bool(
        set(tags) & set(evidence.rollback_image_refs)
    )
    release_bundle = image_id in evidence.release_bundle_image_ids or bool(
        set(tags) & set(evidence.release_bundle_image_refs)
    )
    sealed_candidate = image_id in evidence.sealed_candidate_image_ids or release_bundle
    validation_only = image_id in evidence.validation_image_ids and not sealed_candidate
    promotion_mode = "same_image_id" if production and sealed_candidate else "not_promoted"

    if active:
        classification, action, reason = (
            ACTIVE_PRODUCTION,
            RETAIN,
            "active spread-dashboard container references this Image ID",
        )
    elif production:
        classification, action, reason = (
            ACTIVE_PRODUCTION,
            RETAIN,
            "sealed production_verified deployment_result references this Image ID or tag",
        )
    elif rollback:
        classification, action, reason = (
            ROLLBACK_PROTECTED,
            RETAIN,
            "sealed deployment plan references this Image ID or tag as rollback",
        )
    elif sealed_candidate:
        classification, action, reason = (
            SEALED_CANDIDATE,
            RETAIN,
            "sealed candidate result or release bundle references this candidate Image ID",
        )
    elif validation_only:
        classification, action, reason = (
            VALIDATION_ONLY,
            RETAIN,
            "candidate validation reference exists but is not a sealed candidate result",
        )
    elif formal_tags:
        classification, action, reason = (
            UNKNOWN_PROTECTED,
            RETAIN,
            "formal bNN release tag exists without sealed production evidence",
        )
    elif artifact_origin == "candidate" and candidate_tags and record.get("created_at"):
        classification, action, reason = (
            ORPHAN_CANDIDATE,
            ELIGIBLE_FOR_IMAGE_DELETE,
            "dated candidate-tag image has no active, production, rollback, or sealed-result reference",
        )
    elif artifact_origin == "candidate" or candidate_tags:
        classification, action, reason = (
            UNSEALED_CANDIDATE,
            RETAIN,
            "candidate provenance alone is insufficient evidence for image deletion",
        )
    else:
        classification, action, reason = (
            UNKNOWN_PROTECTED,
            RETAIN,
            "unknown image provenance is protected by default",
        )

    # A promoted Image ID can retain an old candidate tag.  Removing that tag
    # is distinct from deleting the image and is safe only after the image has
    # already been retained as production/rollback evidence.
    if classification in {ACTIVE_PRODUCTION, ROLLBACK_PROTECTED} and candidate_tags:
        action = REMOVE_CANDIDATE_TAGS_ONLY
        reason += "; candidate tags may be removed, but the Image ID must be retained"

    return {
        "image_id": image_id,
        "repo_tags": tags,
        "labels": {
            key: labels[key]
            for key in sorted(labels)
            if key.startswith("market-data.") or key.startswith("org.opencontainers.image.")
        },
        "artifact_origin": artifact_origin,
        "artifact_origin_source": _artifact_origin_source(labels),
        "artifact_promotable": labels.get("market-data.artifact.promotable") == "true",
        "created_at": record.get("created_at") if isinstance(record.get("created_at"), str) else None,
        "deployment_status": "production_verified" if production else "not_production_verified",
        "promotion_mode": promotion_mode,
        "formal_tag_present": bool(formal_tags),
        "candidate_tags": candidate_tags,
        "candidate_tag_removed": False,
        "candidate_tag_removal_eligible": action == REMOVE_CANDIDATE_TAGS_ONLY,
        "rollback_references": image_id in evidence.rollback_image_ids
        or bool(set(tags) & set(evidence.rollback_image_refs)),
        "release_bundle_references": release_bundle,
        "production_evidence": production,
        "active_container_reference": active,
        "classification": classification,
        "cleanup_action": action,
        "cleanup_reason": reason,
    }


def classify_images(
    images: Iterable[Mapping[str, Any]], evidence: GovernanceEvidence
) -> list[dict[str, Any]]:
    """Return deterministic, read-only dry-run records for all supplied images."""

    return sorted(
        (classify_image(image, evidence) for image in images),
        key=lambda item: (item["classification"], item["image_id"]),
    )
