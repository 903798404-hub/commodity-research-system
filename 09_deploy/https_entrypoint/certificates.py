"""IP certificate lifecycle; separate from market collection and application grants."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,ipaddress,json,os,ssl,subprocess

ROOT=Path('/var/lib/market-data/https-entrypoint')
CERTBOT='sha256:f0cfa0056d76325c6a2d3f4dc4dc41449763a9ee1000ed11caf8dc180450144d'
GATEWAY='market-data-https-entrypoint'

def public_ip(value):
    address=ipaddress.IPv4Address(value)
    if not address.is_global:raise ValueError('A public IP address is required')
    return str(address)

def protected(path):
    path=Path(path)
    for item in [path,*path.parents]:
        if item.is_symlink() or item.stat().st_uid!=0 or item.stat().st_mode&0o022:
            raise ValueError('Unprotected HTTPS state path')
    return path

def command(args,timeout=120,input=None):
    p=subprocess.run(args,input=input,capture_output=True,timeout=timeout)
    if p.returncode:raise RuntimeError('HTTPS operation failed: '+args[0])
    return p.stdout

def certificate(ip,config):
    directory=protected(config/'live/market-data-ip')
    # Certbot's live entries are its own managed symlinks. Resolve them only
    # within this protected account's archive; never follow arbitrary targets.
    source={}
    for name in ['cert.pem','chain.pem','fullchain.pem','privkey.pem']:
        path=(directory/name).resolve(strict=True)
        if not path.is_relative_to(config/'archive/market-data-ip'):
            raise ValueError('Certificate escaped its protected archive')
        source[name]=protected(path)
    command(['openssl','verify','-purpose','sslserver','-verify_ip',ip,'-CApath','/etc/ssl/certs','-untrusted',str(source['chain.pem']),str(source['cert.pem'])])
    certkey=command(['openssl','x509','-in',str(source['cert.pem']),'-pubkey','-noout'])
    privatekey=command(['openssl','pkey','-in',str(source['privkey.pem']),'-pubout'])
    if certkey!=privatekey:raise ValueError('Certificate and private key differ')
    info=ssl._ssl._test_decode_cert(str(source['cert.pem']))
    if ('IP Address',ip) not in info['subjectAltName']:raise ValueError('Certificate IP identity differs')
    until=datetime.fromtimestamp(ssl.cert_time_to_seconds(info['notAfter']),timezone.utc)
    if until.timestamp()-datetime.now(timezone.utc).timestamp()<36*3600:
        raise ValueError('Certificate has insufficient remaining life')
    der=ssl.PEM_cert_to_DER_cert(source['cert.pem'].read_text(encoding='ascii'))
    return source,hashlib.sha256(der).hexdigest(),until

def deploy_certificate(ip):
    ip=public_ip(ip);config=protected(ROOT/'acme')
    files,identity,until=certificate(ip,config)
    parent=protected(ROOT/'certificates');generation=parent/identity
    if not generation.exists():
        generation.mkdir(mode=0o750);os.chown(generation,0,101)
        for name in ['fullchain.pem','privkey.pem']:
            raw=files[name].read_bytes()
            with os.fdopen(os.open(generation/name,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o640),'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.chown(generation/name,0,101)
    else:
        protected(generation)
        for name in ['fullchain.pem','privkey.pem']:
            if (generation/name).read_bytes()!=files[name].read_bytes():raise ValueError('Retained certificate generation differs')
    current=parent/'current';old=os.readlink(current) if current.is_symlink() else None
    if old==identity:return {'certificate_sha256':identity,'expires_at':until.isoformat(),'reload_required':False}
    pending=parent/'.next';os.symlink(identity,pending);os.replace(pending,current)
    try:
        command(['docker','exec',GATEWAY,'nginx','-t','-c','/gateway/nginx.conf'])
        command(['docker','kill','--signal','HUP',GATEWAY])
    except Exception:
        if old is not None:os.symlink(old,pending);os.replace(pending,current)
        raise
    return {'certificate_sha256':identity,'expires_at':until.isoformat(),'reload_required':True}

def certbot(ip,mode):
    ip=public_ip(ip)
    if mode not in {'staging','issue','renew','dry-run'}:raise ValueError('Unknown certificate operation')
    account=protected(ROOT/('acme-staging' if mode=='staging' else 'acme'))
    args=['docker','run','--rm','--pull','never','--name','market-data-ip-certificate-job','--network','bridge',
        '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','192m','--memory-swap','192m','--cpus','0.5','--pids-limit','64',
        '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=32m','--log-driver','json-file','--log-opt','max-size=20m','--log-opt','max-file=5',
        '--mount',f'type=bind,src={account},dst=/etc/letsencrypt',
        '--mount',f'type=bind,src={ROOT}/challenge,dst=/challenge',
        '--mount',f'type=bind,src={ROOT}/certificate-work,dst=/var/lib/letsencrypt',
        '--mount',f'type=bind,src={ROOT}/certificate-logs,dst=/var/log/letsencrypt',CERTBOT]
    if mode in {'staging','issue'}:
        args+=['certonly','--non-interactive','--agree-tos','--register-unsafely-without-email','--cert-name','market-data-ip','--preferred-profile','shortlived','--webroot','--webroot-path','/challenge','--ip-address',ip]
        if mode=='staging':args+=['--staging']
    else:
        args+=['renew','--non-interactive','--cert-name','market-data-ip']
        if mode=='dry-run':args+=['--dry-run']
    args+=['--max-log-backups','3']
    # Certbot has no Docker socket, production app mount or deploy hook.
    command(args,timeout=360)
    return deploy_certificate(ip) if mode in {'issue','renew'} else {'mode':mode,'certificate_deployed':False}
