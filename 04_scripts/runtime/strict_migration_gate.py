"""Independent admission for one bounded pending-result write, never a downgrade.

No candidate Python is imported. Only literal, allowlisted SQL runs in memory.
This is not an arbitrary migration executor or a production authorization API.
Actual migrations, old-state deletion and unknown effects remain unsupported.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3


PROFILE = 'soybean-pending-reference/1'
SOURCE = '03_src/agri_research_agent/soybean_margin/reference.py'
STORE = '03_src/agri_research_agent/soybean_margin/store.py'
SQL = ('UPDATE quote_submissions SET result_json=?,result_sha256=? '
       'WHERE request_id=? AND result_json IS NULL')
DDL = '''CREATE TABLE IF NOT EXISTS quote_submissions (
 request_id TEXT PRIMARY KEY, business_date TEXT NOT NULL, origin TEXT NOT NULL,
 revision INTEGER NOT NULL, submitted_at TEXT NOT NULL, cnf_json TEXT NOT NULL,
 result_json TEXT, result_sha256 TEXT,
 UNIQUE(business_date,origin,revision));'''
LEGACY_DDL = '''CREATE TABLE IF NOT EXISTS cnf (
 business_date TEXT NOT NULL, origin TEXT NOT NULL, shipment_year INTEGER NOT NULL,
 shipment_month INTEGER NOT NULL, value REAL,
 PRIMARY KEY (business_date, origin, shipment_year, shipment_month));
CREATE TABLE IF NOT EXISTS revisions (
 business_date TEXT NOT NULL, origin TEXT NOT NULL, version INTEGER NOT NULL,
 PRIMARY KEY (business_date, origin));'''
CHECKS = ('pending_exact_request_only', 'completed_result_immutable',
          'missing_request_no_write', 'other_requests_preserved',
          'legacy_cnf_and_revisions_preserved', 'rollback_and_reupgrade_preserved')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def fields(value, expected, name):
    require(type(value) is dict and set(value) == set(expected), 'INVALID_' + name + '_FIELDS')


def literal_ddl(raw):
    tree = ast.parse(raw)
    assignments = [n for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'DDL' for t in n.targets)]
    require(len(assignments) == 1 and isinstance(assignments[0].value, ast.Constant)
            and isinstance(assignments[0].value.value, str), 'NON_LITERAL_MIGRATION_SCHEMA')
    return assignments[0].value.value


def normalized(value):
    return ' '.join(value.split())


def probe_sources(reference_raw, base_store_raw, target_store_raw):
    """Check the specific destructive call and replay its SQLite semantics.

    The remaining application effects still require normal risk Review, signed
    candidate validation, compatibility and live full recovery observations.
    """
    require(normalized(literal_ddl(reference_raw)) == normalized(DDL), 'REFERENCE_SCHEMA_UNSUPPORTED')
    require(normalized(literal_ddl(base_store_raw)) == normalized(LEGACY_DDL)
            and normalized(literal_ddl(target_store_raw)) == normalized(LEGACY_DDL),
            'EXISTING_CNF_SCHEMA_CHANGED')
    tree = ast.parse(reference_raw)
    submits = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'submit']
    require(len(submits) == 1, 'PENDING_WRITER_MISSING')
    submit = submits[0]
    writes = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and n.func.attr in ('execute', 'executemany', 'executescript')
              and any(isinstance(v, ast.Constant) and isinstance(v.value, str)
                      and normalized(v.value).upper().startswith(('UPDATE ', 'DELETE ', 'DROP ', 'ALTER ', 'TRUNCATE '))
                      for v in ast.walk(n))]
    expected = ast.parse("db.execute(" + repr(SQL) + ", (raw, hashlib.sha256(raw.encode('utf-8')).hexdigest(), submission['request_id']))", mode='eval').body
    require(len(writes) == 1 and ast.dump(writes[0]) == ast.dump(expected), 'PENDING_WRITE_NOT_BOUNDED')
    assignment = ast.parse('changed = ' + ast.unparse(expected) + '.rowcount').body[0]
    guard = ast.parse("if changed != 1:\n raise ValueError('参考行情请求已完成或不存在')").body[0]
    transaction = ast.parse('with sqlite3.connect(path, timeout=10) as db:\n pass').body[0]
    matches = [n for n in ast.walk(submit) if isinstance(n, ast.With)
               and ast.dump(n.items[0]) == ast.dump(transaction.items[0])
               and len(n.items) == 1 and len(n.body) == 2
               and ast.dump(n.body[0]) == ast.dump(assignment) and ast.dump(n.body[1]) == ast.dump(guard)]
    require(len(matches) == 1, 'PENDING_TRANSACTION_OR_CAS_GUARD_CHANGED')
    # All databases here are task-owned in-memory fixtures, never host paths.
    with sqlite3.connect(':memory:') as db:
        db.executescript(LEGACY_DDL + DDL)
        db.execute('INSERT INTO cnf VALUES (?,?,?,?,?)', ('2026-10-12', 'brazil', 2026, 10, 0))
        db.execute('INSERT INTO cnf VALUES (?,?,?,?,?)', ('2026-10-12', 'brazil', 2026, 11, None))
        db.execute('INSERT INTO revisions VALUES (?,?,?)', ('2026-10-12', 'brazil', 7))
        for request, revision, result in (('done', 1, 'original'), ('pending', 2, None), ('other', 3, None)):
            db.execute('INSERT INTO quote_submissions VALUES (?,?,?,?,?,?,?,?)',
                       (request, '2026-10-12', 'brazil', revision, 'fixture', '{}', result, 'old' if result else None))
        before = db.execute('SELECT * FROM quote_submissions ORDER BY request_id').fetchall()
        legacy = (db.execute('SELECT * FROM cnf').fetchall(), db.execute('SELECT * FROM revisions').fetchall())
        require(db.execute(SQL, ('new', 'new-sha', 'pending')).rowcount == 1, 'PENDING_WRITE_FAILED')
        after = db.execute('SELECT * FROM quote_submissions ORDER BY request_id').fetchall()
        require([r for r in before if r[0] != 'pending'] == [r for r in after if r[0] != 'pending'], 'OTHER_REQUEST_CHANGED')
        pending_before, pending_after = next(r for r in before if r[0] == 'pending'), next(r for r in after if r[0] == 'pending')
        require(pending_before[:6] == pending_after[:6] and pending_after[6:] == ('new', 'new-sha'), 'REQUEST_IDENTITY_CHANGED')
        for request in ('done', 'pending', 'missing'):
            require(db.execute(SQL, ('overwrite', 'bad', request)).rowcount == 0, 'COMPLETED_OR_MISSING_WRITE_ALLOWED')
        require(after == db.execute('SELECT * FROM quote_submissions ORDER BY request_id').fetchall(), 'RETRY_CHANGED_RESULT')
        require(legacy == (db.execute('SELECT * FROM cnf').fetchall(), db.execute('SELECT * FROM revisions').fetchall()), 'LEGACY_STATE_CHANGED')
        # Old-compatible connection can read the old schema and retain the new
        # table through backup/reopen. This is a SQLite compatibility proof,
        # explicitly not a substitute for the actual signed runtime recovery.
        with sqlite3.connect(':memory:') as restored:
            db.commit()
            db.backup(restored)
            require(legacy == (restored.execute('SELECT * FROM cnf').fetchall(), restored.execute('SELECT * FROM revisions').fetchall()), 'ROLLBACK_LEGACY_UNREADABLE')
            require(after == restored.execute('SELECT * FROM quote_submissions ORDER BY request_id').fetchall(), 'REUPGRADE_RESULT_CHANGED')
    return {'profile': PROFILE, 'checks': dict.fromkeys(CHECKS, 'PASS'),
            'source_sha256': {SOURCE: hashlib.sha256(reference_raw).hexdigest(),
                              STORE: hashlib.sha256(target_store_raw).hexdigest()},
            'sql_sha256': hashlib.sha256(SQL.encode()).hexdigest(),
            'probe_scope': 'ALLOWLISTED_SQL_IN_MEMORY_NOT_ACTUAL_RUNTIME_REHEARSAL'}


def prepare_binding(request, risk, *, tool, main, candidate_image_id, probe):
    return dict(profile=PROFILE, project_id=request['project_id'], base=risk['base'], target=risk['target'],
                tool=tool, authoritative_main=main, candidate_image_id=candidate_image_id,
                release_inputs_sha256=digest({k: v for k, v in request.items() if k != 'migration_gate'}),
                machine_findings_sha256=digest({k: risk[k] for k in ('MACHINE_DESTRUCTIVE_EVIDENCE',
                    'MACHINE_STATE_CHANGE_CLASS', 'DESTRUCTIVE_FINDINGS', 'findings', 'REVERSIBILITY_EVIDENCE')}),
                state_plan=request['state_plan'], candidate_record=request['candidate_record'],
                current_runtime_config=request['current']['runtime_config'], current_data_schema=request['current']['data_schema'],
                recovery_evidence=request['recovery_evidence'], acceptance_plan=request['acceptance_plan'], probe=probe)


def validate_approval(approval, binding, *, now=None):
    fields(approval, ('schema_version', 'binding', 'decision', 'reviewer', 'timestamp', 'reason'), 'MIGRATION_APPROVAL')
    require(approval['schema_version'] == 'strict-migration-approval/1', 'MIGRATION_APPROVAL_SCHEMA')
    require(approval['binding'] == binding, 'MIGRATION_APPROVAL_BINDING_CHANGED')
    require(approval['decision'] == 'APPROVE_BOUNDED_PENDING_RESULT', 'SEPARATE_MIGRATION_APPROVAL_REQUIRED')
    for name in ('reviewer', 'reason'):
        require(type(approval[name]) is str and approval[name].strip(), 'MIGRATION_REVIEW_IDENTITY_REQUIRED')
    require(type(approval['timestamp']) is str, 'MIGRATION_APPROVAL_TIME')
    observed = datetime.fromisoformat(approval['timestamp'].replace('Z', '+00:00'))
    current = now or datetime.now(timezone.utc)
    require(observed.tzinfo is not None and timedelta(0) <= current - observed <= timedelta(hours=24), 'MIGRATION_APPROVAL_EXPIRED')
    return dict(schema_version='strict-migration-validation/1', profile=PROFILE, status='PASS',
                binding_sha256=digest(binding), checks=dict.fromkeys(CHECKS, 'PASS'),
                production_authorized=False)


def validate_observation(value):
    fields(value, ('schema_version', 'profile', 'status', 'binding_sha256', 'checks', 'production_authorized'), 'MIGRATION_VALIDATION')
    require(value['schema_version'] == 'strict-migration-validation/1' and value['profile'] == PROFILE
            and value['status'] == 'PASS' and value['production_authorized'] is False,
            'UNSUPPORTED_MIGRATION_VALIDATION')
    require(type(value['binding_sha256']) is str and len(value['binding_sha256']) == 64
            and all(c in '0123456789abcdef' for c in value['binding_sha256']), 'MIGRATION_BINDING_DIGEST')
    require(type(value['checks']) is dict and value['checks'] == dict.fromkeys(CHECKS, 'PASS'), 'MIGRATION_CHECKS_INCOMPLETE')


def verify(pre, request, risk, state, host, *, tool, candidate_image_id, rehearsal_validated,
           approval_required=True):
    """Consumed only after the normal signed candidate and live recovery checks."""
    require(risk.get('MACHINE_DESTRUCTIVE_EVIDENCE') is True
            and risk.get('FINAL_RELEASE_TREATMENT') == 'HIGH_RISK', 'MIGRATION_REQUIRES_UNREDUCED_HIGH_RISK_REVIEW')
    require(risk['DESTRUCTIVE_FINDINGS'] == [dict(path=SOURCE, operation='db.execute', reason='EXPLICIT_SCHEMA_OR_DATA_MIGRATION')],
            'MIGRATION_DESTRUCTIVE_SCOPE_UNSUPPORTED')
    require(state['irreversible'] == 'NO' and state['compatible'] is True
            and all(state[k] is False for k in ('database_migration', 'production_data_mutation', 'storage_format_change')),
            'ACTUAL_MIGRATION_REQUIRES_ITS_OWN_CONTRACT')
    require(rehearsal_validated is True and request['recovery_evidence'] is not None, 'MIGRATION_FULL_RECOVERY_REQUIRED')
    if approval_required:
        ref = request['migration_gate']
        existing = [request[k] for k in ('candidate_record', 'state_plan', 'acceptance_plan', 'recovery_evidence')]
        require(type(ref) is dict and not any(ref.get('path') == r['path'] for r in existing), 'INDEPENDENT_MIGRATION_APPROVAL_REQUIRED')
        # Protected, hash-bound operator approval. No new signer, no copied PASS.
        raw = pre._risk_file(ref, host)
        host._protected_path(pre.Path(ref['path']), private=True)
        approval = host._json(raw)
    main = pre.current_main_identity(pre.ROOT)
    def source(commit, name):
        return pre._risk_git(pre.ROOT, 'show', commit + ':' + name)
    probe = probe_sources(source(risk['target']['commit'], SOURCE), source(risk['base']['commit'], STORE),
                          source(risk['target']['commit'], STORE))
    binding = prepare_binding(request, risk, tool=tool, main=main, candidate_image_id=candidate_image_id, probe=probe)
    if not approval_required:
        require(pre.current_main_identity(pre.ROOT) == main, 'MIGRATION_INPUT_CHANGED_DURING_PROPOSAL')
        return dict(schema_version='strict-migration-approval/1', binding=binding, decision='PENDING',
                    reviewer='', timestamp=None, reason=''), main
    observation = validate_approval(approval, binding)
    require(pre._risk_file(ref, host) == raw and pre.current_main_identity(pre.ROOT) == main,
            'MIGRATION_INPUT_CHANGED_DURING_VALIDATION')
    return observation, main
