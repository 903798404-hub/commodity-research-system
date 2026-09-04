"""Registry and startup boundaries using isolated Git fixtures, never production."""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest
from quality import audit_changed_scope as scope
from quality import project_registry as registry
from quality import start_project

ROOT = Path(__file__).resolve().parents[1]


def minimal_registry():
    return {"schema_version":"project-registry/1", "protected_paths":["shared"], "projects":[{
        "project_id":"demo", "change_class":"business", "status":"ready",
        "owned_paths":["feature"], "shared_dependencies":["shared"], "forbidden_paths":["other"],
        "required_tests":["tests/test_feature.py"], "capabilities":["demo"], "boundary_notes":"fixture only"}]}


def write_registry(root, data):
    path=root/registry.REGISTRY_PATH
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data),encoding="utf-8")


@pytest.fixture
def repository(tmp_path):
    root=tmp_path/'main'
    root.mkdir()
    for path in ('feature/code.py','other/code.py','shared/code.py','tests/test_feature.py'):
        target=root/path
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text('# fixture\n',encoding='utf-8')
    write_registry(root,minimal_registry())
    registry.git(root,'init','-b','main')
    registry.git(root,'config','user.name','Scope Fixture')
    registry.git(root,'config','user.email','fixture@example.invalid')
    registry.git(root,'add','--','feature','other','shared','tests','02_configs')
    registry.git(root,'-c','commit.gpgsign=false','commit','-m','fixture baseline')
    registry.git(root,'update-ref','refs/remotes/origin/main','HEAD')
    feature=tmp_path/'feature'
    registry.git(root,'worktree','add','-b','feat/demo',str(feature),'HEAD')
    return root,feature


def test_real_registry_schema_paths_and_minimum_projects():
    data=registry.load_registry(ROOT)
    projects={p['project_id']:p for p in data['projects']}
    assert {'soybean-pm','weather-basis-push','international-spread','weather','domestic-basis',
            'usda','oil-world','palm','canola'} <= projects.keys()
    assert projects['soybean-pm']['status']=='frozen'
    assert not projects['weather-basis-push']['owned_paths']
    assert projects['canola']['status']=='needs-boundary-review'
    assert len(projects['soybean-pm']['capabilities'])==7
    assert all('03_src/agri_research_agent/shared' in p['forbidden_paths'] for p in projects.values() if p['change_class']=='business')


@pytest.mark.parametrize('value',['','.', '..','a/../b','/tmp','C:/tmp','a\\b','a/**','a//b',' a','a\n'])
def test_registry_path_escape_rejected(value):
    with pytest.raises(ValueError):
        registry.relative_path(value)


def test_unknown_missing_and_bad_schema_fail(repository):
    _,root=repository
    with pytest.raises(ValueError,match='Unknown'):
        registry.select_project(root,'unknown')
    (root/registry.REGISTRY_PATH).unlink()
    with pytest.raises(ValueError,match='unavailable'):
        registry.load_registry(root)
    write_registry(root,{'schema_version':'old'})
    with pytest.raises(ValueError,match='schema'):
        registry.load_registry(root)


def test_duplicate_project_and_missing_path_fail(repository):
    _,root=repository
    data=minimal_registry()
    data['projects'].append(copy.deepcopy(data['projects'][0]))
    with pytest.raises(ValueError,match='duplicate'):
        registry.validate_registry(data,root)
    data=minimal_registry()
    data['projects'][0]['owned_paths']=['does-not-exist']
    with pytest.raises(ValueError,match='missing'):
        registry.validate_registry(data,root)


def test_project_business_pass_and_cross_project_fail(repository,monkeypatch,capsys):
    _,root=repository
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    (root/'feature/code.py').write_text('# change\n')
    assert scope.main(['--project','demo'])==0
    report=json.loads(capsys.readouterr().out)
    assert report['PROJECT_SCOPE']=='PASS'
    assert report['comparison']=='origin/main...HEAD'
    (root/'other/code.py').write_text('# cross project\n')
    assert scope.main(['--project','demo'])==1
    report=json.loads(capsys.readouterr().out)
    assert report['forbidden_changes']==['other/code.py']


@pytest.mark.parametrize('args',[
    ['--project','demo','--owned','shared'],
    ['--project','demo','--change-class','shared'],
    ['--project','demo','--baseline','HEAD'],
    ['--project','demo','--known-existing','other'],
    ['--owned','feature'],
    ['--project','unknown'],
])
def test_business_cannot_override_boundaries(repository,monkeypatch,args):
    _,root=repository
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    assert scope.main(args)==2


def test_registry_self_expansion_rejected(repository,monkeypatch):
    _,root=repository
    data=minimal_registry()
    data['projects'][0]['owned_paths'].append('other')
    write_registry(root,data)
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    assert scope.main(['--project','demo'])==2


def test_shared_file_and_rename_cannot_hide(repository,monkeypatch,capsys):
    _,root=repository
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    (root/'shared/code.py').rename(root/'feature/moved.py')
    registry.git(root,'add','--','shared/code.py','feature/moved.py')
    # Root custom protection is applied independent of ownership and renames.
    assert scope.main(['--project','demo'])==1
    report=json.loads(capsys.readouterr().out)
    assert 'shared/code.py' in report['shared_changes']
    assert report['SHARED_CHANGE']=='YES'


def test_shared_low_level_still_passes(repository,monkeypatch,capsys):
    _,root=repository
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    target=root/'04_scripts/quality'
    target.mkdir(parents=True)
    (target/'helper.py').write_text('# fixture')
    assert scope.main(['--change-class','shared','--owned','04_scripts/quality'])==0
    assert json.loads(capsys.readouterr().out)['SHARED_CHANGE']=='YES'


def test_main_clean_mirror_failures(repository):
    main,feature=repository
    assert registry.assert_main_mirror(feature)['main_clean_mirror']
    (main/'feature/code.py').write_text('# dirty')
    with pytest.raises(ValueError,match='NOT_CLEAN'):
        registry.assert_main_mirror(feature)
    (main/'feature/code.py').write_text('# fixture\n')
    registry.git(feature,'commit','--allow-empty','-m','fixture only')
    registry.git(feature,'update-ref','refs/remotes/origin/main','HEAD')
    with pytest.raises(ValueError,match='NOT_MIRROR'):
        registry.assert_main_mirror(feature)


def test_startup_fetches_fresh_remote_and_does_not_modify_main(repository,monkeypatch,tmp_path):
    main,feature=repository
    original=registry.git
    calls=[]
    head=original(main,'rev-parse','HEAD')
    def local_git(root,*args):
        calls.append(args)
        if args==('fetch','origin'):
            return ''
        if args[:1]==('ls-remote',):
            return head+'\trefs/heads/main'
        return original(root,*args)
    monkeypatch.setattr(registry,'git',local_git)
    destination=tmp_path/'new-project'
    result=start_project.prepare(feature,'demo','feat/new',destination)
    assert not destination.exists() and not result['created']
    result=start_project.prepare(feature,'demo','feat/new',destination,create=True)
    assert result['created'] and result['baseline_head']==head
    assert original(main,'status','--porcelain')==''
    assert original(main,'rev-parse','HEAD')==head
    assert calls[:2]==[('fetch','origin'),('ls-remote','--exit-code','origin','refs/heads/main')]


def test_startup_remote_drift_stops_before_creation(repository,monkeypatch,tmp_path):
    _,feature=repository
    original=registry.git
    def drift(root,*args):
        if args==('fetch','origin'):
            return ''
        if args[:1]==('ls-remote',):
            return '0'*40+'\trefs/heads/main'
        return original(root,*args)
    monkeypatch.setattr(registry,'git',drift)
    with pytest.raises(ValueError,match='REMOTE_MOVED'):
        start_project.prepare(feature,'demo','feat/new',tmp_path/'new',create=True)
    assert not (tmp_path/'new').exists()


def test_owned_rename_is_not_reported_as_arrow_path(repository,monkeypatch,capsys):
    _,root=repository
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    registry.git(root,'mv','feature/code.py','feature/new.py')
    assert scope.main(['--project','demo'])==0
    assert json.loads(capsys.readouterr().out)['changed_files']==['feature/code.py','feature/new.py']


@pytest.mark.parametrize('status',['frozen','needs-boundary-review'])
def test_project_not_ready_stops(repository,monkeypatch,status):
    _,root=repository
    data=minimal_registry()
    data['projects'][0]['status']=status
    write_registry(root,data)
    monkeypatch.setattr(scope,'PROJECT_ROOT',root)
    assert scope.main(['--project','demo'])==2


def test_startup_cannot_create_inside_main(repository,monkeypatch):
    main,feature=repository
    original=registry.git
    def local_git(root,*args):
        if args==('fetch','origin'):
            return ''
        if args[:1]==('ls-remote',):
            return original(main,'rev-parse','HEAD')+'\trefs/heads/main'
        return original(root,*args)
    monkeypatch.setattr(registry,'git',local_git)
    with pytest.raises(ValueError,match='OUTSIDE_CALLER'):
        start_project.prepare(feature,'demo','feat/new',main/'nested',create=True)
    assert not (main/'nested').exists()


def bootstrap_registry(root):
    data = minimal_registry()
    data['schema_version'] = 'project-registry/2'
    project = data['projects'][0]
    project['future_owned_paths'] = ['new_module/quotes.py', '08_tests/test_quotes.py']
    project['future_required_tests'] = ['08_tests/test_quotes.py']
    write_registry(root, data)
    registry.git(root, 'add', '--', registry.REGISTRY_PATH)
    registry.git(root, 'commit', '-m', 'fixture future registry')
    registry.git(root, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    return data


def test_future_missing_and_existing_state_and_exact_scope(repository, monkeypatch, capsys):
    _, root = repository
    data = bootstrap_registry(root)
    assert registry.validate_registry(data, root) == data
    # Unit fixture mirror remains independent; registry is approved on fixture remote.
    monkeypatch.setattr(registry, 'assert_main_mirror', lambda root: {})
    monkeypatch.setattr(scope, 'PROJECT_ROOT', root)
    target = root/'new_module/quotes.py'
    target.parent.mkdir()
    target.write_text('# real future creation\n')
    assert registry.validate_registry(data, root) == data
    assert scope.main(['--project', 'demo']) == 0
    assert json.loads(capsys.readouterr().out)['PROJECT_SCOPE'] == 'PASS'
    (target.parent/'random.py').write_text('# undeclared\n')
    assert scope.main(['--project', 'demo']) == 1
    assert json.loads(capsys.readouterr().out)['out_of_scope_changes'] == ['new_module/random.py']
    assert scope.main(['--project', 'unknown']) == 2
    data['projects'][0]['status'] = 'frozen'
    write_registry(root, data)
    assert scope.main(['--project', 'demo']) == 2


@pytest.mark.parametrize('path', ['a/*.py', 'a/**', '../a.py', '/a.py', 'C:/a.py',
                                 '\\\\server\\a.py', 'a/../../x.py', 'new_module/',
                                 'new_module', 'a/file.py.', 'a/NUL.py', 'a/file.py:stream'])
def test_future_invalid_paths(repository, path):
    _, root = repository
    data = bootstrap_registry(root)
    data['projects'][0]['future_owned_paths'] = [path]
    with pytest.raises(ValueError):
        registry.validate_registry(data, root)


def test_future_windows_identity_and_no_prefix_ownership(repository):
    _, root = repository
    data = bootstrap_registry(root)
    project = data['projects'][0]
    assert registry.owns(project, 'NEW_MODULE\\QUOTES.PY')
    assert not registry.owns(project, 'new_module/quotes.py/child.py')
    assert not registry.owns(project, 'new_module/quotes_helper.py')
    project['future_owned_paths'].append('NEW_MODULE\\QUOTES.PY')
    with pytest.raises(ValueError, match='Duplicate'):
        registry.validate_registry(data, root)


def test_future_start_allows_missing_without_creating_files(repository, monkeypatch, tmp_path):
    _, root = repository
    bootstrap_registry(root)
    original = registry.git
    def local_git(root, *args):
        if args == ('fetch', 'origin'):
            return ''
        if args[:1] == ('ls-remote',):
            return original(root, 'rev-parse', 'origin/main') + '\trefs/heads/main'
        return original(root, *args)
    monkeypatch.setattr(registry, 'git', local_git)
    monkeypatch.setattr(registry, 'assert_main_mirror', lambda root: {})
    result = start_project.prepare(root, 'demo', 'feat/future', tmp_path/'future-start')
    assert not result['created']
    assert result['future_required_tests'] == ['08_tests/test_quotes.py']
    assert not (root/'08_tests/test_quotes.py').exists()


@pytest.mark.parametrize('existing', [True, False])
def test_future_ownership_collision(repository, existing):
    _, root = repository
    data = bootstrap_registry(root)
    other = copy.deepcopy(data['projects'][0])
    other['project_id'] = 'other-project'
    other['future_owned_paths'] = [] if existing else ['NEW_MODULE\\QUOTES.PY']
    other['future_required_tests'] = []
    other['owned_paths'] = ['new_module'] if existing else []
    other['status'] = 'needs-boundary-review'
    if existing:
        (root/'new_module').mkdir()
    data['projects'].append(other)
    with pytest.raises(ValueError, match='collision'):
        registry.validate_registry(data, root)


def test_future_test_collision_even_under_existing_ownership(repository):
    _, root = repository
    data = bootstrap_registry(root)
    (root/'08_tests').mkdir()
    project = data['projects'][0]
    project['future_owned_paths'] = ['new_module/quotes.py']
    project['owned_paths'].append('08_tests')
    other = copy.deepcopy(project)
    other.update(project_id='other-project', future_owned_paths=[], future_required_tests=[])
    data['projects'].append(other)
    with pytest.raises(ValueError, match='test ownership collision'):
        registry.validate_registry(data, root)


@pytest.mark.parametrize('field', ['forbidden_paths', 'shared_dependencies', 'protected_paths'])
def test_future_cannot_override_protection(repository, field):
    _, root = repository
    data = bootstrap_registry(root)
    (root/'new_module').mkdir()
    owner = data if field == 'protected_paths' else data['projects'][0]
    owner[field].append('new_module')
    with pytest.raises(ValueError, match='conflicts|protected'):
        registry.validate_registry(data, root)


def test_future_minimum_shared_protection(repository):
    _, root = repository
    data = bootstrap_registry(root)
    data['projects'][0]['future_owned_paths'].append('03_src/agri_research_agent/market_data/new.py')
    with pytest.raises(ValueError, match='protected'):
        registry.validate_registry(data, root)


def test_future_test_area_ownership_and_existing_rules(repository):
    _, root = repository
    data = bootstrap_registry(root)
    for field, value in [('future_required_tests', 'outside/test_x.py'),
                         ('future_required_tests', '08_tests/test_not_owned.py'),
                         ('owned_paths', 'missing.py'), ('required_tests', '08_tests/missing.py')]:
        candidate = copy.deepcopy(data)
        candidate['projects'][0][field] = [value]
        with pytest.raises(ValueError):
            registry.validate_registry(candidate, root)
    (root/'new_module/quotes.py').mkdir(parents=True)
    with pytest.raises(ValueError, match='not a repository file'):
        registry.validate_registry(data, root)


def test_future_symlink_or_junction_escape(repository, tmp_path):
    import os
    _, root = repository
    data = bootstrap_registry(root)
    outside = tmp_path/'outside'
    outside.mkdir()
    link = root/'new_module'
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match='link'):
            registry.validate_registry(data, root)
    finally:
        if os.name == 'nt':
            link.rmdir()
        else:
            link.unlink()


def test_future_completion_lifecycle_real_pytest(repository):
    from quality import complete_project
    _, root = repository
    bootstrap_registry(root)
    with pytest.raises(ValueError, match='missing at completion'):
        complete_project.complete(root, 'demo')
    target = root/'08_tests/test_quotes.py'
    target.parent.mkdir()
    target.write_text('def test_quote():\n    assert True\n')
    # Existing mandatory test must also execute (fixture initially only a comment).
    (root/'tests/test_feature.py').write_text('def test_existing():\n    assert True\n')
    result = complete_project.complete(root, 'demo')
    assert result['PROJECT_COMPLETION'] == 'PASS'
    assert [t['passed'] for t in result['executed_tests']] == [1, 1]
    target.write_text('def test_quote():\n    assert False\n')
    with pytest.raises(ValueError, match='execution failed'):
        complete_project.complete(root, 'demo')


@pytest.mark.parametrize('body', ['# empty\n', 'import pytest\n@pytest.mark.skip\ndef test_skip(): pass\n',
                                'import pytest\n@pytest.mark.xfail\ndef test_xfail(): assert False\n'])
def test_completion_no_empty_skip_or_xfail(repository, body):
    from quality import complete_project
    _, root = repository
    bootstrap_registry(root)
    target = root/'08_tests/test_quotes.py'
    target.parent.mkdir()
    target.write_text(body)
    (root/'tests/test_feature.py').write_text('def test_existing(): pass\n')
    with pytest.raises(ValueError, match='execution failed|without skip'):
        complete_project.complete(root, 'demo')


def test_completion_rejects_candidate_mutation(repository):
    from quality import complete_project
    _, root = repository
    bootstrap_registry(root)
    target = root/'08_tests/test_quotes.py'
    target.parent.mkdir()
    target.write_text("from pathlib import Path\ndef test_mutation():\n    Path('feature/code.py').write_text('# changed')\n")
    (root/'tests/test_feature.py').write_text('def test_existing(): pass\n')
    with pytest.raises(ValueError, match='changed during completion'):
        complete_project.complete(root, 'demo')


def test_shared_future_cannot_change_closed_scope(repository, monkeypatch, capsys):
    _, root = repository
    data = bootstrap_registry(root)
    data['projects'][0]['change_class'] = 'shared'
    write_registry(root, data)
    monkeypatch.setattr(registry, 'assert_main_mirror', lambda root: {})
    monkeypatch.setattr(scope, 'PROJECT_ROOT', root)
    (root/'other/code.py').write_text('# closed infrastructure changed\n')
    assert scope.main(['--project', 'demo', '--change-class', 'shared']) == 1
    assert json.loads(capsys.readouterr().out)['forbidden_changes'] == ['other/code.py']


def test_registered_shared_intraday_exact_boundary():
    _, project = registry.select_project(ROOT, 'shared-intraday')
    assert project['change_class'] == 'shared' and project['status'] == 'ready'
    assert project['owned_paths'] == []
    assert len(project['future_owned_paths']) == 8
    assert len(project['future_required_tests']) == 4
    assert set(project['future_required_tests']) <= set(project['future_owned_paths'])
    assert 'REAL_AM_TEMPORAL_ACCEPTANCE=DEFERRED' in project['boundary_notes']
    assert 'REAL_PM_TEMPORAL_ACCEPTANCE=DEFERRED' in project['boundary_notes']
    assert 'DEFERRED_TEMPORAL_ACCEPTANCE_BLOCKING=NO' in project['boundary_notes']
    for path in project['future_owned_paths']:
        assert registry.owns(project, path)
        assert not registry.owns(project, path + '/unapproved.py')
    for path in project['forbidden_paths'] + project['shared_dependencies']:
        assert not registry.owns(project, path)
    assert registry.select_project(ROOT, 'soybean-pm')[1]['status'] == 'frozen'


def test_tankan_live_registration_is_additive_and_narrow():
    _, project = registry.select_project(ROOT, 'tankan-live-query')
    assert project['status'] == 'ready' and project['change_class'] == 'shared'
    assert project['owned_paths'] == [
        '03_src/agri_research_agent/data_sources/tankan/client.py',
        '03_src/agri_research_agent/data_sources/tankan/queries.py']
    assert project['future_owned_paths'] == project['future_required_tests'] == [
        '08_tests/data_sources/tankan/test_live_queries.py']
    assert not registry.owns(project, '03_src/agri_research_agent/data_sources/tankan/models.py')
    assert not registry.owns(project, '03_src/agri_research_agent/pipelines/public_data_daily.py')


@pytest.mark.parametrize('path,expected', [
    ('03_src/agri_research_agent/market_data/intraday.py', 'PASS'),
    ('03_src/agri_research_agent/market_data/intraday_helper_random.py', 'FAIL'),
    ('03_src/agri_research_agent/pipelines/public_data_daily.py', 'FAIL'),
    ('03_src/agri_research_agent/pipelines/lutou_weather.py', 'FAIL'),
    ('03_src/agri_research_agent/pipelines/lutou_domestic_basis.py', 'FAIL'),
    ('03_src/agri_research_agent/shared/async_update.py', 'FAIL'),
    ('05_apps/import_profit_page.py', 'FAIL'),
])
def test_registered_intraday_synthetic_scope(path, expected):
    _, project = registry.select_project(ROOT, 'shared-intraday')
    def synthetic_git(root, *args):
        if args == ('diff', '--name-only', '--no-renames', 'base...HEAD'):
            return path + '\n'
        if args[:1] == ('rev-parse',):
            return 'fixture-head'
        return ''
    report = scope.run_audit(ROOT, 'base', project['owned_paths'],
                             exact_allowed=project['future_owned_paths'],
                             change_class=project['change_class'], git=synthetic_git)
    assert report['PROJECT_SCOPE'] == expected

# Namespace reservation acceptance uses only temporary repositories.
def reservation_registry(root):
    data = minimal_registry()
    data['schema_version'] = 'project-registry/3'
    for name in ('03_src/agri_research_agent/alerts', '04_scripts', '08_tests',
                 '02_configs', '07_docs/projects'):
        (root/name).mkdir(parents=True, exist_ok=True)
    project = data['projects'][0]
    project.update(project_id='notification-push-fixture', owned_paths=['03_src/agri_research_agent/alerts'],
                   reserved_paths=['04_scripts/notifications', '08_tests/alerts',
                                   '02_configs/notifications', '07_docs/projects/notification'],
                   required_tests=[], future_required_tests=['08_tests/alerts/test_core.py'])
    return data


def test_reservation_bootstrap_real_git(tmp_path, monkeypatch, capsys):
    main = tmp_path/'main'
    main.mkdir()
    for name in ('shared/code.py', 'other/code.py', 'tests/test_feature.py',
                 '03_src/agri_research_agent/pipelines/lutou_weather.py',
                 '03_src/agri_research_agent/pipelines/public_data_daily.py',
                 '04_scripts/common.py', '08_tests/test_anchor.py', '07_docs/projects/canola/contract.md'):
        path=main/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# fixture\n')
    data=reservation_registry(main)
    (main/'03_src/agri_research_agent/alerts/__init__.py').write_text('# fixture\n')
    write_registry(main,data)
    registry.validate_registry(data, main)
    registry.git(main,'init','-b','main')
    registry.git(main,'config','user.name','Scope Fixture')
    registry.git(main,'config','user.email','fixture@example.invalid')
    registry.git(main,'add','.')
    registry.git(main,'-c','commit.gpgsign=false','commit','-m','synthetic approved reservation')
    remote=tmp_path/'remote.git'
    registry.git(main,'clone','--bare',str(main),str(remote))
    registry.git(main,'remote','add','origin',str(remote))
    feature=tmp_path/'feature'
    result=start_project.prepare(main,'notification-push-fixture','feat/bootstrap',feature,create=True)
    assert result['created'] and result['reserved_paths']==data['projects'][0]['reserved_paths']
    assert all(not (feature/p).exists() for p in result['reserved_paths'])
    monkeypatch.setattr(scope,'PROJECT_ROOT',feature)
    for name in ('04_scripts/notifications/preview.py','04_scripts/notifications/README',
                 '08_tests/alerts/test_core.py','02_configs/notifications/default.json',
                 '07_docs/projects/notification/contract.md'):
        path=feature/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('# fixture\n')
    assert scope.main(['--project','notification-push-fixture'])==0
    assert json.loads(capsys.readouterr().out)['PROJECT_SCOPE']=='PASS'
    for name in ('03_src/agri_research_agent/pipelines/lutou_weather.py',
                 '03_src/agri_research_agent/pipelines/public_data_daily.py',
                 '04_scripts/common.py','07_docs/projects/canola/contract.md',
                 '04_scripts/weather_producer/foo.py'):
        path=feature/name
        existed=path.exists()
        original=path.read_bytes() if existed else None
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('# forbidden change\n')
        assert scope.main(['--project','notification-push-fixture'])==1
        assert json.loads(capsys.readouterr().out)['PROJECT_SCOPE']=='FAIL'
        if existed: path.write_bytes(original)
        else: path.unlink()
    assert registry.git(main,'status','--porcelain')==''
    assert (feature/registry.REGISTRY_PATH).read_bytes()==(main/registry.REGISTRY_PATH).read_bytes()


@pytest.mark.parametrize('value',['/tmp/x','C:/tmp/x','../x','04_scripts/../x','.',
    '04_scripts','07_docs/projects','03_src/agri_research_agent','**','04_scripts/**',
    '04_scripts/./x','04_scripts//x','missing/leaf','04_scripts/missing/leaf',
    '04_scripts/NUL','04_scripts/x.','04_scripts/x ','04_scripts/a.py'])
def test_reserved_invalid_namespace(repository,value):
    _,root=repository
    data=reservation_registry(root)
    data['projects'][0]['reserved_paths']=[value]
    with pytest.raises(ValueError): registry.validate_registry(data,root)


@pytest.mark.parametrize('existing, reserved',[
    ('04_scripts','04_scripts/notifications'),
    ('04_scripts/notifications','04_scripts/notifications'),
    ('04_scripts/notifications/child','04_scripts/notifications'),
    ('04_scripts/Notifications','04_scripts/notifications')])
def test_reserved_conflicts_existing_owner(repository,existing,reserved):
    _,root=repository
    data=reservation_registry(root)
    (root/existing).mkdir(parents=True,exist_ok=True)
    other=copy.deepcopy(data['projects'][0])
    other.update(project_id='other-owner',owned_paths=[existing],reserved_paths=[],
                 future_required_tests=[],required_tests=['tests/test_feature.py'])
    data['projects'].append(other)
    with pytest.raises(ValueError,match='collision'): registry.validate_registry(data,root)


def test_reserved_docs_siblings_and_case_collision(repository):
    _,root=repository
    data=reservation_registry(root)
    other=copy.deepcopy(data['projects'][0])
    other.update(project_id='canola-fixture',owned_paths=[],reserved_paths=['07_docs/projects/canola'],
                 required_tests=['tests/test_feature.py'],future_required_tests=[])
    data['projects'].append(other)
    registry.validate_registry(data,root)
    for value in ['07_docs/projects/notification','07_docs/projects/NOTIFICATION','07_docs/projects']:
        other['reserved_paths']=[value]
        with pytest.raises(ValueError): registry.validate_registry(data,root)


def test_reserved_readonly_protected_and_normal_owned_missing(repository):
    _,root=repository
    data=reservation_registry(root)
    for value in ['shared/new','03_src/agri_research_agent/market_data']:
        data['projects'][0]['reserved_paths']=[value, '08_tests/alerts']
        with pytest.raises(ValueError,match='read-only|protected'): registry.validate_registry(data,root)
    data=reservation_registry(root)
    data['projects'][0]['owned_paths']=['04_scripts/notifications']
    with pytest.raises(ValueError,match='missing'): registry.validate_registry(data,root)


def test_reserved_link_rejected(repository,tmp_path):
    import os
    _,root=repository
    data=reservation_registry(root)
    outside=tmp_path/'outside'
    outside.mkdir()
    link=root/'04_scripts/notifications'
    if os.name=='nt':
        subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],check=True,capture_output=True)
    else: link.symlink_to(outside,target_is_directory=True)
    try:
        with pytest.raises(ValueError,match='link'): registry.validate_registry(data,root)
    finally:
        if os.name=='nt': link.rmdir()
        else: link.unlink()


def test_real_records_compatibility_and_docs_partition():
    baseline=json.loads(registry.git(ROOT,'show',f'HEAD:{registry.REGISTRY_PATH}'))
    current=registry.load_registry(ROOT)
    current_by_id = {p['project_id']: p for p in current['projects']}
    for old in baseline['projects']:
        new = current_by_id[old['project_id']]
        if old['project_id']!='dev-governance': assert old==new
        else:
            assert {k:v for k,v in old.items() if k!='owned_paths'}=={k:v for k,v in new.items() if k!='owned_paths'}
            assert '07_docs' not in new['owned_paths']
            expected=list((ROOT/'07_docs').glob('0[0-6]_*.md'))+list((ROOT/'07_docs/templates').glob('*.md'))
            expected.append(ROOT/'07_docs/projects/生产只读检出实例登记.md')
            assert all(registry.owns(new,p.relative_to(ROOT).as_posix()) for p in expected)
            assert not registry.owns(new,'07_docs/projects/notification/contract.md')
        for name in subprocess.check_output(['git','-C',str(ROOT),'ls-files','-z']).decode('utf-8').split('\0'):
            if not name: continue
            before=registry.owns(old,name)
            after=registry.owns(new,name)
            assert not after or before
            if old['project_id']!='dev-governance': assert before==after


def test_normal_ownership_collision_is_rejected(repository):
    _,root=repository
    data=minimal_registry()
    other=copy.deepcopy(data['projects'][0]);other['project_id']='second'
    data['projects'].append(other)
    with pytest.raises(ValueError,match='collision'): registry.validate_registry(data,root)


def test_reserved_only_ready_and_required_tests_still_mandatory(repository):
    _,root=repository
    data=reservation_registry(root)
    p=data['projects'][0]
    p['owned_paths']=[]
    registry.validate_registry(data,root)
    assert p['status']=='ready'
    assert not (root/'08_tests/alerts').exists()
    p['future_required_tests']=[]
    with pytest.raises(ValueError,match='tests'): registry.validate_registry(data,root)


def test_reservation_cannot_claim_governance_docs(repository):
    _,root=repository
    data=reservation_registry(root)
    (root/'07_docs/templates').mkdir()
    owner=copy.deepcopy(data['projects'][0])
    owner.update(project_id='governance-fixture',change_class='shared',owned_paths=['07_docs/templates'],
                 reserved_paths=[],future_required_tests=[],required_tests=['tests/test_feature.py'])
    data['projects'].append(owner)
    data['projects'][0]['reserved_paths'].append('07_docs/templates')
    with pytest.raises(ValueError,match='collision|protected'): registry.validate_registry(data,root)


def test_reserved_ancestor_of_exact_future_file_conflicts(repository):
    _,root=repository
    data=reservation_registry(root)
    other=copy.deepcopy(data['projects'][0])
    other.update(project_id='exact-owner',owned_paths=[],reserved_paths=[],
                 future_owned_paths=['04_scripts/notifications/preview.py'],
                 future_required_tests=[],required_tests=['tests/test_feature.py'])
    data['projects'].append(other)
    with pytest.raises(ValueError,match='collision'): registry.validate_registry(data,root)


def test_existing_project_scope_classification_unchanged():
    before=json.loads(registry.git(ROOT,'show',f'HEAD:{registry.REGISTRY_PATH}'))
    after=registry.load_registry(ROOT)
    after_by_id = {p['project_id']: p for p in after['projects']}
    for old in before['projects']:
        new = after_by_id[old['project_id']]
        probes=old['owned_paths']+old.get('future_owned_paths',[])
        probes=probes+['04_scripts/not_owned.py','03_src/agri_research_agent/pipelines/public_data_daily.py']
        for path in probes:
            if old['project_id']=='dev-governance' and path=='07_docs': continue
            def synthetic_git(root,*args):
                if args==('diff','--name-only','--no-renames','base...HEAD'): return path+'\n'
                if args[:1]==('rev-parse',): return 'fixture-head'
                return ''
            def outcome(project):
                try:
                    return scope.run_audit(ROOT,'base',project['owned_paths'],
                        exact_allowed=project.get('future_owned_paths',[]),
                        change_class=project['change_class'],git=synthetic_git)['PROJECT_SCOPE']
                except ValueError as exc:
                    return ('REJECTED', str(exc))
            assert outcome(old)==outcome(new),(old['project_id'],path)


def test_notification_registration_contract():
    data, project = registry.select_project(ROOT, 'notification-push')
    baseline = json.loads(registry.git(ROOT, 'show', f'HEAD:{registry.REGISTRY_PATH}'))
    assert [p for p in data['projects'] if p['project_id'] != 'notification-push'] == [p for p in baseline['projects'] if p['project_id'] != 'notification-push']
    assert data['schema_version'] == baseline['schema_version'] == 'project-registry/3'
    assert data['protected_paths'] == baseline['protected_paths']
    assert project['change_class'] == 'business' and project['status'] == 'ready'
    assert project['owned_paths'] == ['03_src/agri_research_agent/alerts']
    assert project['reserved_paths'] == ['04_scripts/notifications', '08_tests/alerts', '02_configs/notifications', '07_docs/projects/notification']
    assert project['future_required_tests'] == ['08_tests/alerts/test_notification.py']
    assert len(project['shared_dependencies']) == 9
    assert all((ROOT/p).is_file() and not registry.owns(project,p) for p in project['shared_dependencies'])
    assert 'Soybean Notification BLOCKED' in project['boundary_notes']
    assert '06_outputs/push_logs' in project['boundary_notes']


def test_notification_registered_scope_in_temporary_git(tmp_path, monkeypatch, capsys):
    _, project = registry.select_project(ROOT, 'notification-push')
    main = tmp_path/'main'
    main.mkdir()
    for name in project['owned_paths'] + project['shared_dependencies'] + project['forbidden_paths']:
        path=main/name
        if (ROOT/name).is_file():
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('# fixture\n')
        else:
            path.mkdir(parents=True,exist_ok=True)
            (path/'fixture_anchor.txt').write_text('# fixture\n')
    for name in ['03_src/agri_research_agent/alerts/__init__.py','04_scripts/anchor.py',
                 '08_tests/test_anchor.py','02_configs/anchor.json','07_docs/projects/anchor.md']:
        path=main/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# fixture\n')
    data={'schema_version':'project-registry/3','protected_paths':[], 'projects':[copy.deepcopy(project)]}
    write_registry(main,data)
    registry.validate_registry(data,main)
    registry.git(main,'init','-b','main')
    registry.git(main,'config','user.name','Scope Fixture')
    registry.git(main,'config','user.email','fixture@example.invalid')
    registry.git(main,'add','.')
    registry.git(main,'-c','commit.gpgsign=false','commit','-m','fixture approved registration')
    registry.git(main,'update-ref','refs/remotes/origin/main','HEAD')
    feature=tmp_path/'feature'
    registry.git(main,'worktree','add','-b','feat/notification',str(feature),'HEAD')
    monkeypatch.setattr(scope,'PROJECT_ROOT',feature)
    positives=['03_src/agri_research_agent/alerts/new_module.py','04_scripts/notifications/preview.py',
               '08_tests/alerts/test_notification.py','02_configs/notifications/channel.yaml',
               '07_docs/projects/notification/contract.md']
    negatives=project['shared_dependencies']+[
        '03_src/agri_research_agent/pipelines/lutou_weather.py',
        '03_src/agri_research_agent/pipelines/lutou_domestic_basis.py',
        '03_src/agri_research_agent/pipelines/public_data_daily.py',
        '04_scripts/automation/run_full_daily_windows.ps1',
        '03_src/agri_research_agent/import_profit/market_snapshot.py',
        '03_src/agri_research_agent/pipelines/public_data_prewarm.py',
        '03_src/agri_research_agent/pipelines/public_data_providers.py',
        '07_docs/03_标准开发与生产发布规范.md',
        '07_docs/projects/other.md','07_docs/projects/canola/contract.md']
    for name in positives+negatives:
        path=feature/name;original=path.read_bytes() if path.is_file() else None
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# simulated change\n')
        expected='PASS' if name in positives else 'FAIL'
        assert scope.main(['--project','notification-push']) == (0 if expected=='PASS' else 1), name
        assert json.loads(capsys.readouterr().out)['PROJECT_SCOPE']==expected, name
        if original is None:path.unlink()
        else:path.write_bytes(original)
    assert registry.git(main,'status','--porcelain')==''
    assert registry.git(feature,'status','--porcelain')==''
