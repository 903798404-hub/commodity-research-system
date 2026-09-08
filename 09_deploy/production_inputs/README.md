# Production input authority

This directory provides the host-side authority boundary for two immutable production inputs:

- a new formal historical runtime initialized by byte-copying the already verified stage-a release; and
- a production historical Tankan CNF cache extracted from `quanyong.market.soybean_param.cnf`.

The JSON schemas describe document shape. A document passing a schema is **not authorization**. `asset_tool.py` accepts approval evidence only from `/etc/market-data/production-input-authority`, checks its protected ownership and permissions, binds it to the exact producer checkout and helper image, and then applies the semantic source contract in host code.

## Execution boundary

Run the host driver with Python 3.10 and the standard library from a clean, detached, independent shallow checkout at the approved producer commit and tree. The producer identity is the checkout containing this tool. It is separate from the helper application image identity.

The real Tankan query for a new CNF update runs only on the approved Windows extraction host. Parquet validation and application consumer loading run later on the server in the already existing approved application image. Supply its immutable Docker image ID, application commit, and application tree as `scope.helper_image`; this image identity means `server_validation_only` for a local transfer and never claims the image performed the Windows query. The tool verifies the image ID, its OCI revision label, and `/app/RELEASE.json`. It never builds or pulls an image. Do not rebuild the existing 47fe image for this closure.

All approval files, candidate paths, formal paths, validation reports, and receipts must be below their fixed protected roots:

- approvals: `/etc/market-data/production-input-authority`
- candidates: `/var/lib/market-data/production-input-candidates`
- formal assets: `/var/lib/market-data/production-input-assets`
- evidence: `/var/lib/market-data/production-input-evidence`
- incoming local transfers: `/var/lib/market-data/production-input-incoming`

The host must run as root. Existing path components must be root-owned and not group- or world-writable. Paths must be absolute and canonical and may not contain symlinks or junctions. The candidate and formal destinations must differ, may not contain `preview`, and must not already exist when created or published. Publication is exclusive and immutable; it does not refresh or overwrite an existing asset.

## Approval evidence

Every approval document has `schema_version: production-input-approval/1`, a non-empty `approval_id`, an aware `approved_at` timestamp, a non-empty `approved_by`, the lowercase SHA-256 of the current user authorization attachment in `task_evidence_sha256`, and one exact `scope`. Operators assign distinct event IDs; the tool binds approvals by their exact document SHA and does not maintain a global ID registry. The task evidence binds the document to the user instruction instead of relying only on a self-declared operator name. The scope contains:

- `kind`: `historical` or `cnf`
- `runtime_id` and `release_id`
- `candidate_path` and `formal_path`
- `producer.commit` and `producer.tree` for the host tool checkout
- `helper_image.image_id`, `helper_image.commit`, and `helper_image.tree` for the existing application image
- `source`, in the kind-specific shape below
- for a new CNF update, closed `local_extraction` fields `mode: windows_local_transfer`, the approved `execution_host`, and the exact repository-external Windows `output_path`

An initial approval uses `historical_initialization_approved` or `cnf_extraction_approved`. Both `asset_manifest_sha256` and `prior_approval_sha256` must be `null`; the asset does not exist yet.

After candidate validation passes, create a separate approval with `state: asset_approved`, the unchanged exact scope, `asset_manifest_sha256` set to the candidate's exact `asset_manifest.json` SHA-256, and `prior_approval_sha256` set to the exact initial approval file SHA-256. This second evidence approves the result for production publication. Approval to initialize or query once does not approve the resulting bytes for production use.

## Historical initialization

The historical `source` binds all of the following:

- immutable source runtime root and source manifest path
- source manifest SHA-256
- stage-a release manifest SHA-256
- stage-a `release_index.json` SHA-256
- exact SHA-256 for all four payloads

The tool verifies these identities and the stage-a manifest bindings before creating anything. It copies the four payload byte-for-byte into a new candidate release and creates a new release manifest, index, runtime ID, and release ID. It does not add manual CNF data. It verifies the source again after the copy and records `source_bytes_mutated: false` and `historical_values_recalculated: false`.

This is a new formal initialization based on verified input today. It does not grant retroactive approval to the old stage-a candidate. The four historical payload bytes must remain unchanged; only the new formal release metadata and marker are generated.

Create and validate the candidate:

```text
python3.10 -B 09_deploy/production_inputs/asset_tool.py create \
  --approval /etc/market-data/production-input-authority/historical-initial.json

python3.10 -B 09_deploy/production_inputs/asset_tool.py validate \
  --approval /etc/market-data/production-input-authority/historical-initial.json \
  --report /var/lib/market-data/production-input-evidence/historical-validation.json
```

The `create` result reports `candidate_path`, `runtime_id`, `release_id`, the asset manifest SHA, and observations containing the new release manifest SHA, index SHA, record count, date range, and the two false mutation/recalculation flags. The asset manifest's `payloads` records the complete payload SHA and size set.

## Production Tankan CNF extraction

The CNF `source` is fixed to:

```text
identity = tankan:quanyong.market.soybean_param:cnf
database = quanyong
schema = market
table = soybean_param
field = cnf
query_time_range = all_available_at_extraction
```

`query_sha256` must be `90a9f6fd1d30d6d662c2e3f8c20b11c9eb3a3bae2d46959d3e362680928fec7f`, the SHA-256 of the exact SQL constant in `asset_tool.py`. The source secret is a separately protected file mounted read-only only for extraction; credentials never enter the manifest or evidence output.

This path only accepts a real query against the configured `quanyong` database. It requires both transaction read-only settings to be `on`, records the actual database and relation identity, rolls the transaction back, and preserves the queried CNF and `updated_at` values. It does not accept Preview identity or synthetic rows as production input. It preserves `NULL` separately from zero, performs no filling or interpolation, invents no timestamps, and does not carry values across dates or months. Its natural key remains `(trade_date, origin, month)`.

Run `extract-local` from the exact approved producer checkout on Windows. The checkout must be an independent clean detached Git repository with the canonical GitHub origin; it cannot be a linked worktree. Pin the approval copy independently by its SHA-256. The output directory must match `scope.local_extraction.output_path`, be outside the repository, and not already exist:

```text
python -B 09_deploy/production_inputs/asset_tool.py extract-local \
  --approval C:\protected\cnf-initial.json \
  --approval-sha256 <exact-lowercase-sha256> \
  --secret-file C:\protected\tankan.env \
  --output C:\production-input-transfer\soybean-cnf-20260908-b01
```

This creates only `historical_cnf_cache.parquet` and `local_extraction_transfer.json`. It records the actual Python runtime, dependency versions, execution host, fixed Git-blob identities for this extractor and the imported `03_src` Tankan client, query and database identities, read-only proof, timestamps, and cache observations. It creates no formal marker, asset manifest, production approval, or publication.

Transfer that fixed two-file directory to a new root-owned, non-writable incoming directory on the server. Pin the transfer manifest SHA-256 independently. The server has no Tankan network role and performs no extraction:

```text
python3.10 -B 09_deploy/production_inputs/asset_tool.py receive-local \
  --approval /etc/market-data/production-input-authority/cnf-initial.json \
  --transfer /var/lib/market-data/production-input-incoming/soybean-cnf-20260908-b01 \
  --transfer-sha256 <exact-lowercase-manifest-sha256>

python3.10 -B 09_deploy/production_inputs/asset_tool.py validate \
  --approval /etc/market-data/production-input-authority/cnf-initial.json \
  --report /var/lib/market-data/production-input-evidence/cnf-validation.json
```

Receive rejects missing or extra files, links, traversal, Preview paths, approval/source/producer/path drift, byte or size changes, and schema or provenance drift. It exclusively copies both transfer files into a new candidate and seals them in `asset_manifest.json`; it does not run Docker. Validation then invokes the existing 47fe image once with `--network none`, mounts the candidate read-only, and recomputes the cache semantics. It checks the exact columns, natural-key uniqueness, source identity, nullable numeric CNF type, source timestamps, cache SHA and size, row hash, row count, date range, schema fingerprint, null/zero counts, actual database identity, and read-only transaction proof. The host verifies that validation did not mutate the candidate.

## Publication

Only after validation and a separate exact asset approval may the candidate be published:

```text
python3.10 -B 09_deploy/production_inputs/asset_tool.py publish \
  --approval /etc/market-data/production-input-authority/historical-asset.json \
  --initial-approval /etc/market-data/production-input-authority/historical-initial.json \
  --report /var/lib/market-data/production-input-evidence/historical-validation.json \
  --receipt /var/lib/market-data/production-input-evidence/historical-publication.json
```

Use the equivalent CNF evidence paths for the CNF asset. Publication revalidates the approval chain, exact manifest, validation report, producer, helper image, candidate files, and source. It copies files exclusively into a unique staging directory under the protected formal parent, compares all bytes, validates the staged copy, and makes files read-only and directories non-writable. Linux `renameat2(RENAME_NOREPLACE)` then publishes the complete directory atomically; even an existing empty formal destination is never replaced. Failed staging remains available as evidence. A publication receipt is mandatory before claiming readiness or mounting the asset. There is no existing-image-refresh or existing-asset-refresh path.

The published paths are production inputs for separate final wiring. Publication does not change pages, business keys, Runtime V2, Compose variables, or deployment state. The existing `IMPORT_PROFIT_PREVIEW_HISTORICAL_CNF_PATH` name remains legacy naming debt; legality depends on the formal asset authority and provenance. Final wiring still requires refreshed authorization if the earlier pre-release authorization has expired, followed by production mount and Compose identity revalidation and the normal Production Approval. Until then, production adoption remains blocked even when both formal inputs are ready.
