"""Protected host tooling for separately approved, immutable production inputs.

The host uses only Python's standard library. Parquet/DB operations execute in
an explicitly pinned, existing application image; this tool never builds one.
No command creates an approval. Approval files are independently supplied by
the operator under the protected authority directory.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_ROOT = Path('/etc/market-data/production-input-authority')
CANDIDATE_ROOT = Path('/var/lib/market-data/production-input-candidates')
FORMAL_ROOT = Path('/var/lib/market-data/production-input-assets')
EVIDENCE_ROOT = Path('/var/lib/market-data/production-input-evidence')
SOURCE_IDENTITY = 'tankan:quanyong.market.soybean_param:cnf'
QUERY = """SELECT trade_date, encode(region::bytea, 'hex') AS region_hex,
       month, cnf, updated_at
FROM market.soybean_param
ORDER BY trade_date, region, month
"""
ORIGINS = {'e5b7b4e8a5bf': ('巴西', 'brazil'),
           'e7be8ee6b9be': ('美湾', 'us_gulf'),
           'e7be8ee8a5bf': ('美西', 'us_pnw'),
           'e998bfe6a0b9e5bbb7': ('阿根廷', 'argentina')}
PAYLOAD_NAMES = ('soybean_business_keys.parquet', 'soybean_market_snapshots.parquet',
                 'soybean_net_crush_results.parquet', 'quality_report.json')
MARKER = '.market-data-runtime.json'
ASSET_MANIFEST = 'asset_manifest.json'
CACHE = 'historical_cnf_cache.parquet'
SCHEMAS = {'historical': 'historical_asset.schema.json', 'cnf': 'cnf_asset.schema.json',
           'approval': 'approval.schema.json'}


class AssetError(ValueError):
    """An authority or identity mismatch; no partial result is production ready."""


def require(condition, message):
    if not condition:
        raise AssetError(message)


def sha_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha_file(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'Regular non-symlink file required')
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else sha_bytes(handle.read())


def digest(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None,
            'Expected lowercase SHA-256')
    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    require(isinstance(value, str), 'Missing timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise AssetError('Invalid timestamp') from None
    require(parsed.utcoffset() is not None, 'Timestamp requires timezone')
    return parsed


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    def bad_constant(value):
        raise AssetError('Non-finite JSON value')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)
    except (UnicodeError, json.JSONDecodeError):
        raise AssetError('Invalid UTF-8 JSON') from None


def read_json(path):
    return strict_json(Path(path).read_text(encoding='utf-8', errors='strict'))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def write_exclusive(path, raw):
    path = Path(path)
    with path.open('xb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path, value):
    write_exclusive(path, json_bytes(value))


def validate_schema(value, schema, label='document'):
    """Small fail-closed evaluator for the exact checked-in schema vocabulary.

    Unsupported keywords fail rather than silently weakening the contract.
    Tests additionally validate the schemas with the reference JSON Schema tool.
    """
    known = {'$schema', 'title', 'description', 'type', 'properties', 'required',
             'additionalProperties', 'const', 'enum', 'pattern', 'minLength',
             'minimum', 'minItems', 'items'}
    require(not set(schema) - known, 'Unsupported schema keyword')
    kind = schema.get('type')
    types = {'object': lambda x: type(x) is dict, 'array': lambda x: type(x) is list,
             'string': lambda x: type(x) is str, 'integer': lambda x: type(x) is int,
             'boolean': lambda x: type(x) is bool, 'null': lambda x: x is None}
    if kind is not None:
        options = kind if isinstance(kind, list) else [kind]
        require(all(t in types for t in options), 'Unsupported schema type')
        require(any(types[t](value) for t in options), f'{label}: invalid type')
    if 'const' in schema:
        require(type(value) is type(schema['const']) and value == schema['const'], f'{label}: constant mismatch')
    if 'enum' in schema:
        require(value in schema['enum'], f'{label}: invalid enum')
    if isinstance(value, dict):
        require(set(schema.get('required', [])) <= set(value), f'{label}: missing fields')
        props = schema.get('properties', {})
        for key, item in value.items():
            child = props.get(key, schema.get('additionalProperties', True))
            require(child is not False, f'{label}: unknown field')
            if isinstance(child, dict):
                validate_schema(item, child, label + '.' + key)
    if isinstance(value, list):
        require(len(value) >= schema.get('minItems', 0), f'{label}: empty array')
        for item in value:
            validate_schema(item, schema.get('items', {}), label)
    if isinstance(value, str):
        require(len(value) >= schema.get('minLength', 0), f'{label}: empty string')
        if 'pattern' in schema:
            require(re.search(schema['pattern'], value) is not None, f'{label}: invalid pattern')
    if type(value) is int and 'minimum' in schema:
        require(value >= schema['minimum'], f'{label}: invalid minimum')


def contract(value, kind):
    validate_schema(value, read_json(Path(__file__).parent / SCHEMAS[kind]))


def no_links(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'Absolute canonical path required')
    require(path == Path(os.path.abspath(path)), 'Noncanonical path')
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            require(not item.is_symlink() and not getattr(item, 'is_junction', lambda: False)(), 'Symlink/junction forbidden')
    return path


def protected(path, *, exists=True):
    path = no_links(path)
    require(os.name == 'posix' and os.geteuid() == 0, 'Protected operation requires Linux host root')
    require(not exists or path.exists(), 'Protected path missing')
    for item in (path, *path.parents):
        if item.exists():
            info = item.stat()
            require(info.st_uid == 0 and not info.st_mode & 0o022, 'Unprotected ownership/permissions')
            require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), 'Unsupported protected file type')
    return path


def under(path, parent):
    path = no_links(path)
    require(path != parent and parent in path.parents, 'Path outside approved namespace')
    return path


def git_identity():
    protected(ROOT)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True, stderr=subprocess.DEVNULL).strip()
    require(git('status', '--porcelain=v1', '--untracked-files=all') == '', 'Producer checkout must be clean')
    require(git('rev-parse', '--is-shallow-repository') == 'true', 'Producer requires independent shallow checkout')
    require(git('rev-parse', '--git-dir') == '.git', 'Producer must not use linked worktree')
    require(git('rev-parse', '--abbrev-ref', 'HEAD') == 'HEAD', 'Producer requires detached HEAD')
    require(git('remote', 'get-url', 'origin') in (
        'git@github.com:903798404-hub/commodity-research-system.git',
        'https://github.com/903798404-hub/commodity-research-system.git'), 'Unexpected producer origin')
    return {'commit': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}')}


def file_set(root):
    root = no_links(root)
    result = {}
    for path in sorted(root.rglob('*')):
        no_links(path)
        if path.is_file():
            require(path.stat().st_nlink == 1, 'Hard-linked asset forbidden')
            result[path.relative_to(root).as_posix()] = {'sha256': sha_file(path), 'size_bytes': path.stat().st_size}
        else:
            require(path.is_dir(), 'Special asset file forbidden')
    return result


def protected_tree(root):
    protected(root)
    for path in Path(root).rglob('*'):
        protected(path)


def approval_document(value):
    contract(value, 'approval')
    require(timestamp(value['approved_at']) <= datetime.now(timezone.utc), 'Future approval forbidden')
    scope = value['scope']
    for key in ('runtime_id', 'release_id'):
        require(re.fullmatch('[a-z0-9][a-z0-9-]{2,100}', scope[key]) is not None, 'Invalid runtime/release ID')
    require(scope['candidate_path'] != scope['formal_path'], 'Candidate cannot be formal destination')
    require('preview' not in scope['candidate_path'].lower() and 'preview' not in scope['formal_path'].lower(), 'Preview path rejected')
    if value['state'] == 'asset_approved':
        digest(value['asset_manifest_sha256']); digest(value['prior_approval_sha256'])
    else:
        require(value['asset_manifest_sha256'] is None and value['prior_approval_sha256'] is None, 'Initial approval cannot approve unknown asset')
        expected = 'historical_initialization_approved' if scope['kind'] == 'historical' else 'cnf_extraction_approved'
        require(value['state'] == expected, 'Approval state/kind mismatch')
    return value


def load_approval(path):
    protected(under(path, AUTHORITY_ROOT))
    value = approval_document(read_json(path))
    scope = value['scope']
    protected(under(scope['candidate_path'], CANDIDATE_ROOT), exists=False)
    protected(under(scope['formal_path'], FORMAL_ROOT), exists=False)
    require(scope['producer'] == git_identity(), 'Approved producer identity mismatch')
    return value


def source_facts(source):
    require(set(source) == {'runtime_root', 'source_manifest_path', 'source_manifest_sha256',
                           'release_manifest_sha256', 'index_sha256', 'payload_sha256'}, 'Unknown historical source fields')
    root = no_links(source['runtime_root'])
    index_path = no_links(root / 'release_index.json')
    require(sha_file(index_path) == digest(source['index_sha256']), 'Source index SHA mismatch')
    index = read_json(index_path)
    release = index['current_release_id']
    require(re.fullmatch('[a-z0-9][a-z0-9-]{2,100}', release) is not None, 'Invalid source release ID')
    release_dir = no_links(root / 'releases' / release)
    manifest_path = no_links(release_dir / 'manifest.json')
    require(sha_file(manifest_path) == digest(source['release_manifest_sha256']), 'Source release manifest SHA mismatch')
    require(index['current_manifest_sha256'].lower() == source['release_manifest_sha256'], 'Source index binding mismatch')
    original = read_json(manifest_path)
    require(original['release_id'] == release and original['generation'] == index['generation']
            and original['parent_release_id'] == index['previous_release_id'], 'Source release/index header mismatch')
    stage_manifest = no_links(source['source_manifest_path'])
    require(sha_file(stage_manifest) == digest(source['source_manifest_sha256']), 'Source initialization manifest SHA mismatch')
    require(original['source_candidate']['manifest']['sha256'].lower() == source['source_manifest_sha256'], 'Stage source binding mismatch')
    require(set(source['payload_sha256']) == set(PAYLOAD_NAMES), 'Exact four input payloads required')
    require(not original['manual_cnf_exists'] and original['manual_cnf_record_count'] == 0
            and original['manual_cnf_sha256'] is None, 'Unexpected manual CNF payload')
    require(set(original['output_files']) == set(PAYLOAD_NAMES), 'Unexpected source outputs')
    observed = {}
    for name in PAYLOAD_NAMES:
        path = no_links(release_dir / name)
        actual = sha_file(path)
        require(actual == digest(source['payload_sha256'][name]) == original['output_files'][name]['sha256'].lower(), 'Source payload SHA mismatch')
        require(path.stat().st_size == original['output_files'][name]['size_bytes'], 'Source payload size mismatch')
        observed[name] = actual
    return {'index_sha256': sha_file(index_path), 'release_manifest_sha256': sha_file(manifest_path),
            'source_manifest_sha256': sha_file(stage_manifest), 'payload_sha256': observed}, original, release_dir


def marker(scope, generated_at):
    return {'schema_version': 1, 'runtime_id': scope['runtime_id'], 'classification': 'formal',
            'module_id': 'import-profit' if scope['kind'] == 'historical' else 'production-historical-cnf',
            'created_at': generated_at}


def initialize_history_files(scope, generated_at):
    """Byte-copy only. Called after host authorization; also exercised with local fixtures."""
    candidate = Path(scope['candidate_path'])
    before, original, source_dir = source_facts(scope['source'])
    candidate.mkdir(mode=0o755)
    release_dir = candidate / 'releases' / scope['release_id']
    release_dir.mkdir(parents=True, mode=0o755)
    for name in PAYLOAD_NAMES:
        write_exclusive(release_dir / name, (source_dir / name).read_bytes())
    release = dict(original)
    release.update(release_id=scope['release_id'], parent_release_id=None, generation=1,
                   release_reason='formal_historical_initialization', created_at=generated_at)
    write_json(release_dir / 'manifest.json', release)
    index = {'schema_version': original['schema_version'], 'generation': 1,
             'current_release_id': scope['release_id'], 'previous_release_id': None,
             'updated_at': generated_at, 'index_reason': 'formal_historical_initialization',
             'current_manifest_sha256': sha_file(release_dir / 'manifest.json').upper()}
    write_json(candidate / 'release_index.json', index)
    write_json(candidate / MARKER, marker(scope, generated_at))
    after, _, _ = source_facts(scope['source'])
    require(before == after, 'Initialization source changed')
    for name in PAYLOAD_NAMES:
        require(sha_file(release_dir / name) == before['payload_sha256'][name], 'Copied historical bytes differ')
    return {'manifest_sha256': sha_file(release_dir / 'manifest.json'),
            'index_sha256': sha_file(candidate / 'release_index.json'),
            'source_bytes_mutated': False, 'historical_values_recalculated': False,
            'record_count': original['record_count'], 'date_range': original['date_range']}


def cnf_source_contract(source):
    require(source == {'identity': SOURCE_IDENTITY, 'database': 'quanyong', 'schema': 'market',
                       'table': 'soybean_param', 'field': 'cnf', 'query_sha256': sha_bytes(QUERY.encode()),
                       'query_time_range': 'all_available_at_extraction'}, 'Unapproved CNF source/query scope')


def canonical_rows(rows):
    def encode(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Decimal):
            require(value.is_finite(), 'Non-finite CNF value')
            # Parquet may pad a decimal to the column's scale; compare its exact
            # numeric value without float conversion or context-dependent rounding.
            text = format(value, 'f')
            if '.' in text:
                text = text.rstrip('0').rstrip('.')
            return 'decimal:' + ('0' if value == 0 else text)
        raise AssetError('Unsupported database value')
    try:
        return sha_bytes(json.dumps(rows, ensure_ascii=False, sort_keys=True, default=encode, allow_nan=False).encode('utf-8'))
    except (ValueError, TypeError):
        raise AssetError('Unsupported or non-finite source values') from None


def cnf_frame(rows):
    import pandas as pd
    require(bool(rows), 'Empty production CNF extraction')
    expected = {'trade_date', 'region_hex', 'month', 'cnf', 'updated_at'}
    require(all(set(row) == expected for row in rows), 'Unexpected query output schema')
    frame = pd.DataFrame(rows)
    frame['region'] = frame['region_hex'].map({k: v[0] for k, v in ORIGINS.items()})
    frame['origin'] = frame['region_hex'].map({k: v[1] for k, v in ORIGINS.items()})
    frame['source_identity'] = SOURCE_IDENTITY
    require(not frame[['trade_date', 'origin', 'month']].isna().any().any(), 'Null or unknown natural key')
    require(not frame.duplicated(['trade_date', 'origin', 'month']).any(), 'Duplicate natural key')
    require(all(type(v) is date for v in frame['trade_date']), 'trade_date must be a database date')
    require(all((type(row['month']) is int or (type(row['month']) is str and row['month']))
                for row in rows), 'Invalid month key')
    require(all(row['updated_at'] is None or isinstance(row['updated_at'], datetime) for row in rows), 'Invalid source updated_at')
    require(all(row['cnf'] is None or type(row['cnf']) in (int, float, Decimal) for row in rows), 'Invalid nullable CNF value')
    canonical_rows(rows)  # Reject non-finite values before any output is written.
    # No filling, interpolation, numeric casting, timestamp replacement or deduplication.
    return frame


def inspect_cnf(path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pq.read_table(path)
    expected = ['trade_date', 'region_hex', 'month', 'cnf', 'updated_at', 'region', 'origin', 'source_identity']
    require(table.column_names == expected, 'Cache column contract mismatch')
    rows = table.to_pylist()
    require(bool(rows), 'Empty production CNF cache')
    source_rows = [{k: row[k] for k in expected[:5]} for row in rows]
    cnf_frame(source_rows)
    for row in rows:
        require((row['region'], row['origin']) == ORIGINS.get(row['region_hex']), 'Origin mapping mismatch')
        require(row['source_identity'] == SOURCE_IDENTITY, 'Preview/foreign source identity')
    dtype = table.schema.field('cnf').type
    require(pa.types.is_decimal(dtype) or pa.types.is_floating(dtype) or pa.types.is_integer(dtype) or pa.types.is_null(dtype), 'CNF must remain nullable numeric')
    require(pa.types.is_timestamp(table.schema.field('updated_at').type) or pa.types.is_null(table.schema.field('updated_at').type), 'updated_at schema mismatch')
    schema_text = str(table.schema.remove_metadata())
    return {'record_count': len(rows), 'date_range': [min(r['trade_date'] for r in rows).isoformat(), max(r['trade_date'] for r in rows).isoformat()],
            'schema_fingerprint': sha_bytes(schema_text.encode()), 'cache_sha256': sha_file(path),
            'cache_size_bytes': Path(path).stat().st_size, 'source_rows_sha256': canonical_rows(source_rows),
            'null_cnf_count': sum(r['cnf'] is None for r in rows), 'zero_cnf_count': sum(r['cnf'] == 0 for r in rows),
            'null_updated_at_count': sum(r['updated_at'] is None for r in rows)}


def extract_cnf(secret_file, output):
    import psycopg
    from psycopg.rows import dict_row
    sys.path.insert(0, '/app/03_src')
    from agri_research_agent.data_sources.tankan.client import TankanConnectionSettings
    settings = TankanConnectionSettings.from_secret_file(secret_file)
    started = utc_now()
    try:
        require(settings.database == 'quanyong', 'Actual configured database mismatch')
        with psycopg.connect(host=settings.host, port=settings.port, dbname=settings.database,
                            user=settings.user, password=settings.password, connect_timeout=15,
                            autocommit=False, row_factory=dict_row,
                            options='-c default_transaction_read_only=on -c statement_timeout=120000 -c lock_timeout=3000') as connection:
            connection.read_only = True
            with connection.cursor() as cursor:
                cursor.execute('SHOW transaction_read_only')
                proof = next(iter(cursor.fetchone().values()))
                cursor.execute('SHOW default_transaction_read_only')
                default_proof = next(iter(cursor.fetchone().values()))
                require(proof == default_proof == 'on', 'Read-only transaction required')
                cursor.execute("SELECT current_database() AS database, 'market.soybean_param'::regclass::oid AS relation_oid, current_setting('server_version') AS server_version")
                actual = dict(cursor.fetchone())
                require(actual['database'] == 'quanyong', 'Actual database identity mismatch')
                cursor.execute(QUERY)
                rows = cursor.fetchall()
            connection.rollback()
    finally:
        settings.clear_password()
    original_sha = canonical_rows(rows)
    frame = cnf_frame(rows)
    require(not Path(output).exists(), 'Cache output already exists')
    frame.to_parquet(output, index=False)
    metrics = inspect_cnf(output)
    require(metrics['source_rows_sha256'] == original_sha, 'Extracted values changed during Parquet serialization')
    return {**metrics, 'query_started_at': started, 'query_finished_at': utc_now(),
            'actual_database_identity': actual,
            'readonly_transaction': {'transaction_read_only': proof, 'default_transaction_read_only': default_proof, 'rolled_back': True}}


def existing_image(image):
    raw = subprocess.check_output(['docker', 'image', 'inspect', image['image_id']], stderr=subprocess.DEVNULL)
    info = strict_json(raw)[0]
    require(info['Id'] == image['image_id'], 'Existing image ID mismatch')
    labels = info.get('Config', {}).get('Labels', {}) or {}
    require(labels.get('org.opencontainers.image.revision') == image['commit'], 'OCI revision mismatch')
    return info


def run_worker(operation, candidate, image, *, secret=None):
    existing_image(image)
    source = protected(Path(__file__).parent)
    protected_tree(candidate)
    protected(EVIDENCE_ROOT)
    worker_name = 'production-input-' + uuid.uuid4().hex
    for path in (source, candidate):
        require(',' not in str(path), 'Invalid bind path')
    command = ['docker', 'run', '--rm', '--name', worker_name, '--pull', 'never', '--read-only', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '--user', '0:0', '--network', 'bridge' if secret else 'none',
               '--pids-limit', '128', '--memory', '2g', '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m',
               '--mount', f'type=bind,src={source},dst=/opt/production-input-tool,readonly',
               '--mount', f'type=bind,src={candidate},dst=/asset' + ('' if operation == 'extract' else ',readonly')]
    if secret:
        protected(secret)
        require(',' not in str(secret), 'Invalid secret bind path')
        command += ['--mount', f'type=bind,src={secret},dst=/run/secrets/tankan.env,readonly']
    command += ['--entrypoint', 'python', image['image_id'], '-B', '/opt/production-input-tool/asset_tool.py',
                '_worker', '--operation', operation, '--expected-commit', image['commit'], '--expected-tree', image['tree']]
    result = None
    terminal = {'schema_version': 'production-input-worker/1', 'worker_name': worker_name,
                'operation': operation, 'helper_image': image, 'started_at': utc_now()}
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600, check=False)
        terminal.update(exit_code=result.returncode, stdout_sha256=sha_bytes(result.stdout), stderr_sha256=sha_bytes(result.stderr))
    except subprocess.TimeoutExpired:
        terminal.update(exit_code=None, timed_out=True)
        raise AssetError('Asset worker timed out') from None
    finally:
        terminal['finished_at'] = utc_now()
        write_json(EVIDENCE_ROOT / (worker_name + '.json'), terminal)
        # Preserve terminal evidence before removing only this invocation's transient container.
        inspect = subprocess.run(['docker', 'container', 'inspect', worker_name], capture_output=True, timeout=30)
        if inspect.returncode == 0:
            subprocess.run(['docker', 'rm', '-f', worker_name], capture_output=True, timeout=60, check=True)
        listing = subprocess.check_output(['docker', 'container', 'ls', '-aq', '--filter', 'name=^/' + worker_name + '$'], timeout=30)
        require(not listing.strip(), 'Transient asset worker still exists')
    require(result is not None, 'No asset worker result')
    require(result.returncode == 0, f'Isolated asset worker failed ({result.returncode}); no production publication')
    return strict_json(result.stdout)


def expected_payloads(scope):
    if scope['kind'] == 'cnf':
        return {MARKER, CACHE}
    prefix = 'releases/' + scope['release_id'] + '/'
    return {MARKER, 'release_index.json', prefix + 'manifest.json', *(prefix + name for name in PAYLOAD_NAMES)}


def asset_document(scope, initial_sha, generated_at, observations):
    candidate = Path(scope['candidate_path'])
    payloads = file_set(candidate)
    require(set(payloads) == expected_payloads(scope), 'Unexpected candidate files')
    value = {'schema_version': 'production-historical-runtime/1' if scope['kind'] == 'historical' else 'production-historical-cnf-cache/1',
             'kind': scope['kind'], 'runtime_id': scope['runtime_id'], 'release_id': scope['release_id'],
             'generated_at': generated_at, 'producer': scope['producer'], 'helper_image': scope['helper_image'],
             'initial_approval_sha256': initial_sha, 'candidate_path': scope['candidate_path'],
             'intended_formal_path': scope['formal_path'], 'readonly_consumption': True,
             'source': scope['source'], 'observations': observations, 'payloads': payloads}
    contract(value, scope['kind'])
    return value


def validate_asset_files(candidate, initial, initial_sha):
    scope = initial['scope']
    asset = read_json(Path(candidate) / ASSET_MANIFEST)
    contract(asset, scope['kind'])
    require(timestamp(asset['generated_at']) >= timestamp(initial['approved_at']), 'Asset predates approval')
    require(timestamp(asset['generated_at']) <= datetime.now(timezone.utc), 'Asset timestamp is in the future')
    require(len(asset['observations']['date_range']) == 2, 'Exactly two date range bounds required')
    for key in ('kind', 'runtime_id', 'release_id', 'producer', 'helper_image', 'source', 'candidate_path'):
        require(asset[key] == scope[key], 'Asset approval scope mismatch: ' + key)
    require(asset['initial_approval_sha256'] == initial_sha and asset['intended_formal_path'] == scope['formal_path'], 'Asset authority binding mismatch')
    require(asset['readonly_consumption'] is True, 'Production consumption must be readonly')
    files = file_set(candidate)
    require(set(files) == expected_payloads(scope) | {ASSET_MANIFEST}, 'Unexpected asset files')
    del files[ASSET_MANIFEST]
    require(files == asset['payloads'], 'Asset payload identity mismatch')
    require(read_json(Path(candidate) / MARKER) == marker(scope, asset['generated_at']), 'Runtime marker mismatch')
    if scope['kind'] == 'historical':
        facts, _, _ = source_facts(scope['source'])
        prefix = 'releases/' + scope['release_id'] + '/'
        for name in PAYLOAD_NAMES:
            require(files[prefix + name]['sha256'] == facts['payload_sha256'][name], 'Historical bytes changed')
        require(asset['observations']['source_bytes_mutated'] is False and asset['observations']['historical_values_recalculated'] is False, 'Invalid historical policy')
        require(files[prefix + 'manifest.json']['sha256'] == asset['observations']['manifest_sha256']
                and files['release_index.json']['sha256'] == asset['observations']['index_sha256'], 'Historical metadata SHA mismatch')
    else:
        cnf_source_contract(scope['source'])
        observed = asset['observations']
        require(timestamp(initial['approved_at']) <= timestamp(observed['query_started_at'])
                <= timestamp(observed['query_finished_at']) <= datetime.now(timezone.utc), 'Invalid extraction time evidence')
        require(observed['cache_sha256'] == files[CACHE]['sha256']
                and observed['cache_size_bytes'] == files[CACHE]['size_bytes'], 'Cache observation identity mismatch')
    return asset


def create_candidate(approval_path, secret=None):
    initial = load_approval(approval_path)
    require(initial['state'] != 'asset_approved', 'Extraction/initialization approval required')
    scope = initial['scope']; candidate = Path(scope['candidate_path'])
    require(not candidate.exists(), 'Candidate already exists; preserve existing evidence')
    protected(candidate.parent)
    generated = utc_now()
    if scope['kind'] == 'historical':
        require(secret is None, 'Historical initialization never queries a database')
        observations = initialize_history_files(scope, generated)
    else:
        cnf_source_contract(scope['source'])
        require(secret is not None, 'Protected Tankan secret file required')
        candidate.mkdir(mode=0o755)
        observations = run_worker('extract', candidate, scope['helper_image'], secret=Path(secret))
        write_json(candidate / MARKER, marker(scope, generated))
    asset = asset_document(scope, sha_file(approval_path), generated, observations)
    write_json(candidate / ASSET_MANIFEST, asset)
    return {'CANDIDATE_CREATED': True, 'candidate_path': str(candidate), 'asset_manifest_sha256': sha_file(candidate / ASSET_MANIFEST),
            'runtime_id': scope['runtime_id'], 'release_id': scope['release_id'], 'observations': observations}


def validate_candidate(approval_path, report_path):
    initial = load_approval(approval_path)
    require(initial['state'] != 'asset_approved', 'Initial scope approval required')
    scope = initial['scope']; candidate = Path(scope['candidate_path'])
    protected_tree(candidate)
    protected(under(report_path, EVIDENCE_ROOT), exists=False); protected(Path(report_path).parent)
    before = file_set(candidate)
    asset = validate_asset_files(candidate, initial, sha_file(approval_path))
    observed = run_worker('history' if scope['kind'] == 'historical' else 'cnf', candidate, scope['helper_image'])
    if scope['kind'] == 'cnf':
        require(all(asset['observations'].get(k) == v for k, v in observed.items()), 'Cache observations mismatch')
        require(asset['observations']['readonly_transaction'] == {'transaction_read_only': 'on', 'default_transaction_read_only': 'on', 'rolled_back': True}, 'Missing readonly extraction proof')
        require(asset['observations']['actual_database_identity']['database'] == 'quanyong', 'Actual database mismatch')
    else:
        require(observed['record_count'] == asset['observations']['record_count'], 'Historical consumer count mismatch')
    require(before == file_set(candidate), 'Candidate mutated during validation')
    report = {'schema_version': 'production-input-validation/1', 'status': 'PASS', 'validated_at': utc_now(),
              'producer': git_identity(), 'helper_image': scope['helper_image'], 'candidate_path': str(candidate),
              'initial_approval_sha256': sha_file(approval_path), 'files': before, 'observed': observed}
    write_json(report_path, report)
    return report


def publish(approval_path, initial_path, report_path, receipt_path):
    approval = load_approval(approval_path); initial = load_approval(initial_path)
    require(approval['state'] == 'asset_approved' and initial['state'] != 'asset_approved', 'Separate exact asset approval required')
    require(approval['scope'] == initial['scope'], 'Approval scopes differ')
    require(approval['prior_approval_sha256'] == sha_file(initial_path), 'Initial approval chain mismatch')
    scope = initial['scope']; candidate = Path(scope['candidate_path']); formal = Path(scope['formal_path'])
    protected_tree(candidate)
    require(sha_file(candidate / ASSET_MANIFEST) == approval['asset_manifest_sha256'], 'Approved asset SHA mismatch')
    protected(under(report_path, EVIDENCE_ROOT)); report = read_json(report_path)
    require(report['schema_version'] == 'production-input-validation/1' and report['status'] == 'PASS', 'Validated candidate required')
    require(report['candidate_path'] == str(candidate) and report['producer'] == git_identity()
            and report['helper_image'] == scope['helper_image'] and report['initial_approval_sha256'] == sha_file(initial_path), 'Validation report identity mismatch')
    require(timestamp(approval['approved_at']) >= timestamp(report['validated_at']), 'Asset approval must follow validation')
    validate_asset_files(candidate, initial, sha_file(initial_path))
    files = file_set(candidate)
    require(files == report['files'], 'Candidate changed since validation')
    require(not formal.exists(), 'Formal destination already exists; no overwrite')
    protected(formal.parent)
    protected(under(receipt_path, EVIDENCE_ROOT), exists=False); protected(Path(receipt_path).parent)
    require(not Path(receipt_path).exists(), 'Publication receipt already exists')
    staging = formal.parent / ('.publishing-' + uuid.uuid4().hex)
    staging.mkdir(mode=0o755)
    for relative in files:
        output = staging / relative
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        write_exclusive(output, (candidate / relative).read_bytes())
        os.chmod(output, 0o444)
    require(file_set(staging) == files and file_set(candidate) == files, 'Publication bytes differ')
    validate_asset_files(staging, initial, sha_file(initial_path))
    for folder in sorted((p for p in staging.rglob('*') if p.is_dir()), reverse=True):
        os.chmod(folder, 0o555)
    os.chmod(staging, 0o555)
    atomic_directory_publish(staging, formal)
    receipt = {'schema_version': 'production-input-publication/1', 'status': 'PUBLISHED', 'published_at': utc_now(),
               'formal_path': str(formal), 'asset_manifest_sha256': approval['asset_manifest_sha256'],
               'asset_approval_sha256': sha_file(approval_path), 'validation_report_sha256': sha_file(report_path),
               'producer': git_identity(), 'files': files, 'readonly_consumption': True}
    write_json(receipt_path, receipt)
    return receipt


def atomic_directory_publish(staging, formal):
    """Linux atomic no-overwrite publication; failed staging is retained as evidence."""
    import ctypes
    require(sys.platform == 'linux', 'Atomic publication requires Linux renameat2')
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    # AT_FDCWD, RENAME_NOREPLACE: even a concurrent empty destination cannot be replaced.
    require(rename(-100, os.fsencode(staging), -100, os.fsencode(formal), 1) == 0,
            'Atomic publication failed; preserve staging, do not overwrite destination')
    descriptor = os.open(Path(formal).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def worker_main(args):
    release = read_json('/app/RELEASE.json')
    # RELEASE.json field names are the existing application release contract.
    require(release.get('git_commit') == args.expected_commit and release.get('git_tree') == args.expected_tree,
            'Worker application RELEASE identity mismatch')
    if args.operation == 'extract':
        return extract_cnf('/run/secrets/tankan.env', '/asset/' + CACHE)
    if args.operation == 'cnf':
        return inspect_cnf('/asset/' + CACHE)
    sys.path.insert(0, '/app/03_src')
    from agri_research_agent.import_profit.runtime_store import load_runtime_release_dataset
    loaded = load_runtime_release_dataset('/asset')
    return {'record_count': loaded.dataset.business_key_count, 'consumer_compatibility': 'PASS'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create'); create.add_argument('--approval', type=Path, required=True)
    create.add_argument('--secret-file', type=Path)
    validate = commands.add_parser('validate'); validate.add_argument('--approval', type=Path, required=True)
    validate.add_argument('--report', type=Path, required=True)
    publication = commands.add_parser('publish')
    for name in ('approval', 'initial-approval', 'report', 'receipt'):
        publication.add_argument('--' + name, type=Path, required=True)
    worker = commands.add_parser('_worker')
    worker.add_argument('--operation', choices=('extract', 'cnf', 'history'), required=True)
    worker.add_argument('--expected-commit', required=True); worker.add_argument('--expected-tree', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == '_worker': result = worker_main(args)
        elif args.command == 'create': result = create_candidate(args.approval, args.secret_file)
        elif args.command == 'validate': result = validate_candidate(args.approval, args.report)
        else: result = publish(args.approval, args.initial_approval, args.report, args.receipt)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        # Driver errors can contain credentials or DSNs. Never print exception text/traceback.
        print(json.dumps({'PRODUCTION_INPUT_OPERATION': 'FAIL', 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
