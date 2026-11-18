# API TLS reverse proxy

This optional compose override puts an unprivileged nginx in front of the
DevSandbox API. The FastAPI/uvicorn process intentionally does **not** terminate
TLS: it stays on the private compose network at `orchestrator:8000`, and only
the proxy publishes ports 80 and 443. Port 80 permanently redirects to HTTPS.

## Files

- `nginx.conf` — full nginx config for `nginxinc/nginx-unprivileged`; enables
  TLS 1.2/1.3, HSTS and the other security headers, forwards `Authorization`
  unchanged, caps request bodies at 2 MiB, disables response buffering for the
  SSE event stream, and applies a per-IP edge rate limit.
- `compose.override.yaml` — adds the proxy to `docker/compose.yaml`, keeps the
  root filesystem read-only, drops all capabilities, and mounts the config and
  TLS material read-only.
- `tls/` — local certificate material. It is intentionally ignored by Git.

## Usage

From the repository root:

```bash
mkdir -p docker/api-proxy/tls

# Development/self-signed example. Use a CA-issued certificate in production.
openssl req -x509 -newkey rsa:4096 -sha256 -days 365 -nodes \
  -keyout docker/api-proxy/tls/server.key \
  -out docker/api-proxy/tls/server.crt \
  -subj "/CN=devsandbox.internal.example" \
  -addext "subjectAltName=DNS:devsandbox.internal.example"

# The unprivileged nginx image runs as UID 101 and must be able to read the key.
sudo chown 101:101 docker/api-proxy/tls/server.key
sudo chmod 600 docker/api-proxy/tls/server.key
chmod 644 docker/api-proxy/tls/server.crt

export DEVSANDBOX_API_TOKEN="$(openssl rand -hex 32)"
docker compose -f docker/compose.yaml \
  -f docker/api-proxy/compose.override.yaml up -d
```

Verify the redirect and the authenticated endpoint (`-k` is only for the
self-signed development certificate above):

```bash
curl -I http://localhost/
curl -kfsS https://localhost/healthz
curl -kfsS -H "Authorization: Bearer $DEVSANDBOX_API_TOKEN" \
  https://localhost/v1/projects
```

The base compose file still publishes the API on `127.0.0.1:8000` for local
checks. That mapping is loopback-only; never widen it to `0.0.0.0` or bypass the
proxy for remote access.

## Notes and limits

- The proxy forwards bearer tokens but does not validate them. The app remains
  the authentication and authorization boundary.
- `nginx.conf` targets the compose service name `orchestrator:8000`. If the API
  runs directly on the host (`devsandbox serve`), either run nginx on the host
  and change the upstream to `127.0.0.1:8000`, or use
  `host.docker.internal:8000` from a proxy container on Docker Desktop.
- Access logs intentionally omit request headers, including `Authorization`.
  Do not add `$http_authorization` or a `combined`-style header log.
- HSTS is enabled, including `includeSubDomains`. If the proxy hostname has
  sibling subdomains that are not HTTPS-only, remove `includeSubDomains` before
  rollout. Only serve this proxy over HTTPS, and use a hostname covered by the
  certificate.
- The `chown 101:101` step is for Linux hosts. Docker Desktop usually handles
  bind-mount ownership through its file-sharing layer.
- The per-IP limiter sees the proxy's peer address. If another load balancer or
  CDN is in front of nginx, configure `real_ip`/trusted proxies so the limit is
  not applied to the load balancer's address.
- Pin `nginxinc/nginx-unprivileged` by digest before production use; see
  [docs/images.md](../../docs/images.md).
- The TLS key is mounted into the proxy container, so anyone with Docker API
  access on the host can read it. Protect the host file and the Docker socket.
