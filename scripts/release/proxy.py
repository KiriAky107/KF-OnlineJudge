"""Bounded HTTPS proxy composition, separate from installation and health."""

import re
from pathlib import Path
from urllib.parse import urlsplit

from . import configuration, manifest
from .policy import DeliveryError, canonical, digest, fields, read_json, sha256


def validate_profile(value: dict) -> dict:
    value = fields(
        value,
        {"schema_version", "origin", "certificate_sha256", "key_sha256"},
        "proxy profile",
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise DeliveryError("unsupported proxy profile")
    origin = value["origin"]
    if not isinstance(origin, str) or len(origin) > 253:
        raise DeliveryError("invalid proxy origin")
    parts = urlsplit(origin)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or parts.hostname != parts.hostname.lower()
        or origin != "https://" + parts.hostname
        or len(parts.hostname) > 253
        or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in parts.hostname.split(".")
        )
    ):
        raise DeliveryError("canonical HTTPS DNS origin required")
    sha256(value["certificate_sha256"])
    sha256(value["key_sha256"])
    if value["certificate_sha256"] == value["key_sha256"]:
        raise DeliveryError("certificate and private-key identities overlap")
    return value


def render(release_id: str, value: dict) -> bytes:
    """No credentials, arbitrary includes, upstreams, paths or limit overrides."""
    value = validate_profile(value)
    if not re.fullmatch(r"sd-[0-9a-f]{12}", release_id):
        raise DeliveryError("invalid proxy release identity")
    host = urlsplit(value["origin"]).hostname
    # Only the canonical ZIP route gets the existing 64-MiB upload allowance.
    upload = (
        r"^/api/v1/admin/problem-imports/"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/archive$"
    )
    proxy = """
            proxy_pass http://127.0.0.1:8000;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host HOST;
            proxy_set_header Forwarded "";
            proxy_set_header X-Forwarded-For $remote_addr;
            proxy_set_header X-Forwarded-Host "";
            proxy_set_header X-Forwarded-Proto https;
            proxy_connect_timeout 3s;
            proxy_send_timeout 10s;
            proxy_read_timeout 60s;
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_cache off;
            proxy_hide_header Cache-Control;
            add_header Cache-Control "no-store" always;
    """.replace("HOST", host)
    return (
        """worker_processes 1;
pid /run/kf-proxy/nginx.pid;
error_log /dev/null crit;
events { worker_connections 128; }
http {
    types {
        text/html html; text/css css; application/javascript js mjs;
        application/json json; image/svg+xml svg; image/png png;
        image/jpeg jpg jpeg; image/x-icon ico; font/woff2 woff2;
    }
    default_type application/octet-stream;
    log_format bounded '$request_method $status $body_bytes_sent $request_time';
    access_log syslog:server=unix:/dev/log,tag=kf_proxy,nohostname,severity=info bounded;
    server_tokens off;
    limit_conn_zone $binary_remote_addr zone=per_client:64k;
    limit_conn_zone $server_name zone=per_site:64k;
    limit_conn_status 429;
    client_header_timeout 10s;
    client_body_timeout 10s;
    client_body_buffer_size 16k;
    client_body_temp_path /var/lib/kf-onlinejudge/proxy/body 1 2;
    proxy_temp_path /var/lib/kf-onlinejudge/proxy/proxy 1 2;
    fastcgi_temp_path /var/lib/kf-onlinejudge/proxy/fastcgi 1 2;
    uwsgi_temp_path /var/lib/kf-onlinejudge/proxy/uwsgi 1 2;
    scgi_temp_path /var/lib/kf-onlinejudge/proxy/scgi 1 2;
    large_client_header_buffers 2 8k;
    send_timeout 10s;
    keepalive_timeout 15s;
    keepalive_requests 100;
    lingering_time 10s;
    lingering_timeout 2s;
    reset_timedout_connection on;
    server { listen 443 ssl default_server; ssl_reject_handshake on; }
    server {
        listen 443 ssl;
        server_name HOST;
        ssl_certificate /etc/kf-proxy/tls/fullchain.pem;
        ssl_certificate_key /etc/kf-proxy/tls/key.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_session_cache shared:TLS:1m;
        ssl_session_timeout 5m;
        ssl_session_tickets off;
        if ($host != "HOST") { return 421; }
        root /opt/kf-onlinejudge/releases/RELEASE/payload/frontend;
        autoindex off;
        client_max_body_size 1m;
        limit_conn per_client 8;
        limit_conn per_site 32;
        add_header X-Content-Type-Options nosniff always;
        add_header X-Frame-Options DENY always;
        location = /api/v1/health/live {
            limit_except GET { deny all; }
            PROXY
        }
        location ~ "UPLOAD" {
            client_max_body_size 64m;
            limit_except PUT { deny all; }
            PROXY
        }
        location ~ ^/api/v1/(auth|problems|submissions|admin)(/|$) {
            PROXY
        }
        location ~ ^/(api|internal)(/|$) { return 404; }
        location ~ /\\. { return 404; }
        location ~ \\.(js|mjs|css|woff2|svg|png|jpg|jpeg|ico|json|map)$ {
            try_files $uri =404;
            add_header Cache-Control "public, max-age=31536000, immutable";
        }
        location = /admin { return 308 /admin/; }
        location /admin/ {
            try_files $uri $uri/ /admin/index.html;
            add_header Cache-Control "no-cache" always;
        }
        location / {
            try_files $uri $uri/ /index.html;
            add_header Cache-Control "no-cache" always;
        }
    }
}
""".replace("HOST", host)
        .replace("RELEASE", release_id)
        .replace("UPLOAD", upload)
        .replace("PROXY", proxy)
    ).encode()


def generate(bundle: Path, expected: str, profile: Path, output: Path) -> dict:
    data = manifest.verify(bundle, expected)
    configuration.private(profile)
    value = validate_profile(read_json(profile, 4096))
    if output.exists() or output.is_symlink():
        raise DeliveryError("proxy output must be new")
    contents = render(data["release_id"], value)
    output.mkdir(mode=0o700)
    member = output / "nginx.conf"
    member.write_bytes(contents)
    member.chmod(0o600)
    receipt = {
        "schema_version": 1,
        "source_sha": data["source_sha"],
        "manifest_sha256": expected,
        "origin": value["origin"],
        "tls": {name: value[name] for name in ("certificate_sha256", "key_sha256")},
        "configuration_sha256": digest(member),
        "installed": False,
        "tls_verified": False,
        "services_started": False,
        "execution_verified": False,
    }
    (output / "receipt.json").write_bytes(canonical(receipt))
    (output / "receipt.json").chmod(0o600)
    return receipt
