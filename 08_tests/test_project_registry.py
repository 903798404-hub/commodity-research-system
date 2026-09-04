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
