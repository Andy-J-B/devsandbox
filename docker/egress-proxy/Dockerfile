# syntax=docker/dockerfile:1
#
# DevSandbox egress proxy (allowlist network mode).
#
# The sandbox manager starts one proxy container per allowlisted attempt on an
# internal Docker network.  The agent has no other route off that network, so
# every request it makes must pass through this proxy's allowlist.
#
# Build with:
#   docker/egress-proxy/build.sh
#
# Production deployments should pin the image by digest and enable
# security.require_pinned_images.

# Base image pinned by digest (python:3.12-slim).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Apply Debian security updates for packages that were current when the base
# digest was published (for example zlib).
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# Non-root user.  The sandbox manager overrides the uid/gid at run time, but
# this keeps `docker run` usable for smoke tests.  Some python:* images already
# ship a uid-1000 account, so rename it rather than failing on a duplicate id.
RUN set -eux; \
    if getent passwd 1000 >/dev/null; then \
        current_user="$(getent passwd 1000 | cut -d: -f1)"; \
        current_group="$(getent group 1000 | cut -d: -f1)"; \
        usermod --login devsandbox --home /home/devsandbox "$current_user"; \
        if [ -n "$current_group" ] && [ "$current_group" != devsandbox ]; then \
            groupmod --new-name devsandbox "$current_group"; \
        fi; \
    else \
        useradd --uid 1000 --create-home --shell /usr/sbin/nologin devsandbox; \
    fi

WORKDIR /app
COPY --chown=1000:1000 docker/egress-proxy/proxy.py /app/proxy.py

USER 1000:1000
EXPOSE 8888

HEALTHCHECK --interval=5s --timeout=3s --start-period=2s --retries=6 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8888/healthz', timeout=2).status == 200 else 1)"]

CMD ["python", "/app/proxy.py"]
