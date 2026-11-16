"""Guardrails for the supply-chain and dependency-scanning workflow.

These checks keep the security jobs from silently losing a control (signing,
SBOM generation, vulnerability gating, dependency auditing, CodeQL, or secret
scanning) during future workflow edits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "security.yml"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _job(name: str) -> dict[str, Any]:
    return _workflow()["jobs"][name]


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


def _uses(job: dict[str, Any]) -> list[str]:
    return [step.get("uses", "") for step in job["steps"]]


def test_supply_chain_builds_every_runtime_image() -> None:
    job = _job("supply-chain")
    images = {entry["name"] for entry in job["strategy"]["matrix"]["include"]}

    assert images == {"agent", "validator", "egress-proxy", "orchestrator"}
    dockerfiles = {entry["dockerfile"] for entry in job["strategy"]["matrix"]["include"]}
    assert dockerfiles == {
        "docker/agent-base/Dockerfile",
        "docker/validator-base/Dockerfile",
        "docker/egress-proxy/Dockerfile",
        "docker/orchestrator/Dockerfile",
    }


def test_supply_chain_generates_sboms_and_reports_high_severity() -> None:
    job = _job("supply-chain")
    uses = " ".join(_uses(job))

    assert "anchore/sbom-action@" in uses
    assert "anchore/scan-action@" in uses
    sbom = _step(job, "generate SBOM (syft)")
    assert sbom["with"]["format"] == "spdx-json"
    scan = _step(job, "vulnerability scan (grype)")
    # The reporting scan must not fail the build: scan-action only writes a
    # valid SARIF file after a successful run.
    assert scan["with"]["fail-build"] is False
    assert scan["with"]["severity-cutoff"] == "${{ env.GRYPE_REPORT_SEVERITY }}"
    assert scan["with"]["output-format"] == "sarif"
    assert "id" in scan
    upload = _step(job, "upload vulnerability results")
    assert upload["uses"] == "github/codeql-action/upload-sarif@v4"
    assert upload["with"]["sarif_file"] == "${{ steps.scan.outputs.sarif }}"


def test_supply_chain_gates_on_fixable_vulnerabilities_after_reporting() -> None:
    workflow = _workflow()
    job = _job("supply-chain")
    steps = job["steps"]
    names = [step.get("name") for step in steps]
    gate = _step(job, "gate on fixable vulnerabilities")

    assert workflow["env"]["GRYPE_REPORT_SEVERITY"] == "high"
    assert workflow["env"]["GRYPE_GATE_SEVERITY"] == "critical"
    assert gate["with"]["fail-build"] is True
    assert gate["with"]["only-fixed"] is True
    assert gate["with"]["severity-cutoff"] == "${{ env.GRYPE_GATE_SEVERITY }}"

    # The gate runs after the SARIF upload so findings are visible in code
    # scanning even when the gate fails.
    assert names.index("upload vulnerability results") < names.index(
        "gate on fixable vulnerabilities"
    )


def test_supply_chain_signs_and_attests_pushed_images() -> None:
    job = _job("supply-chain")
    assert job["permissions"]["id-token"] == "write"
    assert job["permissions"]["packages"] == "write"

    uses = " ".join(_uses(job))
    assert "sigstore/cosign-installer@" in uses

    sign = _step(job, "sign image and attest SBOM")
    assert sign["if"] == "env.PUSH_IMAGES == 'true'"
    assert "cosign sign --yes" in sign["run"]
    assert "cosign attest" in sign["run"]
    assert "--type spdxjson" in sign["run"]
    assert "cosign verify" in sign["run"]
    assert "--certificate-oidc-issuer" in sign["run"]


def test_dependency_scan_audits_the_local_project_strictly() -> None:
    job = _job("dependency-scan")
    runs = "\n".join(step.get("run", "") for step in job["steps"])

    assert "pip install --upgrade pip pip-audit" in runs
    assert "pip-audit ." in runs
    assert "--skip-editable" in runs
    assert "--strict" in runs


def test_secret_scan_covers_git_history_with_gitleaks() -> None:
    job = _job("secret-scan")
    checkout = next(
        step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
    )

    assert checkout["with"]["fetch-depth"] == 0
    assert any("gitleaks/gitleaks-action@" in use for use in _uses(job))
    config = Path(__file__).resolve().parents[2] / ".gitleaks.toml"
    assert "useDefault = true" in config.read_text(encoding="utf-8")


def test_codeql_analyzes_python_with_the_repository_config() -> None:
    job = _job("codeql")
    uses = " ".join(_uses(job))

    assert "github/codeql-action/init@" in uses
    assert "github/codeql-action/autobuild@" in uses
    assert "github/codeql-action/analyze@" in uses
    init = next(step for step in job["steps"] if step.get("uses") == "github/codeql-action/init@v4")
    assert init["with"]["languages"] == "python"
    assert init["with"]["config-file"] == "./.github/codeql/codeql-config.yml"
    assert job["permissions"]["security-events"] == "write"


def test_security_workflow_runs_on_pull_requests_and_a_schedule() -> None:
    workflow = _workflow()
    triggers = workflow.get("on") or workflow.get(True)

    assert "pull_request" in triggers
    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers


def test_runtime_images_apply_debian_security_updates() -> None:
    dockerfiles = [
        _ROOT / "docker" / "agent-base" / "Dockerfile",
        _ROOT / "docker" / "validator-base" / "Dockerfile",
        _ROOT / "docker" / "egress-proxy" / "Dockerfile",
        _ROOT / "docker" / "orchestrator" / "Dockerfile",
    ]

    for dockerfile in dockerfiles:
        assert "apt-get upgrade -y" in dockerfile.read_text(encoding="utf-8"), dockerfile


def test_validator_uses_upstream_node_instead_of_distro_packages() -> None:
    text = (_ROOT / "docker" / "validator-base" / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM node:22-trixie-slim@sha256:" in text
    assert "COPY --from=node /usr/local/lib/node_modules" in text
    # The Debian packages bundled an unpatched handlebars with critical CVEs.
    assert "\n        nodejs \\\n" not in text
    assert "\n        npm \\\n" not in text


def test_local_supply_chain_script_matches_the_ci_gate() -> None:
    text = (_ROOT / "scripts" / "supply-chain.sh").read_text(encoding="utf-8")

    assert 'FAIL_ON="${GRYPE_FAIL_ON:-critical}"' in text
    assert 'ONLY_FIXED="${GRYPE_ONLY_FIXED:-1}"' in text
    assert "grype_args+=(--only-fixed)" in text
