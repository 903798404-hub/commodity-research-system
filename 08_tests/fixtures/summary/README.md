# Frozen offline summary samples

These are public weather/NASS development samples frozen for contract tests, not current production data. `manifest.json` records each source and fixture SHA-256, row count and byte size. No credentials, raw snapshots, database connection information or production pointers are included.

Weather samples retain June observations and forecasts for all available historical years, preserving the approved comparison windows and country/region identities. The normal tables retain their original baseline fields. Crop samples retain six public comparison fields and their historical values.

Tests read this directory explicitly. Page tests inject these sources only through pytest monkeypatches; application defaults and production readers are unchanged. Do not replace these files during daily data updates. Change fixtures only with an explicit test-contract review and update the manifest hashes.
