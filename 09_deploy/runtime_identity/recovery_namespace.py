"""Bounded host-policy projection for recovery using the existing production domain.

No container-side protocol, signing key, application override or deployment action.
The protected policy binds a retained production policy and a live old instance.
"""
import copy
import hashlib
import os
from pathlib import Path
import re
import stat


SANDBOX_PARENT = Path('/var/lib/market-data/recovery-sandboxes')


def validate_sandbox_declaration(h, r):
    s = r['sandbox']
    require(h, type(s) is dict and set(s) == {'mode', 'root', 'roots'} and
        s['mode'] == 'isolated-rehearsal', 'invalid sandbox declaration')
    require(h, s['root'] == str(SANDBOX_PARENT / r['nonce']), 'sandbox outside approved parent/nonce')
    require(h, type(s['roots']) is list and bool(s['roots']), 'sandbox roots missing')
    sources, destinations = set(), set()
    for item in s['roots']:
        require(h, type(item) is dict and set(item) == {'production_source', 'sandbox_source',
            'initialization', 'uid', 'gid', 'mode'}, 'invalid sandbox root fields')
        h._absolute(item['production_source']); h._absolute(item['sandbox_source'])
        root = Path(item['sandbox_source'])
        require(h, root.parent == Path(s['root']) and re.fullmatch(r'root-[0-9]+', root.name),
            'sandbox root is not a direct declared child')
        require(h, item['initialization'] in {'EMPTY_SANDBOX_OK', 'COPY_CURRENT_STATE_REQUIRED'} and
            all(type(item[k]) is int for k in ('uid', 'gid', 'mode')) and
            item['uid'] > 0 and item['gid'] > 0 and 0 <= item['mode'] <= 0o777 and
            item['mode'] & 0o700 == 0o700 and not item['mode'] & 0o022,
            'sandbox ownership/mode/initialization invalid')
        require(h, item['production_source'] not in sources and item['sandbox_source'] not in destinations,
            'duplicate sandbox source')
        sources.add(item['production_source']); destinations.add(item['sandbox_source'])


def tree_identity(h, root):
    """Observe regular byte-owned trees; links, devices and shared inodes fail closed."""
    root = Path(root)
    require(h, root.is_absolute() and root.resolve(strict=True) == root and root.is_dir(),
        'sandbox/source tree missing or aliased')
    result = {}
    for item in (root, *sorted(root.rglob('*'))):
        before = item.lstat()
        require(h, not item.is_symlink() and item.resolve(strict=True) == item and
            (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)), 'aliased/unsupported tree entry')
        require(h, stat.S_ISDIR(before.st_mode) or before.st_nlink == 1, 'hardlinked tree content')
        fields = {k: getattr(before, k) for k in ('st_dev', 'st_ino', 'st_uid', 'st_gid',
            'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
        fields['sha256'] = hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else None
        after = item.lstat()
        require(h, all(getattr(after, k) == v for k, v in fields.items() if k != 'sha256'),
            'tree changed during observation')
        result[str(item.relative_to(root))] = fields
    return result


def sandbox_mounts(h, policy, old, *, check_files=True):
    """One bounded source projection; target, access and alias groups stay exact."""
    require(h, policy.get('role') == 'production', 'sandbox requires production domain')
    r = policy['recovery']; validate_sandbox_declaration(h, r); s = r['sandbox']
    writable_sources = {m['source'] for m in old['mounts'] if m['read_only'] is False}
    declared = {item['production_source']: item for item in s['roots']}
    require(h, set(declared) == writable_sources, 'sandbox must cover every and only production RW source')
    require(h, all(not overlap(h, s['root'], m['source']) for m in old['mounts']),
        'sandbox overlaps production source')
    for a in declared:
        require(h, all(a == b or not overlap(h, a, b) for b in declared), 'nested production RW sources unsupported')
    if check_files:
        h._protected_path(SANDBOX_PARENT, directory=True)
        h._protected_path(Path(s['root']), directory=True)
        require(h, stat.S_IMODE(Path(s['root']).stat().st_mode) == 0o700, 'sandbox allocation must be root-controlled 0700')
        old_inodes = set()
        for source in writable_sources:
            state = tree_identity(h, source)
            old_inodes.update((v['st_dev'], v['st_ino']) for v in state.values())
            item = declared[source]; st = Path(source).stat()
            require(h, (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) ==
                (item['uid'], item['gid'], item['mode']), 'sandbox permissions differ from retained runtime')
        for item in s['roots']:
            state = tree_identity(h, item['sandbox_source']); st = Path(item['sandbox_source']).stat()
            require(h, (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) ==
                (item['uid'], item['gid'], item['mode']), 'sandbox ownership/mode differs')
            require(h, all((v['st_dev'], v['st_ino']) not in old_inodes for v in state.values()),
                'sandbox shares production inode')
    # Include a retained RO alias only when it is the exact same old source.
    # In particular capture-snapshots and its RO consumer must remain samefile.
    return [{**m, 'source': declared[m['source']]['sandbox_source']}
            if m['source'] in declared else copy.deepcopy(m) for m in old['mounts']]


def validate_mount_projection(h, actual, expected, grant_target):
    require(h, [m for m in actual if m['target'] != grant_target] ==
        [m for m in expected if m['target'] != grant_target], 'business mount source or semantics changed')


def build_sandbox(h, policy):
    """Root-run controlled byte copy/empty allocation; never alter a source.

    Call only under separately authorized rehearsal operations. Failures retain
    the unique partial allocation for audit; no recursive cleanup is implicit.
    """
    h._require_linux_root(); h.require_protected_authority_source(); validate(h, policy)
    r = policy['recovery']; require(h, 'sandbox' in r, 'explicit sandbox mode required')
    old, _ = retained(h, policy, sandbox_ready=False)
    h._protected_path(SANDBOX_PARENT, directory=True)
    root = Path(r['sandbox']['root']); require(h, not root.exists(), 'sandbox allocation already exists')
    h._reject_other_writable_sources(str(root), None)
    before = {item['production_source']: tree_identity(h, item['production_source']) for item in r['sandbox']['roots']}
    root.mkdir(mode=0o700); root.chmod(0o700)
    for item in r['sandbox']['roots']:
        src, dst = Path(item['production_source']), Path(item['sandbox_source'])
        dst.mkdir(mode=0o700)
        if item['initialization'] == 'COPY_CURRENT_STATE_REQUIRED':
            for relative, identity in before[str(src)].items():
                if relative == '.': continue
                a, b = src / relative, dst / relative
                if stat.S_ISDIR(identity['st_mode']):
                    b.mkdir(mode=0o700)
                else:
                    # Plain bytes, O_NOFOLLOW and exclusive destination; no
                    # symlink/hardlink/reflink or copy-on-write shortcut.
                    fd = os.open(a, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        opened = os.fstat(fd)
                        require(h, (opened.st_dev, opened.st_ino) == (identity['st_dev'], identity['st_ino']),
                            'copy source replaced')
                        with os.fdopen(fd, 'rb', closefd=False) as reader, b.open('xb') as writer:
                            for data in iter(lambda: reader.read(1048576), b''): writer.write(data)
                            writer.flush(); os.fsync(writer.fileno())
                    finally: os.close(fd)
                    require(h, hashlib.sha256(b.read_bytes()).hexdigest() == identity['sha256'], 'copy bytes differ')
                os.chown(b, item['uid'], item['gid']); b.chmod(stat.S_IMODE(identity['st_mode']))
        os.chown(dst, item['uid'], item['gid']); dst.chmod(item['mode'])
    after = {name: tree_identity(h, name) for name in before}
    require(h, before == after, 'production source changed during copy')
    projected = sandbox_mounts(h, policy, old)
    return {'rehearsal_id': r['nonce'], 'root': str(root), 'mounts': projected,
        'production_before': before, 'production_after': after,
        'sandbox_initial': {i['sandbox_source']: tree_identity(h, i['sandbox_source']) for i in r['sandbox']['roots']}}


def require(h, value, message):
    if not value:
        raise h.HostAuthorizationError('recovery: ' + message)


def validate(h, policy):
    r = policy['recovery']
    fields = {'purpose', 'baseline_policy', 'production_container_id',
        'production_observation_sha256', 'nonce', 'project', 'container', 'host_port', 'network',
        'preserved_store_sources'}
    # The network object does not exist while the Compose projection is prepared.
    # It must be pinned in the protected policy after create and before any grant.
    require(h, type(r) is dict and set(r) in (fields, fields | {'expected_network_id'},
        fields | {'sandbox'}, fields | {'expected_network_id', 'sandbox'}),
        'invalid protected projection fields')
    require(h, r['purpose'] == 'recovery-validation', 'not a recovery validation policy')
    require(h, isinstance(r['nonce'], str) and re.fullmatch('[0-9a-f]{32}', r['nonce']), 'invalid nonce')
    require(h, r['project'] == 'spread-recovery-' + r['nonce'] and
        r['container'] == policy['service_id'] + '-recovery-' + r['nonce'] and
        r['network'] == r['project'] + '-net', 'namespace is not isolated')
    if 'expected_network_id' in r:
        require(h, isinstance(r['expected_network_id'], str) and
            h._HEX64.fullmatch(r['expected_network_id']), 'invalid expected recovery network object ID')
    require(h, type(r['host_port']) is int and 1024 <= r['host_port'] <= 65535 and
        r['host_port'] != 8501, 'host port must be a distinct unprivileged slot')
    h._container_id(r['production_container_id'])
    require(h, isinstance(r['production_observation_sha256'], str) and
        h._HEX64.fullmatch(r['production_observation_sha256']), 'missing live production binding')
    ref = r['baseline_policy']
    require(h, type(ref) is dict and set(ref) == {'path', 'sha256'} and
        isinstance(ref['sha256'], str) and h._HEX64.fullmatch(ref['sha256']), 'invalid baseline reference')
    h._absolute(ref['path'])
    paths = r['preserved_store_sources']
    require(h, type(paths) is list and bool(paths) and len(paths) == len(set(paths)), 'missing operational roots')
    for path in paths:
        h._absolute(path)
    require(h, not any(overlap(h, a, b) for i, a in enumerate(paths) for b in paths[i+1:]), 'operational roots overlap')
    if 'sandbox' in r:
        require(h, policy.get('role') == 'production', 'sandbox requires production domain')
        validate_sandbox_declaration(h, r)


def overlap(h, a, b):
    return h._within(a, b) or h._within(b, a)


def production_identity(h, c):
    """Stable actual identity, including network/ports/start, not a caller PASS."""
    return h._digest({k: c.get(k) for k in ('Id', 'Name', 'Image', 'Created', 'RestartCount', 'Config',
        'HostConfig', 'NetworkSettings', 'Path', 'Args')} | {'started_at': c['State']['StartedAt']})


def preserved(h, policy):
    result = []
    for name in policy['recovery']['preserved_store_sources']:
        p = Path(name)
        require(h, p.is_dir() and not p.is_symlink() and p.resolve() == p, 'operational directory missing/aliased')
        require(h, not any(overlap(h, name, m['source']) for m in policy['mounts']),
            'old recovery must not mount operational stores, even readonly')
        st = p.stat()
        files = {}
        for item in sorted(p.rglob('*')):
            require(h, not item.is_symlink() and item.resolve() == item, 'aliased operational content')
            if item.is_file():
                require(h, item.stat().st_nlink == 1, 'hardlinked operational content')
                digest = hashlib.sha256()
                with item.open('rb') as stream:
                    for data in iter(lambda: stream.read(1048576), b''):
                        digest.update(data)
                files[str(item.relative_to(p))] = digest.hexdigest()
            else:
                require(h, item.is_dir(), 'unsupported operational entry')
        result.append(dict(source=name, device=st.st_dev, inode=st.st_ino, files=files))
    return result


def retained(h, policy, *, sandbox_ready=True):
    """Re-read protected old approval and Docker; never reuse its execution grant."""
    validate(h, policy)
    r = policy['recovery']
    raw = h._protected_path(Path(r['baseline_policy']['path']), private=True).read_bytes()
    require(h, hashlib.sha256(raw).hexdigest() == r['baseline_policy']['sha256'], 'baseline changed')
    old = h._json(raw)
    require(h, 'recovery' not in old, 'recursive recovery policy')
    h.validate_policy(old, 'production')
    variable = {'recovery', 'actual_config_sha256', 'rendered_compose_sha256', 'compose_sources',
        'compose_project_directory', 'compose_environment_file', 'mounts'}
    require(h, {k:v for k,v in old.items() if k not in variable} ==
        {k:v for k,v in policy.items() if k not in variable}, 'artifact/runtime policy changed')
    grant_target = policy['grant_container_directory']
    old_mounts = [m for m in old['mounts'] if m['target'] != grant_target]
    mounts = [m for m in policy['mounts'] if m['target'] != grant_target]
    expected_mounts = sandbox_mounts(h, policy, old, check_files=sandbox_ready) if 'sandbox' in r else old['mounts']
    validate_mount_projection(h, policy['mounts'], expected_mounts, grant_target)
    og = [m for m in old['mounts'] if m['target'] == grant_target]
    ng = [m for m in policy['mounts'] if m['target'] == grant_target]
    require(h, len(og) == len(ng) == 1 and ng[0]['read_only'] is True and
        not overlap(h, og[0]['source'], ng[0]['source']) and
        not any(overlap(h, ng[0]['source'], m['source']) for m in old_mounts),
        'fresh grant directory must be distinct; old grant cannot be reused')
    c = h.docker_inspect(r['production_container_id'])
    require(h, c.get('Name') == '/' + old['service_id'], 'old production name differs')
    require(h, c['State']['Running'] is True and production_identity(h, c) ==
        r['production_observation_sha256'], 'production instance changed')
    if 'sandbox' in r:
        require(h, all(c['Config'].get('User') == f"{item['uid']}:{item['gid']}"
            for item in r['sandbox']['roots']), 'sandbox owner differs from actual non-root runtime user')
    image = h.docker_image_inspect(old['image_id'])
    release = h.copy_container_json(c['Id'], old['source_root'] + '/RELEASE.json')
    observed = h.normalize_observation(c, image, release)
    require(h, observed['image_id'] == old['image_id'] and observed['mounts'] == old['mounts'],
        'old artifact/config/mount identity differs')
    h.compare_observed_config(observed, old['actual_config_sha256'])
    require(h, release['git_commit'] == old['approved_commit'] and release['git_tree'] == old['approved_tree']
        and release['application'] == old['release_application'], 'old release identity differs')
    require(h, image['Config']['Labels'].get('org.opencontainers.image.revision') == old['approved_commit']
        and image['Config']['Labels'].get('market-data.git.tree') == old['approved_tree'], 'old OCI identity differs')
    return old, c


def baseline(h, policy):
    old, c = retained(h, policy)
    source = Path(old['approved_source_root'])
    entry = h._contract_module(h.SOURCE_ROOT / '04_scripts/runtime/pre_release_runtime.py', '_recovery_pre')
    engine = h._contract_module(h.SOURCE_ROOT / '04_scripts/runtime/validate_target_runtime.py', '_recovery_engine')
    entry.require_source(h, engine)
    identity = entry.require_source(h, engine, source_root=source)
    require(h, identity == (old['approved_commit'], old['approved_tree']), 'old source identity differs')
    project = engine._project(source, old['project_id'])
    _, manifest, binding = engine.source_contract(source, old['project_id'], project['runtime_contract'])
    require(h, binding['source_sha256'][project['runtime_contract']] == old['runtime_manifest_sha256']
        and hashlib.sha256(h.copy_container_bytes(c['Id'], old['runtime_manifest_path'])).hexdigest() ==
        old['runtime_manifest_sha256'], 'old Manifest differs')
    engine.validate_source_compose(source, manifest)
    # Internal observed result, not a new signed record or authority protocol.
    observation = h.normalize_observation(c, h.docker_image_inspect(old['image_id']),
        h.copy_container_json(c['Id'], old['source_root'] + '/RELEASE.json'))
    return dict(rendered_compose_sha256=old['rendered_compose_sha256'],
        config_comparison=h.compare_observed_config(observation, old['actual_config_sha256']),
        production_identity=production_identity(h, c), preserved_stores=preserved(h, policy)), manifest


def semantic_network(h, raw):
    """Compare effective bridge defaults and optional custom IPAM, not JSON spelling."""
    require(h, type(raw) is dict and set(raw) <= {'name', 'driver', 'internal', 'ipam'}
        and isinstance(raw.get('name'), str) and bool(raw['name'])
        and isinstance(raw.get('driver', 'bridge'), str)
        and type(raw.get('internal', False)) is bool, 'unsupported old network semantics')
    result = dict(name=raw['name'], driver=raw.get('driver', 'bridge'),
        internal=raw.get('internal', False))
    if 'ipam' in raw:
        require(h, type(raw['ipam']) is dict, 'invalid custom IPAM')
        if raw['ipam']:
            result['ipam'] = copy.deepcopy(raw['ipam'])
    return result


def project_compose(h, desired, policy):
    """Return the sole allowed projection; issuer compares every other field."""
    old, production = retained(h, policy)
    r = policy['recovery']
    require(h, desired['name'] == r['project'], 'unexpected project')
    command = ['compose', '--project-directory', old['compose_project_directory'],
        '--env-file', old['compose_environment_file']]
    h._protected_path(Path(old['compose_environment_file']), private=True)
    for ref in old['compose_sources']:
        raw = h._protected_path(Path(ref['path'])).read_bytes()
        require(h, hashlib.sha256(raw).hexdigest() == ref['sha256'], 'old Compose changed')
        command += ['-f', ref['path']]
    original = h._json(h._run_docker(command + ['config', '--format', 'json']))
    require(h, h._digest(original) == old['rendered_compose_sha256'], 'old rendered config changed')
    projected = copy.deepcopy(original)
    service_id = policy['service_id']
    require(h, set(projected['services']) == {service_id}, 'other services not supported')
    service = projected['services'][service_id]
    require(h, service.get('container_name') == service_id and
        len(service.get('ports', [])) == 1 and service['ports'][0].get('target') == 8501
        and service['ports'][0].get('protocol', 'tcp') == 'tcp', 'unsupported old port contract')
    require(h, set(projected.get('networks', {})) == {'default'} and
        set(desired.get('networks', {})) == {'default'} and
        service.get('networks') == {'default': None}, 'unsupported old network semantics')
    original_network = semantic_network(h, projected['networks']['default'])
    desired_network = semantic_network(h, desired['networks']['default'])
    require(h, original_network['name'] == projected['name'] + '_default' and
        desired_network['name'] == r['project'] + '_default' and
        original_network.get('driver', 'bridge') == desired_network.get('driver', 'bridge') == 'bridge' and
        original_network.get('internal', False) is desired_network.get('internal', False) is False and
        'ipam' not in original_network and 'ipam' not in desired_network and
        {k: v for k, v in original_network.items() if k != 'name'} ==
        {k: v for k, v in desired_network.items() if k != 'name'},
        'unsupported old network semantics')
    old_name = projected['name']
    projected['name'] = r['project']
    projected['networks']['default'] = dict(original_network, name=r['project'] + '_default')
    for definition in projected.get('secrets', {}).values():
        if 'name' in definition:
            require(h, definition['name'].startswith(old_name + '_'), 'non-project secret identity')
            definition['name'] = r['project'] + definition['name'][len(old_name):]
    grant_source = next(m['source'] for m in policy['mounts'] if m['target'] == policy['grant_container_directory'])
    for mount in service.get('volumes', []):
        if mount['target'] == policy['grant_container_directory']:
            mount['source'] = grant_source
        elif 'sandbox' in r:
            replacement = next((m for m in sandbox_mounts(h, policy, old)
                if m['target'] == mount['target']), None)
            require(h, replacement is not None, 'undeclared Compose mount')
            require(h, type(mount.get('read_only', False)) is bool and mount.get('read_only', False) == replacement['read_only'], 'Compose access mode changed')
            mount['source'] = replacement['source']
    expected = copy.deepcopy(desired)
    expected['networks']['default'] = desired_network
    for doc in (expected, projected):
        doc['services'][service_id].pop('build', None)
        doc['services'][service_id].pop('hostname', None)
    require(h, expected == projected, 'business configuration differs from retained production')
    result = copy.deepcopy(desired)
    service = result['services'][service_id]
    service['container_name'] = r['container']
    service['ports'][0].update(host_ip='127.0.0.1', published=str(r['host_port']))
    result['networks']['default'] = dict(name=r['network'], driver='bridge', internal=False)
    require(h, r['network'] not in production['NetworkSettings']['Networks'], 'production network joined')
    return result


def validate_projected_network(h, projected, rendered, policy):
    """Own the recovery network subtree before the host compares all other Compose fields."""
    r = policy['recovery']
    _, production = retained(h, policy)
    require(h, r['network'] not in production['NetworkSettings']['Networks'], 'production network joined')
    for document in (projected, rendered):
        require(h, type(document) is dict and type(document.get('networks')) is dict and
            set(document['networks']) == {'default'} and
            type(document.get('services')) is dict and set(document['services']) == {policy['service_id']} and
            document['services'][policy['service_id']].get('networks') == {'default': None},
            'recovery must use only its isolated default network')
        ports = document['services'][policy['service_id']].get('ports')
        require(h, type(ports) is list and len(ports) == 1 and type(ports[0]) is dict and
            ports[0].get('host_ip') == '127.0.0.1' and
            ports[0].get('published') == str(r['host_port']) and
            ports[0].get('target') == 8501 and ports[0].get('protocol', 'tcp') == 'tcp',
            'recovery port must bind only localhost')
    expected = dict(name=r['network'], driver='bridge', internal=False)
    require(h, semantic_network(h, projected['networks']['default']) == expected and
        semantic_network(h, rendered['networks']['default']) == expected,
        'recovery network semantics differ')
    return True


def validate_instance(h, container, policy, phase):
    """Validate declared attachment before start or materialized endpoint after start."""
    old, production = retained(h, policy)
    r = policy['recovery']
    expected_id = r.get('expected_network_id')
    require(h, isinstance(expected_id, str) and h._HEX64.fullmatch(expected_id),
        'expected recovery network object ID must be pinned before grant')
    state = container.get('State', {})
    if phase == 'pre_start':
        require(h, state.get('Status') == 'created' and state.get('Running') is False,
            'pre-start network validation requires a created container')
    elif phase == 'post_start':
        require(h, state.get('Status') == 'running' and state.get('Running') is True,
            'post-start network validation requires a running container')
    else:
        require(h, False, 'unknown recovery network lifecycle phase')
    require(h, container['Id'] != production['Id'] and container.get('Name') == '/' + r['container'], 'wrong recovery instance')
    labels = container['Config'].get('Labels', {})
    require(h, labels.get('com.docker.compose.project') == r['project'] and
        container['Config']['Hostname'] == r['nonce'], 'project/nonce differs')
    require(h, container['HostConfig'].get('PortBindings') ==
        {'8501/tcp': [{'HostIp':'127.0.0.1', 'HostPort':str(r['host_port'])}]}, 'actual port is not localhost recovery slot')
    require(h, set(container['NetworkSettings']['Networks']) == {r['network']} and
        container['HostConfig'].get('NetworkMode') == r['network'], 'actual recovery network differs')
    networks = h._observe("inspect_object", h._run_docker(['network', 'inspect', r['network']]),
                          'recovery network inspect', expected_id=expected_id)
    require(h, networks.get('Name') == r['network'] and networks.get('Driver') == 'bridge' and
        networks.get('Internal') is False and not networks.get('EnableIPv6') and
        networks.get('Labels', {}).get('com.docker.compose.project') == r['project'] and
        not networks.get('Options') and not networks.get('Attachable') and not networks.get('Ingress') and
        set(networks.get('Containers', {})) <= {container['Id']}, 'network is shared or not an isolated bridge')
    require(h, networks.get('Id') == expected_id, 'expected recovery network object changed')
    require(h, expected_id not in {v.get('NetworkID') for v in production['NetworkSettings']['Networks'].values()},
        'production network identity reused')
    endpoint = container['NetworkSettings']['Networks'][r['network']]
    actual_id = endpoint.get('NetworkID')
    if phase == 'pre_start':
        require(h, type(actual_id) is str and actual_id in ('', expected_id),
            'created container has a wrong materialized network ID')
        assertion = 'DEFERRED_UNTIL_POST_START' if actual_id == '' else 'PASS'
    else:
        require(h, actual_id == expected_id and
            isinstance(endpoint.get('EndpointID'), str) and h._HEX64.fullmatch(endpoint['EndpointID']) and
            set(networks.get('Containers', {})) == {container['Id']},
            'post-start recovery endpoint identity is not materialized or differs')
        assertion = 'PASS'
    preserved(h, policy)
    return {'RUNTIME_NETWORK_IDENTITY_ASSERTION': assertion,
        'expected_network_id': expected_id, 'network': r['network'], 'container_id': container['Id']}


def grant_id(h, policy, container_id):
    """Commit the expected network object to the existing signed grant ID field.

    The retained image accepts only the existing grant/2 schema, so the host
    cannot append a payload field. The fresh namespace nonce keeps this ID
    unique while the signed value binds the protected network object identity.
    """
    validate(h, policy)
    r = policy['recovery']
    require(h, isinstance(r.get('expected_network_id'), str) and
        h._HEX64.fullmatch(r['expected_network_id']),
        'fresh recovery grant requires the expected network object ID')
    h._container_id(container_id)
    binding = dict(domain='recovery-network-grant-id/1',
        nonce=r['nonce'], container_id=container_id, network=r['network'],
        network_id=r['expected_network_id'], host_port=r['host_port'],
        image_id=policy['image_id'], approved_commit=policy['approved_commit'],
        approved_tree=policy['approved_tree'])
    if 'sandbox' in r:
        binding['sandbox'] = r['sandbox']
    return h._digest(binding)[:32]


def verify_sandbox_evidence(h, policy, grant, data):
    """Consume protected collector facts, never a caller-provided PASS flag."""
    old, _ = retained(h, policy)
    r = policy['recovery']; require(h, 'sandbox' in r, 'sandbox evidence lacks explicit mode')
    require(h, grant['role'] == 'production' and grant['authorization_mode'] == 'production',
        'sandbox evidence requires production grant')
    require(h, grant['grant_id'] == grant_id(h, policy, grant['container_id']) and
        grant['mount_contract_sha256'] == h._digest(policy['mounts']) and
        grant['actual_config_sha256'] == policy['actual_config_sha256'] and
        grant['rendered_compose_sha256'] == policy['rendered_compose_sha256'], 'sandbox grant scope differs')
    writable = [m['target'] for m in policy['mounts'] if not m['read_only']]
    require(h, sorted(grant['writable_roots']) == sorted(writable), 'grant writes outside sandbox scope')
    require(h, type(data) is dict and set(data) == {'production_before', 'production_after',
        'sandbox_before', 'sandbox_after'}, 'sandbox collector facts incomplete')
    sources = {i['production_source'] for i in r['sandbox']['roots']}
    destinations = {i['sandbox_source'] for i in r['sandbox']['roots']}
    for name, expected in (('production_before', sources), ('production_after', sources),
            ('sandbox_before', destinations), ('sandbox_after', destinations)):
        require(h, type(data[name]) is dict and set(data[name]) == expected and
            all(type(v) is dict and bool(v) for v in data[name].values()), 'sandbox tree coverage incomplete')
    require(h, data['production_before'] == data['production_after'], 'live production tree changed')
    require(h, data['production_after'] == {name: tree_identity(h, name) for name in sources},
        'production preservation evidence no longer current')
    require(h, data['sandbox_after'] == {name: tree_identity(h, name) for name in destinations},
        'sandbox output evidence differs from actual allocation')
    return old
