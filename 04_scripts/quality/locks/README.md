# Hosted Python dependency locks

CI installs every Python dependency with `--require-hashes`, then runs `pip check`.
Download caches are keyed by the actual platform lock. Cache hits still undergo hash checks.

- `linux-py312.txt`: existing development pins and hashes, adding cryptography and PyMySQL declared by current runtime inputs, and replacing Windows mini-racer with the audited Linux AKShare dependencies from the runtime lock.
- `windows-py312.txt`: the complete Windows development pins and archive hashes, identical to the synchronized root development lock. All existing package versions remain unchanged.
- `aggregate-py312.txt`: only the YAML/JSON-schema dependency closure used by final aggregation.
- `host-tools-py310.txt`: Python 3.10 host compatibility tools, including the Python 3.10-only pytest dependencies and compatible transitive versions.
- `browser-py312.txt`: Playwright 1.55.0 and its Python dependency closure. Chromium and operating-system packages remain separately provisioned and recorded by the existing workflow.

The single candidate full-regression job installs the candidate Linux lock and requires ALL_GREEN: no failures, skips, missing collections or altered sources. Historical paired comparison remains a diagnostic command, not a main-entry workflow. Main-entry evidence still binds exact Commit/Tree, plan, runner and actual environment; locked Python packages do not imply that hosted images, system packages or fonts are immutable.

To regenerate, use the corresponding Python version on the named platform and the existing `pip-tools==7.5.0`. Run `python -m piptools compile --generate-hashes --allow-unsafe --output-file=<profile>.txt <profile>.in` from this directory. Existing output pins are retained unless an upgrade is explicitly requested. Review package/version changes and validate the real hosted environments before admission. The initial full profiles are audited projections of the existing locks, not claimed to have been compiled on Linux locally.

Local development and production continue to use the repository's existing root locks. These CI locks do not authorize local environment changes or production deployment.

The root `requirements-dev.txt` is compiled on Windows Python 3.12 from
`requirements-dev.in`, with `--generate-hashes --allow-unsafe --strip-extras`.
Use the reviewed `windows-py312.txt` as a constraint when synchronizing it to
preserve existing pins. This updates the lock file; it does not install packages.
Actual Hosted installs read the compiled CI `.txt` files, not root `.in` files.
Windows and aggregate profiles have no Linux release replay consumers; host,
Linux and browser profile changes retain full lifecycle replay. CI routing and
consumer-boundary tests must be reviewed when changing that separation.

Validation checks current direct `.in` requirements, platform markers and selected extras as well as dependency closure and archive hashes. `pip check` alone cannot detect an omitted project dependency that no installed package declares.
