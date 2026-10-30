from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / '09_deploy/https_entrypoint' / (name + '.py')
    spec = importlib.util.spec_from_file_location('_https_test_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('address', ['127.0.0.1','10.0.0.1','192.0.2.1','::1','124.222.106.25;return 200;'])
def test_gateway_rejects_non_public_or_injected_addresses(address):
    with pytest.raises(ValueError):
        load('configuration').configuration(address, tls=True)


def test_public_gateway_keeps_native_paths_uploads_and_websockets():
    module = load('configuration')
    text = module.configuration('124.222.106.25', tls=True)
    assert 'listen 8443 ssl' in text and 'TLSv1.2 TLSv1.3' in text
    assert 'proxy_pass http://host.docker.internal:8856/;' in text
    assert 'proxy_pass http://host.docker.internal:8080;' in text
    assert 'Upgrade $http_upgrade' in text and 'proxy_request_buffering off' in text
    assert 'client_max_body_size 0' in text and 'auth_basic' not in text
    unit = module.gateway_unit()
    assert '--user 101:101 --read-only --cap-drop ALL' in unit
    assert '--entrypoint' not in unit and '--log-opt max-size=20m --log-opt max-file=5' in unit
    assert '/01_data' not in unit and 'docker.sock' not in unit


@pytest.mark.parametrize('mode', ['staging','dry-run'])
def test_validation_certificate_never_deploys_or_has_business_access(monkeypatch, mode):
    module = load('certificates');calls=[]
    monkeypatch.setattr(module,'protected',lambda path:path)
    monkeypatch.setattr(module,'command',lambda args,**kwargs:calls.append(args) or b'')
    monkeypatch.setattr(module,'deploy_certificate',lambda *_:pytest.fail('Untrusted test certificate was deployed'))
    assert module.certbot('124.222.106.25',mode)['certificate_deployed'] is False
    args=calls[0]
    assert module.CERTBOT in args and '--read-only' in args and '--cap-drop' in args
    assert '--deploy-hook' not in args and all('/01_data' not in arg and 'docker.sock' not in arg for arg in args)
    assert ('--staging' if mode=='staging' else '--dry-run') in args


def test_certificate_cannot_read_an_unrelated_live_file(tmp_path, monkeypatch):
    module=load('certificates');config=tmp_path/'acme';live=config/'live/market-data-ip';live.mkdir(parents=True)
    (live/'cert.pem').write_text('Unrelated file')
    monkeypatch.setattr(module,'protected',lambda path:path)
    monkeypatch.setattr(module,'command',lambda *_args,**_kwargs:pytest.fail('Out of archive certificate was processed'))
    with pytest.raises(ValueError,match='escaped'):
        module.certificate('124.222.106.25',config)


def test_firewall_stops_before_any_rule_if_trusted_https_fails(monkeypatch):
    module=load('firewall')
    def failed(_ip):raise ValueError('TLS failed')
    monkeypatch.setattr(module,'tls_health',failed)
    monkeypatch.setattr(module,'call',lambda *_args,**_kwargs:pytest.fail('Firewall was changed before TLS validation'))
    with pytest.raises(ValueError,match='TLS failed'):
        module.install('124.222.106.25')


def test_unknown_firewall_chain_is_not_flushed_or_modified(monkeypatch):
    module=load('firewall');calls=[]
    monkeypatch.setattr(module,'tls_health',lambda _ip:None)
    monkeypatch.setattr(module,'interface',lambda:'eth0')
    def call(args,**_kwargs):
        calls.append(args)
        if args[0]=='ip':return SimpleNamespace(stdout='[]',returncode=0)
        if '-S' in args:return SimpleNamespace(stdout='-N MARKET_HTTPS_ONLY\n-A MARKET_HTTPS_ONLY -j DROP\n',returncode=0)
        if '-C' in args:return SimpleNamespace(stdout='',returncode=1)
        pytest.fail('Unknown existing policy was mutated')
    monkeypatch.setattr(module,'call',call)
    with pytest.raises(ValueError,match='unknown rules'):
        module.install('124.222.106.25')
    assert all('-F' not in cmd and '-A' not in cmd and '-I' not in cmd for cmd in calls)


def test_backend_rules_leave_replies_and_unrelated_services_outside_scope():
    module=load('firewall')
    assert set(module.PORTS)=={8501,8080,8081,8856,8858}
    assert all('--ctdir' in row and 'ORIGINAL' in row and '--ctorigdstport' in row for row in module.rules())


def report():
    names=['spread-dashboard','usda-dashboard','oil-world-dashboard','guangxi-weather-preview','yunnan-weather-preview','market-data-https-entrypoint']
    return dict(schema_version='public-https-status/1',checked_at=datetime.now(timezone.utc).isoformat(),status='CURRENT',
        public_url='https://124.222.106.25/',access='PUBLIC_READ_WRITE',legacy_ports_restricted=True,
        certificate={'sha256':'a'*64,'expires_at':(datetime.now(timezone.utc)+timedelta(days=5)).isoformat()},
        logs=[dict(service=n,bounded=True,maximum_megabytes=100) for n in names],errors=[])


@pytest.mark.parametrize('broken', ['certificate','logs','legacy','url','future'])
def test_status_page_rejects_incomplete_or_misleading_success(tmp_path, broken):
    sys.path.insert(0,str(ROOT/'05_apps'));page=importlib.import_module('page_monitor_page')
    value=report()
    if broken=='certificate':value['certificate']=None
    if broken=='logs':value['logs'].pop()
    if broken=='legacy':value['legacy_ports_restricted']=False
    if broken=='url':value['public_url']='http://124.222.106.25/'
    if broken=='future':value['checked_at']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    path=tmp_path/'status.json';path.write_text(json.dumps(value),encoding='utf8')
    with pytest.raises(ValueError):page.load_https_report(path)


def test_renewal_units_have_bounded_retries_and_no_shell_hooks():
    module=load('runtime');units=module.render_units('/opt/market-data-releases/example/source','/etc/market-data/https-entrypoint/example.json')
    assert 'RestartSec=15min' in units['market-data-https-renew.service']
    assert 'StartLimitBurst=3' in units['market-data-https-renew.service']
    assert '00,06,12,18:15:00' in units['market-data-https-renew.timer']
    with pytest.raises(ValueError):module.render_units('/opt/example;cmd','/etc/example.json')
