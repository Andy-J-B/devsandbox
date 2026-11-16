# Deployment and Docker access

DevSandbox needs a Docker daemon to create agent and validation containers.
That is the most privileged part of the system: access to the daemon is
effectively host root. This document gives two deployment recipes that reduce
that exposure, plus the limits you must understand before using them.

The container sandbox itself is locked down (non-root, read-only rootfs,
`cap_drop: ALL`, no host mounts except the workspace). The risk described here
is the *orchestrator's* connection to the Docker API, not the agent container.

## 1. Threat model in one paragraph

A compromised orchestrator can create privileged containers and mount the host
filesystem. A compromised agent cannot: the sandbox manager never passes the
Docker socket or extra host mounts to agent/validator containers. So the
question is: how much can an attacker do with the Docker endpoint that the
orchestrator uses? Prefer, in order:

1. a dedicated host or VM (no unrelated workloads, no production credentials);
2. rootless Docker (the orchestrator has no host-root daemon to abuse);
3. a constrained Docker socket proxy in front of a dedicated daemon (smaller
   API surface, but **not** a substitute for rootless Docker);
4. a rootful socket mounted directly into the orchestrator container (local
   development only).

Never expose the Docker socket to an agent or validator container. The
DevSandbox images do not contain a Docker CLI, and the sandbox manager does not
mount the socket.

## 2. Option A — rootless Docker (recommended)

Rootless Docker runs the daemon as your unprivileged user. A compromised
orchestrator then has the same access as that user, not host root. It is a real
security boundary, with some operational constraints.

### Prerequisites

- Linux with cgroup v2 (`stat -fc %T /sys/fs/cgroup` prints `cgroup2fs`);
- `newuidmap` / `newgidmap` from `uidmap`, and subordinate UID/GID ranges in
  `/etc/subuid` and `/etc/subgid`;
- kernel support for unprivileged user namespaces;
- Docker Engine installed (the client is enough for some steps, but use the
  rootless daemon for the images DevSandbox runs).

On Debian/Ubuntu:

```bash
sudo apt-get install -y uidmap dbus-user-session
grep "^$(id -un):" /etc/subuid /etc/subgid
```

### Install and start

```bash
dockerd-rootless-setuptool.sh install
systemctl --user enable --now docker
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
docker info
docker run --rm hello-world
```

Add the `DOCKER_HOST` export to the environment that starts `devsandbox serve`
(or to a systemd user unit). Do not set it globally if other tools expect the
rootful daemon.

### Build the images in the rootless daemon

The agent, validator, and (for allowlist mode) egress-proxy images must exist
in the same daemon the orchestrator uses:

```bash
docker/agent-base/build.sh
docker build -t devsandbox/validator:latest docker/validator-base
docker/egress-proxy/build.sh
```

The per-project data root must be writable by your user and readable/writable
by containers. Rootless containers run as your user (with subordinate UID
mapping), so the default `DEVSANDBOX_DATA_ROOT=$PWD/.devsandbox` works.

```bash
export DEVSANDBOX_API_TOKEN="$(openssl rand -hex 32)"
export DEVSANDBOX_DATA_ROOT="$PWD/.devsandbox"
devsandbox serve
```

### Rootless constraints to verify

- `--privileged`, host networking, and some kernel features are unavailable or
  restricted. DevSandbox does not use them, but check `docker info` on your
  host.
- Low-numbered published ports are not available. DevSandbox does not publish
  agent/validator ports; the API listens on loopback from the host process.
- The rootless daemon uses `slirp4netns`/`pasta`. Verify that
  `--network none`, the default bridge, and user-defined **internal** networks
  work on your kernel. Allowlist mode creates a per-attempt internal network
  plus a dual-homed egress proxy; run `DEVSANDBOX_E2E=1 pytest -q
  tests/security/test_allowlist.py` on the deployment host before relying on it.
- Storage drivers can differ. Run the smoke checks in
  [quickstart.md](quickstart.md) and `scripts/check-all.sh` after switching.
- A rootless socket is owned by your user. If you run the orchestrator in a
  container, mount that socket path and run the container with a matching
  UID/GID; the default `docker-compose` setup assumes the rootful path
  `/var/run/docker.sock`.

Example override for a rootless host (replace the UID):

```yaml
services:
  orchestrator:
    user: "1000:1000"
    volumes:
      - /run/user/1000/docker.sock:/var/run/docker.sock
      - ${DEVSANDBOX_DATA_ROOT:-/var/lib/devsandbox}:${DEVSANDBOX_DATA_ROOT:-/var/lib/devsandbox}
```

Do not use this override with a rootful daemon; the socket permissions and the
container UID must match.

## 3. Option B — constrained Docker socket proxy

If you cannot use rootless Docker, put a filtering proxy in front of a
**dedicated** daemon. The orchestrator talks HTTP to the proxy; only the proxy
mounts the Docker socket.

```bash
cd docker/socket-proxy
docker compose up -d
export DOCKER_HOST=tcp://127.0.0.1:23750
devsandbox serve
```

The example proxy allows only the API groups DevSandbox uses (containers,
images, networks, exec, info/ping/version) and disables build, swarm, secrets,
configs, plugins, and volumes. Review
[`docker/socket-proxy/compose.yaml`](../docker/socket-proxy/compose.yaml) before
using it and pin the proxy image by digest.

**Important limits:**

- The proxy still allows `POST /containers/create`. A compromised orchestrator
  can ask for a privileged container or a host mount, so the proxy does **not**
  turn a compromised orchestrator into a non-root process. It reduces the
  surface and prevents accidental calls to unrelated APIs.
- `EXEC=1` is required for `SandboxManager.exec_in`; it allows exec in any
  container the daemon manages. Use a dedicated daemon.
- The proxy itself must be treated as a privileged service. Keep it on a
  private network, bind its published port to loopback, and do not expose it to
  agent containers.
- If you use a TCP endpoint across hosts, enable Docker TLS with client
  certificates (`DOCKER_TLS_VERIFY=1`, `DOCKER_CERT_PATH=...`) or use an SSH
  tunnel. Never expose an unauthenticated Docker API on a network interface.

## 4. Kernel hardening — seccomp, AppArmor, user namespaces

These controls are independent of the Docker endpoint and apply to every agent
and validator container.

### seccomp (on by default)

`security.seccomp_profile=hardened` is the default. The bundled profile is
Docker's default profile plus explicit denials for namespace, mount, tracing,
keyring, module, and kernel-attack syscalls. No host setup is required: the
profile is a read-only file shipped inside the Python package and passed to
Docker as `seccomp=<path>`.

```bash
# keep the default
# DEVSANDBOX_SECURITY__SECCOMP_PROFILE=hardened
```

To use a custom profile, point at an **absolute** path. The file must exist and
be readable before Docker is contacted; otherwise the attempt fails with a
`PolicyViolation` instead of falling back to a weaker profile.

```bash
export DEVSANDBOX_SECURITY__SECCOMP_PROFILE=/etc/devsandbox/seccomp.json
```

`docker-default` keeps Docker's built-in profile. `unconfined` disables seccomp
and should only be used to diagnose a compatibility problem on a disposable
host.

### AppArmor (opt-in)

Docker applies its `docker-default` profile automatically. For a stricter
profile, load the one in this repository on every Docker host:

```bash
sudo apparmor_parser -r -W docker/apparmor/devsandbox-agent
sudo aa-status | grep devsandbox-agent
export DEVSANDBOX_SECURITY__APPARMOR_PROFILE=devsandbox-agent
```

The profile denies mounts, `ptrace`, signals to unconfined processes, and writes
to `/proc/sys`, `/sys`, `/proc/kcore`, process memory, and `/dev/mem`. Keep the
setting unset on hosts without AppArmor (for example macOS Docker Desktop);
containers still get Docker's default profile and the hardened seccomp profile.

### User-namespace remapping (opt-in)

Docker can remap container UIDs/GIDs into an unprivileged range on the host.
Enable `userns-remap` in the daemon first (Docker's `/etc/docker/daemon.json`),
then set:

```bash
export DEVSANDBOX_SECURITY__USERNS_MODE=private
```

Without daemon-side `userns-remap`, Docker rejects the container request and the
attempt fails closed. Note that remapping changes the container's view of the
workspace owner; verify that the bind-mounted workspace is still writable by the
mapped uid before enabling it in production. Run
`DEVSANDBOX_E2E=1 pytest -q tests/security` on the deployment host after
enabling any of these controls.

## 5. Option C — dedicated host or VM

This is the simplest boundary and the baseline recommendation. Run the
orchestrator, daemon, PostgreSQL, and data root on a host that holds no
production credentials and no unrelated containers. Snapshot or recreate the
host after experiments. Back up `<data_root>` if you need the audit history.

## 6. Operational checklist

- [ ] `main` is never touched by DevSandbox; only
      `devsandbox/<project>/staging` is advanced.
- [ ] `security.allow_push` is `false` unless you have an explicit review
      process.
- [ ] The Docker endpoint is not reachable by agent/validator containers.
- [ ] `security.require_pinned_images=true` in production; agent, validator,
      and egress-proxy images are referenced by digest. See
      [images.md](images.md).
- [ ] If allowlist egress is enabled, `security.allowlist_hosts` contains only
      the required hosts and the proxy image is pinned. The proxy is an
      HTTP/HTTPS application proxy, not a per-packet firewall.
- [ ] The data root is on a dedicated filesystem with free-space monitoring
      (`limits.disk_min_free_mb`).
- [ ] PostgreSQL is not exposed beyond loopback or a private network.
- [ ] The API token is generated with `openssl rand -hex 32` and rotated
      deliberately.
- [ ] Backups cover the database and the per-project bare mirrors under
      `<data_root>/repos`.
- [ ] You have run `scripts/check-all.sh` and the drills in
      [drills.md](drills.md) on the deployment host.

## 7. Related documents

- [security-review.md](security-review.md) — threat model, findings, residual
  risks.
- [images.md](images.md) — digest pinning and image upgrades.
- [sandbox.md](sandbox.md) — container isolation and network policy.
- [drills.md](drills.md) — Docker/PostgreSQL/disk failure drills.
