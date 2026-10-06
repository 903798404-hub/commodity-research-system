# Hosted Python dependency locks

CI installs every Python dependency with `--require-hashes`, then runs `pip check`.
Download caches are keyed by the actual platform lock. Cache hits still undergo hash checks.

- `linux-py312.txt`: existing development pins and hashes, replacing Windows mini-racer with the audited Linux AKShare dependencies from the runtime lock.
- `windows-py312.txt`: existing Windows development lock without version changes.
- `aggregate-py312.txt`: only the YAML/JSON-schema dependency closure used by final aggregation.
- `host-tools-py310.txt`: Python 3.10 host compatibility tools, including the Python 3.10-only pytest dependencies and compatible transitive versions.
- `browser-py312.txt`: Playwright 1.55.0 and its Python dependency closure. Chromium and operating-system packages remain separately provisioned and recorded by the existing workflow.

The two full-regression jobs both install the candidate executor's Linux lock. The base checkout must not choose another dependency environment. Main-entry evidence still binds exact Commit/Tree, plan, runner and actual environment; locked Python packages do not imply that hosted images, system packages or fonts are immutable.

To regenerate, use the corresponding Python version on the named platform and the existing `pip-tools==7.5.0`. Run `python -m piptools compile --generate-hashes --allow-unsafe --output-file=<profile>.txt <profile>.in` from this directory. Existing output pins are retained unless an upgrade is explicitly requested. Review package/version changes and validate the real hosted environments before admission. The initial full profiles are audited projections of the existing locks, not claimed to have been compiled on Linux locally.

Local development and production continue to use the repository's existing root locks. These CI locks do not authorize local environment changes or production deployment.
