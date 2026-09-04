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
