"""Bounded host-policy projection for recovery using the existing production domain.

No container-side protocol, signing key, application override or deployment action.
The protected policy binds a retained production policy and a live old instance.
"""
import copy
import hashlib
from pathlib import Path
import re


def require(h, value, message):
    if not value:
        raise h.HostAuthorizationError('recovery: ' + message)


def validate(h, policy):
    r = policy['recovery']
    require(h, type(r) is dict and set(r) == {'purpose', 'baseline_policy', 'production_container_id',
        'production_observation_sha256', 'nonce', 'project', 'container', 'host_port', 'network',
        'preserved_store_sources'}, 'invalid protected projection fields')
    require(h, r['purpose'] == 'recovery-validation', 'not a recovery validation policy')
    require(h, isinstance(r['nonce'], str) and re.fullmatch('[0-9a-f]{32}', r['nonce']), 'invalid nonce')
    require(h, r['project'] == 'spread-recovery-' + r['nonce'] and
        r['container'] == policy['service_id'] + '-recovery-' + r['nonce'] and
        r['network'] == r['project'] + '-net', 'namespace is not isolated')
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


def retained(h, policy):
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
    require(h, mounts == old_mounts, 'business mount source or semantics changed')
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
    image = h.docker_image_inspect(old['image_id'])
    release = h.copy_container_json(c['Id'], old['source_root'] + '/RELEASE.json')
    observed = h.normalize_observation(c, image, release)
    require(h, observed['image_id'] == old['image_id'] and observed['actual_config_sha256'] ==
        old['actual_config_sha256'] and observed['mounts'] == old['mounts'], 'old artifact/config/mount identity differs')
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
    return dict(rendered_compose_sha256=old['rendered_compose_sha256'],
        production_identity=production_identity(h, c), preserved_stores=preserved(h, policy)), manifest


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
        set(projected['networks']['default']) <= {'name', 'driver', 'internal'} and
        projected['networks']['default'].get('driver', 'bridge') == 'bridge' and
        projected['networks']['default'].get('internal', False) is False and
        service.get('networks') == {'default': None}, 'unsupported old network semantics')
    old_name = projected['name']
    projected['name'] = r['project']
    projected['networks']['default']['name'] = r['project'] + '_default'
    for definition in projected.get('secrets', {}).values():
        if 'name' in definition:
            require(h, definition['name'].startswith(old_name + '_'), 'non-project secret identity')
            definition['name'] = r['project'] + definition['name'][len(old_name):]
    grant_source = next(m['source'] for m in policy['mounts'] if m['target'] == policy['grant_container_directory'])
    for mount in service.get('volumes', []):
        if mount['target'] == policy['grant_container_directory']:
            mount['source'] = grant_source
    expected = copy.deepcopy(desired)
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


def validate_instance(h, container, policy):
    """Actual Docker metadata is checked before signing, again before sealing."""
    old, production = retained(h, policy)
    r = policy['recovery']
    require(h, container['Id'] != production['Id'] and container.get('Name') == '/' + r['container'], 'wrong recovery instance')
    labels = container['Config'].get('Labels', {})
    require(h, labels.get('com.docker.compose.project') == r['project'] and
        container['Config']['Hostname'] == r['nonce'], 'project/nonce differs')
    require(h, container['HostConfig'].get('PortBindings') ==
        {'8501/tcp': [{'HostIp':'127.0.0.1', 'HostPort':str(r['host_port'])}]}, 'actual port is not localhost recovery slot')
    require(h, set(container['NetworkSettings']['Networks']) == {r['network']} and
        container['HostConfig'].get('NetworkMode') == r['network'], 'actual recovery network differs')
    import json
    values = json.loads(h._run_docker(['network', 'inspect', r['network']]))
    require(h, type(values) is list and len(values) == 1, 'network observation missing')
    networks = values[0]
    require(h, networks.get('Name') == r['network'] and networks.get('Driver') == 'bridge' and
        networks.get('Internal') is False and not networks.get('EnableIPv6') and
        networks.get('Labels', {}).get('com.docker.compose.project') == r['project'] and
        not networks.get('Options') and not networks.get('Attachable') and not networks.get('Ingress') and
        container['NetworkSettings']['Networks'][r['network']].get('NetworkID') == networks.get('Id') and
        set(networks.get('Containers', {})) <= {container['Id']}, 'network is shared or not an isolated bridge')
    require(h, networks.get('Id') not in {v.get('NetworkID') for v in production['NetworkSettings']['Networks'].values()},
        'production network identity reused')
    preserved(h, policy)
