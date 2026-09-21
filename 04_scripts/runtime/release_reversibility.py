"""Bounded code/Compose proof for additive stores; never execute candidate code.

This intentionally does not prove arbitrary Python. Supported additions use literal
Path roots, direct file I/O and unchanged existing readers. Opaque storage adapters,
authorization/lifecycle changes and unanalysed effects need Maintainer review.
Optional recovery observations do not gate a proven additive reversible release.
"""
from __future__ import annotations

import ast
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import PurePosixPath
import re

import yaml


class Unproven(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise Unproven(reason)


def overlap(a, b):
    a, b = PurePosixPath(a), PurePosixPath(b)
    return a == b or a in b.parents or b in a.parents


def path(value):
    require(isinstance(value, str) and value.startswith('/') and
            not any(p in value for p in ('..', '\\', '${', '//')), 'UNRESOLVED_PATH')
    return value


def requirements(risk_class, state_class, final_treatment=None):
    routine = (final_treatment or risk_class) == 'ROUTINE_STATELESS'
    full = not routine and state_class != 'ADDITIVE_REVERSIBLE'
    return dict(FULL_ROLLBACK_REHEARSAL_REQUIRED=full,
                TARGETED_RECOVERY_VALIDATION_REQUIRED=full,
                # Legacy name continues to mean full rehearsal, not targeted.
                ROLLBACK_REHEARSAL_REQUIRED=full,
                ROUTINE_RELEASE_ELIGIBLE=routine)


def machine_findings(risk):
    """Review binds the full machine observations, not just a PASS label."""
    return {k: copy.deepcopy(risk[k]) for k in ('MACHINE_DESTRUCTIVE_EVIDENCE',
            'MACHINE_STATE_CHANGE_CLASS', 'DESTRUCTIVE_FINDINGS', 'findings', 'REVERSIBILITY_EVIDENCE')}


ROUTINE_SEMANTIC_FACTS = (
    'PERSISTENT_STATE_DELTA', 'WRITE_PATH_DELTA', 'STORAGE_CONTRACT_DELTA',
    'SCHEMA_MIGRATION', 'DATA_MIGRATION', 'MOUNT_DELTA',
    'DEPLOYMENT_INFRA_DELTA', 'NETWORK_INFRA_DELTA',
    'DESTRUCTIVE_WRITE_SEMANTICS', 'REVERSE_MIGRATION_REQUIRED',
    'UNRESOLVED_STATEFUL_BEHAVIOR',
)


def reviewed_diff_identity(risk):
    """Bind a review to the exact Git identities and machine observations."""
    payload = {
        'base': risk['base'], 'target': risk['target'],
        'project_id': risk.get('project_id'),
        'build_contract_sha256': risk.get('build_contract_sha256'),
        'machine_findings': machine_findings(risk),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def machine_classification(risk):
    if risk['RELEASE_RISK_CLASS'] == 'ROUTINE_STATELESS':
        return 'ROUTINE_STATELESS'
    return risk['MACHINE_STATE_CHANGE_CLASS']


def _require_routine_resolution(risk, review):
    require(risk['MACHINE_DESTRUCTIVE_EVIDENCE'] is False and not risk['DESTRUCTIVE_FINDINGS'],
            'MACHINE_DESTRUCTIVE_EVIDENCE_CANNOT_BE_DOWNGRADED')
    require(risk['MACHINE_STATE_CHANGE_CLASS'] == 'NEEDS_MAINTAINER_RISK_REVIEW',
            'MACHINE_CLASSIFICATION_NOT_REVIEWABLE_AS_ROUTINE')
    require(risk.get('build_projection_complete') is True,
            'RUNTIME_EFFECT_CLOSURE_INCOMPLETE')
    require(not risk['REVERSIBILITY_EVIDENCE'].get('new_roots'),
            'PERSISTENT_STATE_DELTA_CANNOT_BE_ROUTINE')
    blocked_reasons = {
        'STATE_OR_STORAGE_CHANGE', 'DEPLOYED_INFRASTRUCTURE_CHANGE',
        'RUNTIME_ACTIVE_INFRASTRUCTURE_CHANGE',
    }
    require(not any(f.get('high_risk') and f.get('reason') in blocked_reasons
                    for f in risk['findings']), 'STATEFUL_OR_INFRA_DELTA_CANNOT_BE_ROUTINE')
    require(review.get('machine_classification') == machine_classification(risk)
            and review.get('machine_destructive_evidence') is False,
            'RISK_REVIEW_MACHINE_CLASSIFICATION_CHANGED')
    require(review.get('reviewed_diff_identity') == reviewed_diff_identity(risk),
            'RISK_REVIEW_DIFF_IDENTITY_CHANGED')
    facts = review.get('semantic_delta')
    require(type(facts) is dict and set(facts) == set(ROUTINE_SEMANTIC_FACTS),
            'ROUTINE_SEMANTIC_FACTS_INCOMPLETE')
    require(all(facts[name] == 'NO' for name in ROUTINE_SEMANTIC_FACTS),
            'STATEFUL_DELTA_CANNOT_BE_DOWNGRADED_TO_ROUTINE')


def apply_review(risk, review, authoritative_main, *, now=None):
    """An ordinary Maintainer assessment, not an execution grant or CI bypass.

    The release operator supplies this record via the existing protected request.
    There is deliberately no signature, token, service, or new approver identity.
    """
    if risk['RELEASE_RISK_CLASS'] == 'ROUTINE_STATELESS':
        require(review is None, 'ROUTINE_DOES_NOT_REQUIRE_RISK_REVIEW')
        return {**risk, 'MACHINE_CLASSIFICATION': 'ROUTINE_STATELESS',
                'MAINTAINER_RELEASE_TREATMENT': None,
                'FINAL_RELEASE_TREATMENT': 'ROUTINE_STATELESS',
                **requirements(risk['RELEASE_RISK_CLASS'], risk['STATE_CHANGE_CLASS'], 'ROUTINE_STATELESS')}
    legacy_fields = {'reviewer', 'timestamp', 'base', 'target', 'authoritative_main',
                     'machine_findings', 'maintainer_classification', 'reason'}
    routine_fields = legacy_fields | {'machine_classification', 'machine_destructive_evidence',
                                      'reviewed_diff_identity', 'semantic_delta'}
    require(type(review) is dict and set(review) in (legacy_fields, routine_fields),
            'INVALID_RISK_REVIEW_FIELDS')
    for key in ('reviewer', 'reason'):
        require(isinstance(review[key], str) and bool(review[key].strip()), 'RISK_REVIEW_IDENTITY_OR_REASON_MISSING')
    require(review['base'] == risk['base'] and review['target'] == risk['target'] and
            review['authoritative_main'] == authoritative_main, 'RISK_REVIEW_IDENTITY_CHANGED')
    require(review['machine_findings'] == machine_findings(risk), 'RISK_REVIEW_MACHINE_FINDINGS_CHANGED')
    require(isinstance(review['timestamp'], str), 'INVALID_RISK_REVIEW_TIME')
    observed = datetime.fromisoformat(review['timestamp'].replace('Z', '+00:00'))
    current = now or datetime.now(timezone.utc)
    require(observed.tzinfo is not None and current - timedelta(hours=24) <= observed <= current,
            'INVALID_RISK_REVIEW_TIME')
    decision = review['maintainer_classification']
    require(decision in ('ROUTINE_STATELESS', 'ADDITIVE_REVERSIBLE', 'HIGH_RISK'),
            'INVALID_MAINTAINER_CLASSIFICATION')
    if decision == 'ROUTINE_STATELESS':
        require(set(review) == routine_fields, 'ROUTINE_REVIEW_REQUIRES_SEMANTIC_FACTS')
        _require_routine_resolution(risk, review)
    require(not risk['MACHINE_DESTRUCTIVE_EVIDENCE'] or decision == 'HIGH_RISK', 'MACHINE_DESTRUCTIVE_EVIDENCE_CANNOT_BE_DOWNGRADED')
    effective = ('IRREVERSIBLE_OR_DESTRUCTIVE' if risk['MACHINE_DESTRUCTIVE_EVIDENCE'] else
                 'NOT_APPLICABLE' if decision == 'ROUTINE_STATELESS' else decision)
    return {**risk, 'STATE_CHANGE_CLASS': effective, 'MAINTAINER_STATE_CHANGE_CLASS': decision,
            'MACHINE_CLASSIFICATION': machine_classification(risk),
            'MAINTAINER_RELEASE_TREATMENT': decision,
            'FINAL_RELEASE_TREATMENT': decision,
            'maintainer_risk_review': copy.deepcopy(review),
            **requirements(risk['RELEASE_RISK_CLASS'], effective, decision)}


def destructive_findings(before, after, contract_path, findings):
    """Collect positive evidence separately from unsupported semantics.

    This is not a complete absence-of-destruction proof. Unknown custom calls
    are reviewed; explicit changed persistent assets, migration SQL and direct
    destructive calls against old literal state paths cannot be reviewed away.
    """
    result = []
    old = json.loads(before[contract_path])
    roots = [r['container_path'] for r in old.get('runtime_roots', [])
             if r['role'] != old.get('identity_root_role')]
    sql = re.compile(r'\b(?:ALTER\s+TABLE|DROP\s+(?:TABLE|DATABASE|SCHEMA)|TRUNCATE\s+TABLE|DELETE\s+FROM|UPDATE\s+\w+\s+SET)\b', re.I)
    for finding in findings:
        name = finding['path']
        if name.startswith(('07_docs/', '08_tests/')) or name.endswith('.md'):
            continue
        prior, current = before.get(name), after.get(name)
        persistent = name.startswith(('01_data/', '06_outputs/', '10_logs/')) or name.endswith(('.db', '.sqlite', '.parquet'))
        if persistent and prior is not None and prior != current:
            result.append(dict(path=name, reason='EXISTING_PERSISTENT_ASSET_CHANGED_OR_DELETED'))
        if name.endswith('.sql'):
            matches = set(sql.findall((current or b'').decode('utf-8'))) - set(sql.findall((prior or b'').decode('utf-8')))
            if matches:
                result.append(dict(path=name, reason='EXPLICIT_SCHEMA_OR_DATA_MIGRATION', operations=sorted(matches)))
        if not name.endswith('.py') or not finding['high_risk'] or current is None:
            continue
        try:
            old_calls = {ast.dump(n) for n in ast.walk(ast.parse(prior or b'')) if isinstance(n, ast.Call)}
            for node in ast.walk(ast.parse(current)):
                if not isinstance(node, ast.Call) or ast.dump(node) in old_calls:
                    continue
                literals = [v.value for v in ast.walk(node) if isinstance(v, ast.Constant) and isinstance(v.value, str)]
                operation = ast.unparse(node.func)
                if operation.rsplit('.', 1)[-1] in {'execute', 'executemany', 'executescript'} and any(sql.search(v) for v in literals):
                    result.append(dict(path=name, reason='EXPLICIT_SCHEMA_OR_DATA_MIGRATION', operation=operation))
                elif operation.rsplit('.', 1)[-1] in {'unlink','remove','rmtree','replace','rename','write_text','write_bytes','to_parquet','to_csv'}:
                    touched = [v for v in literals if v.startswith('/') and any(overlap(v, r) for r in roots)]
                    if touched:
                        result.append(dict(path=name, reason='DIRECT_OLD_STATE_MUTATION', operation=operation, paths=touched))
        except SyntaxError:
            pass  # Unsupported code is not fabricated destructive evidence.
    return result


def _mount_delta(old, new):
    """Preserve all old definitions; new roots must be disjoint except identity."""
    for key in old.keys() | new.keys():
        if key not in {'runtime_roots', 'required_mounts', 'required_environment',
                       'environment_bindings', 'source_inputs', 'forbidden_environment'}:
            require(old.get(key) == new.get(key), 'RUNTIME_IDENTITY_OR_LIFECYCLE_CHANGED:' + key)
    old_roots = {r['role']: r for r in old['runtime_roots']}
    new_roots = {r['role']: r for r in new['runtime_roots']}
    require(len(new_roots) == len(new['runtime_roots']), 'DUPLICATE_ROOT_ROLE')
    require(all(new_roots.get(k) == v for k, v in old_roots.items()), 'EXISTING_ROOT_CHANGED')
    additions = [v for k, v in new_roots.items() if k not in old_roots]
    require(bool(additions), 'NO_NEW_INDEPENDENT_STATE_ROOT')
    for r in additions:
        require(r['access'] == 'rw', 'NEW_ROOT_NOT_SCOPED_RW')
        path(r['container_path'])
        for other in new['runtime_roots']:
            if other == r or other['role'] == old.get('identity_root_role'):
                continue
            require(not overlap(r['container_path'], other['container_path']), 'STATE_ROOT_OVERLAP')
    old_mounts = {r['role']: r for r in old['required_mounts']}
    new_mounts = {r['role']: r for r in new['required_mounts']}
    require(len(new_mounts) == len(new['required_mounts']), 'DUPLICATE_MOUNT_ROLE')
    expected = dict(old_mounts, **{r['role']: dict(role=r['role'],
                    container_path=r['container_path'], read_only=False) for r in additions})
    require(new_mounts == expected, 'EXISTING_MOUNT_CONTRACT_CHANGED')
    return additions


def _compose_delta(before, after, old, new, additions):
    sources = set()
    added_env = {}
    targets = {r['container_path'] for r in additions}
    for filename in old['build']['compose_sources']:
        a, b = yaml.safe_load(before[filename]), yaml.safe_load(after[filename])
        normalized = copy.deepcopy(b)
        require(a.keys() == b.keys() and a['services'].keys() == b['services'].keys(), 'SERVICE_TOPOLOGY_CHANGED')
        for service, original in a['services'].items():
            current = normalized['services'][service]
            if service != old['service_id']:
                require(original == current, 'OTHER_SERVICE_CHANGED')
                continue
            old_volumes = original.get('volumes', [])
            extra = [m for m in current.get('volumes', []) if m not in old_volumes]
            require(all(m in current.get('volumes', []) for m in old_volumes), 'EXISTING_COMPOSE_MOUNT_CHANGED')
            require(len(extra) == len(additions), 'UNDECLARED_ADDED_MOUNT')
            for m in extra:
                require(set(m) == {'type', 'source', 'target', 'read_only', 'bind'} and
                        m['type'] == 'bind' and m['read_only'] is False and
                        m['bind'] == {'create_host_path': False} and m['target'] in targets,
                        'NEW_STATE_NOT_PERSISTENT_EXPLICIT_BIND')
                source = m['source']
                require(isinstance(source, str) and re.fullmatch(r'\$\{[A-Z_]+:\?[^}]+\}', source), 'UNRESOLVED_BIND_SOURCE')
                require(source not in sources and all(source != x.get('source') for x in old_volumes
                        if isinstance(x, dict)), 'NEW_SOURCE_REUSES_OLD_SOURCE')
                sources.add(source)
            require({m['target'] for m in extra} == targets, 'ADDED_MOUNT_TARGET_MISMATCH')
            current['volumes'] = old_volumes
            env, prior = current.get('environment', {}), original.get('environment', {})
            require(isinstance(env, dict) and all(env.get(k) == v for k, v in prior.items()), 'EXISTING_ENVIRONMENT_CHANGED')
            added_env.update({k: v for k, v in env.items() if k not in prior})
            current['environment'] = prior
        require(normalized == a, 'COMPOSE_NONADDITIVE_CHANGE')
    bindings = {x['name']: x for x in new.get('environment_bindings', [])}
    for k, value in added_env.items():
        b = bindings.get(k, {})
        require(b.get('kind') == 'runtime_path' and b.get('role') in {r['role'] for r in additions},
                'NEW_ENVIRONMENT_NOT_STATE_PATH:' + k)
        root = next(r['container_path'] for r in additions if r['role'] == b['role'])
        require(path(value) == str(PurePosixPath(root) / b.get('relative_path', '')), 'ENVIRONMENT_PATH_MISMATCH')
    # Bindings cannot silently redirect an existing input or disable protection.
    prior = {x['name']: x for x in old.get('environment_bindings', [])}
    require(all(bindings.get(k) == v for k, v in prior.items()) and
            set(bindings) - set(prior) == set(added_env), 'ENVIRONMENT_BINDINGS_CHANGED')
    require(set(new.get('required_environment', [])) == set(old.get('required_environment', [])) | set(added_env),
            'REQUIRED_ENVIRONMENT_CHANGED')
    require(new.get('forbidden_environment', []) == old.get('forbidden_environment', []), 'AUTHORIZATION_ENVIRONMENT_CHANGED')
    return sorted(sources)


def _source_additions(before, after, old, new):
    a = {x['path']: x for x in old.get('source_inputs', [])}
    b = {x['path']: x for x in new.get('source_inputs', [])}
    require(all(b.get(k) == v for k, v in a.items()), 'EXISTING_SOURCE_CONTRACT_CHANGED')
    for name in b.keys() - a.keys():
        require(name not in before and name in after and b[name]['role'] != 'initialization',
                'NEW_INITIALIZATION_OR_EXISTING_SOURCE_CHANGED')


def _python_additions(old, new, roots):
    """Prove a small direct-I/O language, with unchanged old executable nodes.

    New functions cannot call old functions with new arguments, mutate globals,
    execute at import, dynamically load code or route writes outside added roots.
    Readers of the same file use unchanged Python/Path storage format. This
    restriction deliberately rejects general Parquet/pipeline adapters.
    """
    a, b = ast.parse(old), ast.parse(new)
    dump = lambda n: ast.dump(n, include_attributes=False)
    old_nodes = [dump(n) for n in a.body]
    remaining = list(b.body)
    for n in old_nodes:
        matches = [i for i, value in enumerate(remaining) if dump(value) == n]
        require(bool(matches), 'EXISTING_EXECUTABLE_CHANGED')
        remaining.pop(matches[0])
    names = {n.name for n in a.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    require(all(isinstance(n, ast.FunctionDef) and n.name not in names for n in remaining), 'NEW_IMPORT_OR_MODULE_EFFECT')
    # Path must refer to pathlib's existing import, never a caller-defined alias.
    require(any(isinstance(n, ast.ImportFrom) and n.module == 'pathlib' and
                any(x.name == 'Path' and x.asname is None for x in n.names) for n in a.body), 'PATH_PRIMITIVE_UNBOUND')
    require(not any(isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == 'Path' or
                    isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'Path' for t in n.targets)
                    for n in a.body), 'PATH_PRIMITIVE_SHADOWED')
    written, read = set(), set()
    for fn in remaining:
        require(not fn.decorator_list and not fn.args.defaults and not any(fn.args.kw_defaults), 'FUNCTION_CREATION_EFFECT')
        require(fn.returns is None and all(x.annotation is None for x in
                (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)), 'ANNOTATION_CREATION_EFFECT')
        require('Path' not in {x.arg for x in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)},
                'PATH_PRIMITIVE_SHADOWED')
        require(fn.args.vararg is None and fn.args.kwarg is None, 'UNRESOLVED_FUNCTION_ARGUMENTS')
        values = {}
        def resolve(expr):
            if isinstance(expr, ast.Name):
                require(expr.id in values, 'UNRESOLVED_IO_PATH')
                return values[expr.id]
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == 'Path':
                require(len(expr.args) == 1 and not expr.keywords and isinstance(expr.args[0], ast.Constant), 'DYNAMIC_PATH')
                value = path(expr.args[0].value)
                require(any(PurePosixPath(r) in PurePosixPath(value).parents for r in roots), 'WRITE_OUTSIDE_NEW_STORE')
                return value
            raise Unproven('UNRESOLVED_IO_PATH')
        def expression(expr):
            if isinstance(expr, (ast.Constant, ast.Name)):
                return
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
                name = resolve(expr.func.value)
                require(not expr.keywords and expr.func.attr in ('write_text', 'read_text', 'write_bytes', 'read_bytes'), 'OPAQUE_STORAGE_OPERATION')
                if expr.func.attr.startswith('write'):
                    require(len(expr.args) == 1 and isinstance(expr.args[0], (ast.Name, ast.Constant)), 'UNRESOLVED_WRITE_VALUE')
                    written.add((name, expr.func.attr.removeprefix('write_')))
                else:
                    require(not expr.args, 'UNRESOLVED_READ_OPERATION')
                    read.add((name, expr.func.attr.removeprefix('read_')))
                return
            raise Unproven('UNRESOLVED_EXECUTABLE_EFFECT')
        for stmt in fn.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                key = stmt.targets[0].id
                require(key not in values and key != 'Path', 'IO_PATH_REASSIGNED')
                values[key] = resolve(stmt.value)
            elif isinstance(stmt, (ast.Return, ast.Expr)):
                if stmt.value is not None:
                    expression(stmt.value)
            else:
                raise Unproven('UNRESOLVED_CONTROL_OR_STATE_EFFECT')
    require(written <= read, 'REUPGRADE_READER_UNPROVEN')
    return sorted({v[0] for v in written})


def classify(before, after, contract_path, findings, graphs):
    """Evidence is generated from exact archived trees, never request metadata."""
    state = 'NEEDS_MAINTAINER_RISK_REVIEW'
    evidence = dict(analysis='bounded-additive-store/1', facts=[], new_roots=[], failure_codes=[])
    changes = {f['path']: f for f in findings}
    destructive = destructive_findings(before, after, contract_path, findings)
    if destructive:
        return dict(STATE_CHANGE_CLASS='IRREVERSIBLE_OR_DESTRUCTIVE',
                    MACHINE_STATE_CHANGE_CLASS='IRREVERSIBLE_OR_DESTRUCTIVE',
                    MACHINE_DESTRUCTIVE_EVIDENCE=True, DESTRUCTIVE_FINDINGS=destructive,
                    REVERSIBILITY_EVIDENCE=evidence)
    try:
        require(all(g['complete'] for g in graphs.values()), 'RUNTIME_EFFECT_CLOSURE_INCOMPLETE')
        old, new = json.loads(before[contract_path]), json.loads(after[contract_path])
        additions = _mount_delta(old, new)
        evidence['new_roots'] = additions
        sources = _compose_delta(before, after, old, new, additions)
        _source_additions(before, after, old, new)
        infrastructure = {contract_path, *old['build']['compose_sources']}
        roots = [r['container_path'] for r in additions]
        files = []
        for p, finding in changes.items():
            if p in infrastructure or not finding['high_risk']:
                continue
            require(p.endswith('.py') and not p.startswith(('09_deploy/', '04_scripts/', '03_src/agri_research_agent/shared/')),
                    'AUTHORIZATION_BUILD_OR_LIFECYCLE_UNPROVEN:' + p)
            files.extend(_python_additions(before.get(p, b''), after[p], roots))
        # Old code cannot name/enumerate the new state. Absolute old path literals
        # at a parent of an added root conservatively prevent a proof.
        for p in set(next(iter(graphs.values()))['active_paths']):
            if not p.endswith('.py') or p not in before:
                continue
            for n in ast.walk(ast.parse(before[p])):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith('/'):
                    require(not any(overlap(n.value, r) for r in roots), 'OLD_RUNTIME_MAY_ACCESS_NEW_STATE:' + p)
            # Prove old code is also within the bounded language. Merely not
            # finding literal new paths cannot rule out a dynamic parent delete.
            tree = ast.parse(before[p])
            imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == 'pathlib'
                       and all(x.name == 'Path' and x.asname is None for x in n.names)]
            require(len(imports) == 1, 'OLD_RUNTIME_EFFECTS_UNPROVEN:' + p)
            old_paths = [r['container_path'] for r in old['runtime_roots']]
            _python_additions(ast.unparse(ast.Module(body=imports, type_ignores=[])), before[p], old_paths)
        evidence.update(new_bind_sources=sources, new_files=files,
                        facts=['EXISTING_MOUNTS_UNCHANGED', 'OLD_EXECUTABLE_EFFECTS_UNCHANGED',
                               'NEW_WRITES_SCOPED_TO_ADDED_ROOTS', 'SAME_TARGET_READ_FORMAT_PROVEN',
                               'NO_REVERSE_MIGRATION', 'PERSISTENT_BIND_SOURCES_RETAINED'])
        state = 'ADDITIVE_REVERSIBLE'
    except (Unproven, KeyError, TypeError, ValueError, SyntaxError, yaml.YAMLError) as exc:
        evidence['failure_codes'].append(str(exc))
    return dict(STATE_CHANGE_CLASS=state, MACHINE_STATE_CHANGE_CLASS=state,
                MACHINE_DESTRUCTIVE_EVIDENCE=False, DESTRUCTIVE_FINDINGS=[], REVERSIBILITY_EVIDENCE=evidence)
