"""Guardrails for the proxy-first API deployment assets.

TLS terminates at the bundled nginx proxy, never in uvicorn.  These checks keep
the assets from silently losing TLS enforcement, authentication-header
forwarding, SSE-safe proxying, or container hardening.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_PROXY = _ROOT / "docker" / "api-proxy"
_NGINX = _PROXY / "nginx.conf"
_OVERRIDE = _PROXY / "compose.override.yaml"
_BASE_COMPOSE = _ROOT / "docker" / "compose.yaml"
_DEPLOYMENT = _ROOT / "docs" / "deployment.md"

_SSE_LOCATION = re.compile(
    r"location ~ \^/v1/projects/\[\^/\]\+/events/stream\$ \{(?P<body>.*?)\n        \}",
    re.DOTALL,
)
_ROOT_LOCATION = re.compile(r"location / \{(?P<body>.*?)\n        \}", re.DOTALL)


def _nginx() -> str:
    return _NGINX.read_text(encoding="utf-8")


def _compose(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _proxy_service() -> dict[str, Any]:
    return _compose(_OVERRIDE)["services"]["api-proxy"]


def test_proxy_terminates_tls_and_redirects_plaintext() -> None:
    text = _nginx()

    assert "listen 8443 ssl;" in text
    assert "http2 on;" in text
    assert re.search(r"ssl_protocols\s+TLSv1\.2 TLSv1\.3;", text)
    assert "listen 8080;" in text
    assert "return 308 https://$host$request_uri;" in text
    # Legacy TLS versions must stay disabled.
    assert "TLSv1 " not in text
    assert "TLSv1.1" not in text


def test_proxy_sets_security_headers_and_hides_version() -> None:
    text = _nginx()

    assert 'Strict-Transport-Security "max-age=31536000; includeSubDomains" always' in text
    assert 'X-Content-Type-Options "nosniff" always' in text
    assert 'X-Frame-Options "DENY" always' in text
    assert 'Referrer-Policy "no-referrer" always' in text
    assert re.search(r"server_tokens\s+off;", text)


def test_proxy_forwards_authorization_but_not_in_access_log() -> None:
    text = _nginx()

    forwarded = re.findall(
        r"proxy_set_header\s+Authorization\s+\$http_authorization;",
        text,
    )
    assert len(forwarded) == 2, "both the SSE and general locations must forward auth"

    log_format = re.search(r"log_format main (?P<body>.*?);", text, re.DOTALL)
    assert log_format is not None
    assert "$http_authorization" not in log_format.group("body")
    assert "authorization" not in log_format.group("body").lower()
    assert "access_log /dev/stdout main;" in text


def test_proxy_streams_sse_without_buffering() -> None:
    text = _nginx()

    sse = _SSE_LOCATION.search(text)
    assert sse is not None, "SSE location block is missing"
    block = sse.group("body")
    assert re.search(r"proxy_buffering\s+off;", block)
    assert re.search(r"proxy_cache\s+off;", block)
    assert re.search(r"proxy_read_timeout\s+3600s;", block)

    root_location = _ROOT_LOCATION.search(text)
    assert root_location is not None
    assert re.search(r"proxy_buffering\s+on;", root_location.group("body"))


def test_proxy_caps_bodies_and_applies_an_edge_rate_limit() -> None:
    text = _nginx()

    assert re.search(r"client_max_body_size\s+2m;", text)
    assert re.search(
        r"limit_req_zone \$binary_remote_addr zone=api_per_ip:10m rate=20r/s;",
        text,
    )
    assert re.search(r"limit_req_status\s+429;", text)
    limited = re.findall(r"limit_req zone=api_per_ip burst=40 nodelay;", text)
    assert len(limited) == 2


def test_proxy_targets_the_private_compose_service() -> None:
    text = _nginx()

    assert "server orchestrator:8000;" in text
    assert "proxy_pass http://devsandbox_api;" in text
    # The shipped config must not point at a host-local upstream.
    assert "127.0.0.1:8000" not in text


def test_override_publishes_only_proxy_ports() -> None:
    override = _compose(_OVERRIDE)
    assert set(override["services"]) == {"api-proxy"}

    proxy = override["services"]["api-proxy"]
    assert proxy["ports"] == ["80:8080", "443:8443"]
    assert proxy["depends_on"]["orchestrator"]["condition"] == "service_started"


def test_base_compose_keeps_every_service_on_loopback() -> None:
    compose = _compose(_BASE_COMPOSE)

    assert compose["services"]["orchestrator"]["ports"] == ["127.0.0.1:8000:8000"]
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)


def test_proxy_container_is_read_only_and_unprivileged() -> None:
    proxy = _proxy_service()

    assert proxy["read_only"] is True
    assert proxy["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in proxy["security_opt"]
    assert proxy["tmpfs"] == ["/tmp:mode=1777"]

    volumes = proxy["volumes"]
    assert "./api-proxy/nginx.conf:/etc/nginx/nginx.conf:ro" in volumes
    assert "./api-proxy/tls/server.crt:/etc/nginx/tls/server.crt:ro" in volumes
    assert "./api-proxy/tls/server.key:/etc/nginx/tls/server.key:ro" in volumes
    assert all(str(volume).endswith(":ro") for volume in volumes)


def test_proxy_image_is_not_floating() -> None:
    image = _proxy_service()["image"]

    assert image == "nginxinc/nginx-unprivileged:1.27-alpine"
    assert not image.endswith(":latest")
    images_doc = (_ROOT / "docs" / "images.md").read_text(encoding="utf-8")
    assert "nginxinc/nginx-unprivileged" in images_doc
    assert "pin it by digest" in images_doc.lower()


def test_local_tls_material_is_gitignored() -> None:
    text = (_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "docker/api-proxy/tls/" in text


def test_deployment_docs_require_proxy_tls_and_reject_app_tls() -> None:
    normalized = " ".join(_DEPLOYMENT.read_text(encoding="utf-8").split())

    assert "docker/api-proxy/" in normalized
    assert "docker compose -f docker/compose.yaml" in normalized
    assert "Direct TLS in the app is intentionally unsupported." in normalized
    assert "Do not add `ssl_certfile`/`ssl_keyfile` settings" in normalized


def test_app_has_no_direct_tls_configuration() -> None:
    sources = [
        _ROOT / "src" / "devsandbox" / "config" / "settings.py",
        _ROOT / "src" / "devsandbox" / "api" / "server.py",
    ]

    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert "ssl_certfile" not in text, source
        assert "ssl_keyfile" not in text, source
        assert "ssl_cert_reqs" not in text, source
