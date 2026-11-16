# Supply chain: SBOMs, signing, and vulnerability scanning

DevSandbox ships four runtime images (`agent`, `validator`, `egress-proxy`,
`orchestrator`) and depends on a Python package graph. The `security` workflow
([`.github/workflows/security.yml`](../.github/workflows/security.yml)) checks
both.

## What runs in CI

| Job | Trigger | What it does |
| --- | --- | --- |
| `supply-chain (agent/validator/egress-proxy/orchestrator)` | push to `main`/tags, PR, weekly schedule, manual | builds each image, generates an SPDX SBOM with Syft, reports every high/critical finding to code scanning, and fails only on **critical findings that already have a fix** |
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

make supply-chain                 # build + SBOM + Grype gate (fixable critical)
SUPPLY_CHAIN_IMAGES="validator" scripts/supply-chain.sh
GRYPE_FAIL_ON=high scripts/supply-chain.sh         # stricter triage gate
GRYPE_ONLY_FIXED=0 scripts/supply-chain.sh         # include unfixed advisories
SUPPLY_CHAIN_SKIP_BUILD=1 scripts/supply-chain.sh
```

SBOMs and scan output are written to `dist/supply-chain/` (ignored by Git).

Dependency, secret, and static-analysis checks:

```bash
make audit                    # pip-audit + gitleaks (+ CodeQL if installed)
scripts/security-scans.sh --no-codeql
CODEQL_REQUIRE=1 scripts/security-scans.sh
```

`pip-audit` audits the project's declared dependencies (not just the current
virtualenv), so a clean local environment still catches vulnerable transitive
packages. `gitleaks` scans the working tree and Git history with
[`.gitleaks.toml`](../.gitleaks.toml); the only allowlisted findings are the
deliberate test canaries used by the redaction tests.

CodeQL runs in CI with
[`.github/codeql/codeql-config.yml`](../.github/codeql/codeql-config.yml).
Locally, the script uses `python-security-and-quality.qls` when the CodeQL CLI
is installed and writes SARIF to `dist/codeql-results.sarif`.


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
- **Report, then gate**: the SARIF upload always runs, so every high/critical
  finding appears in code scanning. A separate Grype pass then fails the job
  only for **critical findings with a fix available** (`only-fixed: true`,
  `severity-cutoff: critical`). This keeps the gate actionable: an advisory
  with no upstream patch is reported, while a fixable critical blocks the
  build (and therefore blocks image push and signing on `main`).
- **Why not fail on every high**: the runtime images inherit OS and npm
  packages from pinned base images. Failing on all highs would make the
  pipeline red whenever an upstream advisory lands, even when no fixed package
  exists. Raise the gate to `high` once the base images and bundled toolchains
  are routinely patched.
- **Base image hygiene**: each Dockerfile runs `apt-get upgrade -y` before
  installing packages, so Debian security updates published after the pinned
  base digest are applied. The validator copies Node/npm from the official Node
  image because Debian's `nodejs`/`npm` packages bundled an unpatched
  `handlebars` with fixable critical CVEs.
- **Fail threshold**: defaults to fixable `critical`. Lower it with
  `GRYPE_FAIL_ON` locally only for triage; the CI gate is defined by
  `GRYPE_GATE_SEVERITY` in the workflow.
- **Scanner version**: the workflow pins `GRYPE_VERSION` (`v0.121.3`). Do not
  rely on scan-action's bundled Grype (`v0.80.0`): it still requests the
  retired schema-v5 database, downloads a months-old snapshot, and then refuses
  to scan because the database exceeds the five-day age limit. The local script
  needs a current Grype for the same reason.
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
