"""Guardrails for the supply-chain and dependency-scanning workflow.

These checks keep the security jobs from silently losing a control (signing,
SBOM generation, vulnerability gating, dependency auditing, CodeQL, or secret
scanning) during future workflow edits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "security.yml"


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

    assert images == {"agent", "validator", "egress-proxy"}
    dockerfiles = {entry["dockerfile"] for entry in job["strategy"]["matrix"]["include"]}
    assert dockerfiles == {
        "docker/agent-base/Dockerfile",
        "docker/validator-base/Dockerfile",
        "docker/egress-proxy/Dockerfile",
    }


def test_supply_chain_generates_sboms_and_gates_on_high_severity() -> None:
    job = _job("supply-chain")
    uses = " ".join(_uses(job))

    assert "anchore/sbom-action@" in uses
    assert "anchore/scan-action@" in uses
    sbom = _step(job, "generate SBOM (syft)")
    assert sbom["with"]["format"] == "spdx-json"
    scan = _step(job, "vulnerability scan (grype)")
    assert scan["with"]["fail-build"] is True
    assert scan["with"]["severity-cutoff"] == "high"
    assert scan["with"]["output-format"] == "sarif"


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
