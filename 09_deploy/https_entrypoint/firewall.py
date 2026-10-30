"""Only legacy public Docker entry ports; no global firewall replacement."""
import json
import re
import shlex
import socket
import ssl
import subprocess

CHAIN = 'MARKET_HTTPS_ONLY'
PORTS = (8501, 8080, 8081, 8856, 8858)


def call(args, *, check=True):
    p = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if check:
        p.check_returncode()
    return p


def rules():
    return [('-p','tcp','-m','conntrack','--ctdir','ORIGINAL','--ctorigdstport',str(port),
             '-j','REJECT','--reject-with','tcp-reset') for port in PORTS]


def interface():
    rows = json.loads(call(['ip','-j','-4','route','show','default']).stdout)
    devices = {row['dev'] for row in rows}
    if len(devices) != 1 or not re.fullmatch(r'[a-zA-Z0-9_.:-]+', next(iter(devices))):
        raise ValueError('Public interface is ambiguous')
    return next(iter(devices))


def tls_health(ip):
    # Connect locally but verify the real public IP SAN and system CA chain.
    with socket.create_connection(('127.0.0.1',443),timeout=5) as connection:
        with ssl.create_default_context().wrap_socket(connection,server_hostname=ip) as tls:
            tls.sendall(('GET /gateway-health HTTP/1.1\r\nHost: '+ip+'\r\nConnection: close\r\n\r\n').encode('ascii'))
            if tls.recv(4096).split(b'\r\n',1)[0] != b'HTTP/1.1 200 OK':
                raise ValueError('Trusted HTTPS is not healthy')


def public_jump_first(dev):
    result = call(['iptables','-S','DOCKER-USER'],check=False)
    rows = [shlex.split(line) for line in result.stdout.splitlines() if line.startswith('-A ')]
    return result.returncode == 0 and bool(rows) and rows[0] == ['-A','DOCKER-USER','-i',dev,'-j',CHAIN]


def confirmed():
    try:
        dev = interface()
        if not public_jump_first(dev):
            return False
        own = call(['iptables','-S',CHAIN],check=False)
        if own.returncode or sum(line.startswith('-A ') for line in own.stdout.splitlines()) != len(PORTS):
            return False
        return all(call(['iptables','-C',CHAIN,*rule],check=False).returncode == 0 for rule in rules())
    except (OSError,ValueError,subprocess.SubprocessError):
        return False


def install(ip):
    tls_health(ip)
    # This deployment serves IPv4 only; do not claim IPv6 isolation if the host
    # later acquires a global IPv6 address that would need a separate policy.
    addresses = json.loads(call(['ip','-j','-6','addr','show','scope','global']).stdout)
    if any(row.get('addr_info') for row in addresses):
        raise ValueError('Global IPv6 needs independent entrypoint validation')
    dev = interface()
    existing = call(['iptables','-S',CHAIN],check=False)
    if existing.returncode:
        call(['iptables','-N',CHAIN])
    else:
        # Only a new chain or a prefix from our own interrupted construction
        # may be completed. Unknown preexisting rules never get flushed.
        count = sum(line.startswith('-A ') for line in existing.stdout.splitlines())
        known = sum(call(['iptables','-C',CHAIN,*rule],check=False).returncode==0 for rule in rules())
        # -S may reorder conntrack options; -C compares actual engine semantics.
        if count != known:
            raise ValueError('Owned firewall chain has unknown rules')
    for rule in rules():
        if call(['iptables','-C',CHAIN,*rule],check=False).returncode:
            call(['iptables','-A',CHAIN,*rule])
    # The rule applies only to incoming public-interface ORIGINAL connections.
    # Loopback, gateway bridge traffic, replies, SSH, 80/443 and other ports stay
    # under their original policies; existing chains are not flushed or saved over.
    if not public_jump_first(dev):
        if call(['iptables','-C','DOCKER-USER','-i',dev,'-j',CHAIN],check=False).returncode == 0:
            call(['iptables','-D','DOCKER-USER','-i',dev,'-j',CHAIN])
        call(['iptables','-I','DOCKER-USER','1','-i',dev,'-j',CHAIN])
    if not confirmed():
        raise ValueError('Legacy entrypoint firewall validation failed')
