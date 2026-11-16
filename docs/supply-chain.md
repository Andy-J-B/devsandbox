# Supply chain: SBOMs, signing, and vulnerability scanning

DevSandbox ships three runtime images (`agent`, `validator`, `egress-proxy`)
and depends on a Python package graph. The `security` workflow
([`.github/workflows/security.yml`](../.github/workflows/security.yml)) checks
both.

## What runs in CI

| Job | Trigger | What it does |
| --- | --- | --- |
| `supply-chain (agent/validator/egress-proxy)` | push to `main`/tags, PR, weekly schedule, manual | builds each image, generates an SPDX SBOM with Syft, scans it with Grype (fails on `high` or `critical`), uploads SARIF to code scanning |
| `dependency-scan` | same | `pip-audit` over the project dependencies |
| `secret-scan` | same | Gitleaks over the working tree and Git history |
| `codeql` | same | CodeQL static analysis for Python |

The image matrix also pushes and signs on `push` (or a manual run with
`push_images=true`):

1. build and push `ghcr.io/<owner>/<repo>-<image>` by digest;
2. sign the digest keylessly with cosign through GitHub OIDC;
3. attach the SPDX SBOM as a cosign attestation;
4. verify the signature and identity.

Pull requests never push or sign, so fork PRs need no secrets.

## Local checks

```bash
# One-time tool installs (see the script header for links):
#   syft, grype, cosign, pip-audit, gitleaks

make supply-chain                 # build + SBOM + Grype scan
SUPPLY_CHAIN_IMAGES="validator" scripts/supply-chain.sh
GRYPE_FAIL_ON=critical scripts/supply-chain.sh
SUPPLY_CHAIN_SKIP_BUILD=1 scripts/supply-chain.sh
```

SBOMs and scan output are written to `dist/supply-chain/` (ignored by Git).

Signing locally requires a registry reference because cosign signs pushed
images, not local tags:

```bash
export COSIGN_IMAGE_REFS="ghcr.io/me/devsandbox-agent@sha256:<digest>"
scripts/supply-chain.sh
```

Use `COSIGN_KEY=/path/to/cosign.key` for a key-based signature; otherwise
cosign uses the ambient keyless/OIDC configuration.

## Verify a released image

```bash
cosign verify \
  --certificate-identity-regexp "https://github.com/<owner>/<repo>/" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/<owner>/<repo>-agent@sha256:<digest>

cosign verify-attestation \
  --type spdxjson \
  --certificate-identity-regexp "https://github.com/<owner>/<repo>/" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/<owner>/<repo>-agent@sha256:<digest>
```

Then pin the verified digest in `DEVSANDBOX_AGENT__IMAGE`,
`DEVSANDBOX_VALIDATION__IMAGE`, and
`DEVSANDBOX_SECURITY__ALLOWLIST_PROXY_IMAGE`; see [images.md](images.md).

## Policy choices

- **SBOM format**: SPDX JSON. It is human-readable, widely supported, and can be
  consumed by Grype, Trivy, Dependency-Track, and GitHub's dependency graph.
- **Fail threshold**: Grype fails the job at `high`. Lower it with
  `GRYPE_FAIL_ON` locally only for triage; CI should keep the stricter gate.
- **Base images**: the Dockerfiles already pin base images by digest. SBOMs
  still matter because they record the OS packages and language dependencies
  pulled in at build time.
- **Signing identity**: keyless signing binds the image to the GitHub Actions
  workflow identity (repository + OIDC issuer). Do not replace it with a
  long-lived private key unless your registry requires it.
- **Trivy alternative**: Trivy can consume the same SPDX SBOM with
  `trivy sbom dist/supply-chain/agent.spdx.json`. The CI uses Grype so the
  scan and the SBOM tool come from one vendor; either is acceptable if you
  standardize on one.

## Related documents

- [images.md](images.md) — digest pinning and the upgrade process.
- [deployment.md](deployment.md) — kernel and Docker-endpoint hardening.
- [security-review.md](security-review.md) — threat model and residual risks.
