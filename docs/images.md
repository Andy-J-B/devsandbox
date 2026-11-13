# Image pinning and upgrades

DevSandbox launches three kinds of container:

| Image | Built from | Default reference |
| --- | --- | --- |
| Agent | `docker/agent-base/Dockerfile` | `devsandbox/agent:latest` |
| Validator | `docker/validator-base/Dockerfile` | `devsandbox/validator:latest` |
| Orchestrator | `docker/orchestrator/Dockerfile` | `devsandbox/orchestrator:local` |

Tags such as `:latest` are mutable. A registry owner can repoint them, and a
`docker pull` can silently replace the code that runs with Docker-socket access
or with access to project source. Production deployments should therefore use
content-addressed image references and set:

```bash
DEVSANDBOX_AGENT__IMAGE=registry.example/devsandbox/agent@sha256:<digest>
DEVSANDBOX_VALIDATION__IMAGE=registry.example/devsandbox/validator@sha256:<digest>
DEVSANDBOX_SECURITY__REQUIRE_PINNED_IMAGES=true
DEVSANDBOX_SECURITY__ALLOWED_IMAGES='["registry.example/devsandbox/agent@sha256:<digest>", "registry.example/devsandbox/validator@sha256:<digest>"]'
```

When `security.require_pinned_images` is true, any tag reference is rejected
**before** Docker is contacted: `DockerSandboxManager.run` raises
`PolicyViolation`, and validation records an `INFRA_ERROR` check instead of
running the container. The accepted forms are:

- `repo@sha256:<64 hex characters>` — a registry manifest digest (preferred);
- `sha256:<64 hex characters>` — a local image ID, useful for images built on
  the deployment host and never pushed.

## What is pinned in the repository

- Base images are pinned by digest:
  - `python:3.12-slim@sha256:5e2bdb1b...` in `docker/validator-base/Dockerfile`
    and `docker/orchestrator/Dockerfile`;
  - `node:22-bookworm-slim@sha256:e9f565d3...` in
    `docker/agent-base/Dockerfile`;
  - `postgres:16@sha256:f9a7b9eb...` in `docker/compose.yaml`.
- OpenCode is installed at an exact version (`OPENCODE_VERSION`, default
  `1.19.5`), and the build fails if the installed CLI reports a different
  version.
- Validator Python tools are installed at exact versions:
  `pytest==9.2.0`, `pytest-asyncio==1.4.1.post1`, `ruff==0.17.4`.

Application images (`devsandbox/agent`, `devsandbox/validator`) are still built
locally with tags for development. That is intentional: a locally built image
has no registry digest. Use the image ID form (`sha256:<id>`) if you want to
exercise `require_pinned_images=true` without a registry.

## Local development

Tags are fine:

```bash
docker/agent-base/build.sh
docker build -t devsandbox/validator:latest docker/validator-base
```

Keep `DEVSANDBOX_SECURITY__REQUIRE_PINNED_IMAGES=false` (the default). To use a
locally built image with pinning enabled:

```bash
AGENT_ID="$(docker image inspect --format '{{.Id}}' devsandbox/agent:latest)"
VALIDATOR_ID="$(docker image inspect --format '{{.Id}}' devsandbox/validator:latest)"
export DEVSANDBOX_AGENT__IMAGE="$AGENT_ID"
export DEVSANDBOX_VALIDATION__IMAGE="$VALIDATOR_ID"
export DEVSANDBOX_SECURITY__REQUIRE_PINNED_IMAGES=true
```

## Production deployment

1. Build the image, push it to your registry, and record the manifest digest
   from the push output or with:

   ```bash
   docker buildx imagetools inspect registry.example/devsandbox/agent:1.2.3
   # or, for a single-platform manifest:
   docker manifest inspect registry.example/devsandbox/agent:1.2.3 | jq -r '.config.digest'
   ```

2. Set `DEVSANDBOX_AGENT__IMAGE` and `DEVSANDBOX_VALIDATION__IMAGE` to the
   `repo@sha256:<digest>` form. Also set `security.allowed_images` to the same
   references if you use the allowlist.
3. Set `DEVSANDBOX_SECURITY__REQUIRE_PINNED_IMAGES=true`.
4. Start the orchestrator and confirm startup and a validation check succeed.
   A stale tag reference now fails closed instead of pulling whatever the tag
   points to.

## Upgrade process

Base images, OpenCode, and validator tools are upgraded deliberately, never by
floating tags:

1. Choose the new version and resolve its digest:

   ```bash
   docker buildx imagetools inspect node:22-bookworm-slim
   docker buildx imagetools inspect python:3.12-slim
   docker buildx imagetools inspect postgres:16
   ```

2. Update the `FROM ...@sha256:...` line (or the compose `image:`) and, for the
   validator, the exact pip versions. Keep the human-readable tag before the
   `@` so reviewers can see the intended release.
3. Rebuild and run the image smoke checks:

   ```bash
   docker/agent-base/build.sh
   docker build -t devsandbox/validator:latest docker/validator-base
   docker run --rm --network none devsandbox/validator:latest pytest --version
   ```

4. Run the fast gate and the container suites:

   ```bash
   scripts/check.sh
   DEVSANDBOX_E2E=1 python3 -m pytest -q tests/security tests/e2e
   ```

5. Record the old and new digest in the commit message and, for a production
   rollout, in the change ticket. Re-pin the application images in the
   deployment configuration to the newly built digests.

## Verification

- `tests/unit/test_config_policy.py::test_image_pinning_rejects_tags_when_required`
- `tests/unit/test_config_policy.py::test_image_pinning_is_optional_for_local_development`
- `tests/security/test_policy.py::test_sandbox_rejects_unpinned_image_before_docker`
- `tests/unit/test_validation_runner.py::test_unpinned_validation_image_is_rejected_before_docker`
