# S1 — Approved Image + Controlled Ephemeral Launcher

Status: candidate capability only. No production promotion, deployment, Capture migration, scheduler or worker integration. Existing signed-grant consumers remain unchanged (`LEGACY_TASK_RUNTIME_STATUS = STILL_ACTIVE_FOR_EXISTING_CONSUMERS`).

## Existing identity and new approval record

The existing release flow already checks exact local Docker `Image ID` against Commit/Tree, OCI `org.opencontainers.image.revision` and `market-data.git.tree` labels, and `/app/RELEASE.json` during candidate/deployment validation (`04_scripts/runtime/routine_release.py`, `09_deploy/runtime_identity/host_authorization.py`). It does **not** expose a single task-runtime Approved Production Image record. S1 defines that record, but does not write it or alter Promotion. Promotion must eventually atomically publish it only after Build Once / Candidate / Hosted validation / explicit approval.

`/etc/market-data/task-runtime/approved-image.json` is the future root-controlled record:

```json
{"schema_version":"approved-production-image/1","approved_commit":"<40 hex>","approved_tree":"<40 hex>","image_identity_kind":"LOCAL_DOCKER_IMAGE_ID","image_identity":"sha256:<64 hex>"}
```

`LOCAL_DOCKER_IMAGE_ID` is the daemon-local image ID, **not** a registry digest. Future `REGISTRY_DIGEST` requires `repository@sha256:<64 hex>`. The two formats are validated separately. Main moving, routine data tasks and the launcher must not update this record. Approved Production Commit remains independent of main. The record is deliberately *not* a new per-task signed grant. The host must protect the file and parent from group/other writes; the production reader requires root ownership on Linux.

The launcher verifies image ID (or exact RepoDigest) and OCI Commit/Tree labels before creating a container. The established release pipeline still supplies deeper `/app/RELEASE.json` verification during promotion; S1 does not claim that label comparison alone proves build provenance. The promoted image must be the exact one validated, not a rebuilt image.

## Host boundary and caller API

Only a trusted host process has Docker authority. `HostLauncher.run_task(task_type, business_identity)` accepts a registered task name and `{business_date, session}` only. The CLI accepts only those fields. Image, command/entrypoint, UID/GID, network, mounts, secret and tmpfs come exclusively from `/etc/market-data/task-runtime/policies.json`, a root-controlled host file. The business worker and task container must not get the Docker socket or docker-group membership. Protect the launcher binary, policy file, approval file, receipt directory and Docker-accessible host account as production authority. Do not expose constructor injection to untrusted processes; it exists for host composition and tests.

The policy record schema is `host-task-policies/1` with a `tasks` map. Each task has exactly `image_identity_kind`, `image_identity`, `command` (non-empty argv), `uid`, `gid`, `network`, `mounts`, `secret` (optional), and `tmpfs` (bounded). Host policy image must exactly equal the current approval record before Docker is called. No mutable tags or arbitrary caller Docker options are accepted. Docker `create` uses the approved immutable reference, forces policy entrypoint, disables pulls, and sets read-only rootfs, `cap-drop=ALL`, `no-new-privileges`, explicit user/network/mounts. Image-declared implicit volumes are rejected. A container is started, waited, inspected and removed. Host receipt is written atomically. An exit 0 means only generic task-process success; a future Capture worker must independently verify its immutable snapshot before business success.

A future `soybean_capture` policy would bind `image_identity_kind`/`image_identity` to the same values as the approval record, set `command` to a fixed capture adapter argv, `uid`/`gid` to `24051`, `network` to an explicit Tankan-reachable network, `mounts` to the two rows below, `secret` to the fixed Tankan file row, and `tmpfs` to `[]` unless proven necessary. S1 does not ship an active production policy file because the present Capture CLI still requires the legacy signed runtime and no Approved Task Image has been promoted.

The proposed dedicated `soybean-capture` container identity is numeric UID/GID `24051:24051`. The repo's existing public-intraday and spread containers use shared `65532:65532`; S1 deliberately avoids silently sharing that identity. `24051` is a candidate policy value, not an instruction to create or alter a production Linux user in S1. Host launcher alone has Docker control. The policy model does not hardcode a UID in business code. Real host file permissions and UID/GID mapping require Linux acceptance.

## Future soybean capture mount matrix

| Access | Host-owned source (future policy) | Container target | Reason |
| --- | --- | --- | --- |
| RO | exact capture request/calendar input root | `/runtime/inputs` | Capture CLI reads request and calendar, validates file identities |
| RW | dedicated immutable snapshot root | `/runtime/snapshots` | Capture seal/write and idempotent existing snapshot read |
| RO secret | host-managed Tankan credential file | `/run/secrets/tankan.env` | Tankan client reads a fixed in-container path |

No manual CNF, AM/PM profit result, lifecycle/event write root, Public Current, FULL DAILY write root, repository bind or Docker socket. Host worker owns event/lifecycle writes; the capture container does not. This is a *future* matrix, not a runnable production policy: current `public_intraday_runtime.py` still requires a signed grant, runtime marker and existing mount layout. S1 does not migrate it. The current capture CLI also needs exact request/calendar filenames and business date/session, so later migration must provide a fixed adapter without accepting caller-supplied command or path. Network access to Tankan is an explicit future policy choice, not implied by mounts; S1 Hosted fake test uses `network=none`.

Use a root-controlled host-managed secret file and a fixed read-only bind. On Linux, a root-owned mode 0600 file will not generally be readable by UID 24051; provision root-owned, tightly dedicated-group-readable ACL/mode for the mapped GID and verify it in Hosted/production acceptance. Never record secret content or pass it in environment variables. Rotations affect new containers. No Vault or new secret platform is introduced.

## Receipt and outcomes

`task-execution-receipt/1` records task ID/type, business identity, approved Commit/Tree, image kind/value, container ID, UID/GID, UTC start/end, exit code, input identity (canonical JSON SHA-256 of business identity in S1), output identity (null until later worker reconciliation), result status and error category. It contains no secret values or Docker stderr. The receipt directory is host-owned and must be protected and monitored. Generic status is one of `SUCCESS`, `LAUNCH_FAILED`, `TASK_FAILED`, `RUNTIME_FAILED`, `POLICY_REJECTED`, `IMAGE_NOT_APPROVED`. `SUCCESS` is **not** AM/PM sealed success. Cleanup failure is a runtime failure.

## Acceptance boundary

`08_tests/test_task_runtime_s1.py` exercises contract and fake Docker transport locally. `08_tests/test_task_runtime_s1_docker.py` is an **opt-in** Linux Docker E2E (`RUN_TASK_RUNTIME_DOCKER_E2E=1`): it builds a disposable fixture image, verifies actual UID, read/write/secret, missing forbidden paths/socket, read-only rootfs, dropped capabilities, no-new-privileges, receipt and cleanup. It does not run automatically on Windows and is not evidence of Hosted acceptance until actually executed in Hosted Linux. The test uses a fake credential only. The test fixture's temporary approval reader bypasses root ownership because GitHub runner cannot own a root-controlled file; production reader keeps the ownership check.

No existing Promotion, Provider Isolation, Application Runtime Readability, FULL DAILY, Public Current, Phase A, Capture RuntimeContext, dashboard grant or signed-grant consumer is modified by S1.
