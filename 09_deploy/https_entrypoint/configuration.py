"""Fixed-route configuration for the independent public HTTPS gateway."""
from ipaddress import IPv4Address

NGINX_IMAGE='sha256:43a775095fde460e747cc4799d2060c1953d588f6b896443051acccab1bb355f'
CERTBOT_IMAGE='sha256:f0cfa0056d76325c6a2d3f4dc4dc41449763a9ee1000ed11caf8dc180450144d'
ROUTES={'/usda/':(8080,False),'/oil-world/':(8081,False),'/guangxi-weather/':(8856,True),'/yunnan-weather/':(8858,True),'/':(8501,False)}

def routes():
 text='location = /gateway-health { access_log off; default_type text/plain; return 200 "ok"; }\n'
 for path,(port,strip) in ROUTES.items():
  if path!='/':text+='location = '+path[:-1]+' { return 308 '+path+'$is_args$args; }\n'
  text+='location '+path+' {\nproxy_pass http://host.docker.internal:'+str(port)+('/' if strip else '')+';\n}\n'
 return text

def configuration(ip,tls=False):
 address=IPv4Address(ip)
 if not address.is_global:raise ValueError('A public IP address is required')
 ip=str(address)
 common='''worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 1024; }
http {
 include /etc/nginx/mime.types;
 default_type application/octet-stream;
 server_tokens off;
 access_log off;
 client_body_temp_path /tmp/client-body;
 proxy_temp_path /tmp/proxy;
 fastcgi_temp_path /tmp/fastcgi;
 uwsgi_temp_path /tmp/uwsgi;
 scgi_temp_path /tmp/scgi;
 map $http_upgrade $connection_upgrade { default upgrade; '' close; }
 proxy_http_version 1.1;
 proxy_set_header Host $http_host;
 proxy_set_header Upgrade $http_upgrade;
 proxy_set_header Connection $connection_upgrade;
 proxy_set_header X-Forwarded-Proto $scheme;
 proxy_set_header X-Forwarded-Host $http_host;
 proxy_set_header X-Real-IP $remote_addr;
 proxy_set_header X-Forwarded-For $remote_addr;
 proxy_request_buffering off;
 proxy_buffering off;
 proxy_read_timeout 86400s;
 proxy_send_timeout 86400s;
 client_max_body_size 0;
'''
 if tls:
  common+='server { listen 8080; server_name '+ip+'; location ^~ /.well-known/acme-challenge/ { root /challenge; default_type text/plain; try_files $uri =404; }\nlocation = / { return 308 https://'+ip+'/guangxi-weather/$is_args$args; }\nlocation / { return 308 https://'+ip+'$request_uri; } }\n'
  common+='server { listen 8443 ssl; server_name '+ip+'; ssl_certificate /certificates/current/fullchain.pem; ssl_certificate_key /certificates/current/privkey.pem; ssl_protocols TLSv1.2 TLSv1.3; ssl_session_cache shared:TLS:1m; ssl_session_timeout 10m;\n'
 else:common+='server { listen 8080; server_name '+ip+'; location ^~ /.well-known/acme-challenge/ { root /challenge; default_type text/plain; try_files $uri =404; }\n'
 return common+routes()+'}\n}\n'


def gateway_unit():
 return '''[Unit]
Description=Unified public HTTPS entrypoint
Requires=docker.service
After=docker.service network-online.target
RequiresMountsFor=/var/lib/market-data/https-entrypoint

[Service]
Type=simple
ExecStart=/usr/bin/docker run --rm --name market-data-https-entrypoint --pull never --network market-data-https-entrypoint --add-host host.docker.internal:host-gateway --user 101:101 --read-only --cap-drop ALL --security-opt no-new-privileges --memory 128m --memory-swap 128m --cpus 0.5 --pids-limit 64 --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777 --log-driver json-file --log-opt max-size=20m --log-opt max-file=5 --mount type=bind,src=/var/lib/market-data/https-entrypoint/config,dst=/gateway,readonly --mount type=bind,src=/var/lib/market-data/https-entrypoint/challenge,dst=/challenge,readonly --mount type=bind,src=/var/lib/market-data/https-entrypoint/certificates,dst=/certificates,readonly --publish 0.0.0.0:80:8080 --publish 0.0.0.0:443:8443 '''+NGINX_IMAGE+''' nginx -c /gateway/nginx.conf -g "daemon off;"
ExecStop=/usr/bin/docker stop --time 15 market-data-https-entrypoint
Restart=always
RestartSec=5
TimeoutStopSec=25
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
'''
