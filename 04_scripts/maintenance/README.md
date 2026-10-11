# Public package retention

This host maintenance tool preserves all historical data, every sealed package
manifest and its original readable paths. It never changes Current, source
records, CNF, revisions, application images, grants or runtime source clones.
No Docker prune, journal purge or business-log expiration is included.

Keep independent copies for Current, explicitly pinned verified rollback
packages, the newest three packages, all packages created within seven days,
and exact package mounts or open files. Older files are verified against their
sealed manifests and atomically replaced by hardlinks to identical root-owned,
read-only objects on the same data disk. Shared objects are never writable by
the application or publication account. No package or unique content is deleted.
New unique data still occupies space; compaction removes repeated identical
files, not the genuine growth of history. There is no unlimited-space claim.

Configuration schema `public-package-retention/1` includes `tool_sha256`,
`data_disk_uuid`, `state_root`, `keep_full_days` (7), `keep_full_count` (3),
`publisher_locks` and `stores`. Each store names its exact `root` and
`protected_package_ids`; required pins must exist. The source SHA, root ownership,
paths and disk UUID are checked on every run. Missing disks fail closed.

Run `public_package_retention.py --config CONFIG` to validate and plan without
altering package files; add `--apply` only in the independently authorized server
maintenance task. Run with the publication `.current.lock` and collector lock
held. Stale pointers, symlinks, special files, damaged bytes, external hardlinks
or missing rollback pins fail closed. Interrupted operations leave each replaced
file complete; a repeat run verifies and resumes. Current and pinned rollback
files are never pooled. Root-owned JSON receipts retain the exact file changes.

The timer runs daily at 04:00 Asia/Shanghai. Capacity is NORMAL below 75%, WARNING
from 75%, CRITICAL from 85%; critical status returns nonzero and is recorded in
the service journal and receipt. This does not silently pause data updates.
Failed runs remain visible; an expired or failed package is not automatically
deleted. Candidate directories, recovery evidence and images stay outside the
automatic scope and require a separate reference/restore audit.

Install only the tested exact source with its hash-bound root-owned config and
validated units; verify an actual service execution, all retained manifest/file
hashes, application readers, unchanged Current and unchanged service identities.
Disabling the timer stops future maintenance. Existing compacted versions remain
directly readable and usable by the normal formal rollback validator.
