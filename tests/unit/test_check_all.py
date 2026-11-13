"""Guardrails for the full check runner and Makefile wiring."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-all.sh"
MAKEFILE = ROOT / "Makefile"
SOAK_TARGET = "tests/e2e/test_e2e_crash_soak.py::test_soak_random_crash_points"


def _target(name: str) -> str:
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}:\n((?:\t.*\n)+)", text, flags=re.MULTILINE)
    assert match is not None, f"Makefile target {name!r} is missing"
    return match.group(1)


def test_check_all_script_is_executable() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


def test_makefile_runs_each_stage() -> None:
    assert "./scripts/check.sh" in _target("check")
    assert "integration and not docker" in _target("integration")
    assert "-m security" in _target("security")
    assert f"--deselect={SOAK_TARGET}" in _target("e2e")
    assert SOAK_TARGET in _target("soak")
    assert "./scripts/check-all.sh" in _target("check-all")
    assert "./scripts/drills.sh" in _target("drills")


def test_dry_run_lists_every_stage_and_the_soak_exclusion() -> None:
    result = subprocess.run(
        [str(SCRIPT)],
        cwd=ROOT,
        env={**os.environ, "CHECK_ALL_DRY_RUN": "1"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    for stage in ("check", "integration", "security", "e2e", "soak"):
        assert re.search(rf"^==> \[{stage}\] ", result.stdout, flags=re.MULTILINE)
        assert re.search(rf"^{stage}\s+PASS", result.stdout, flags=re.MULTILINE)
    assert f"--deselect={SOAK_TARGET}" in result.stdout
    assert "dry run" in result.stdout


def test_failed_stage_is_summarised_and_exits_nonzero(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts / "check-all.sh")
    (scripts / "check.sh").write_text("#!/usr/bin/env bash\nexit 7\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for executable in (scripts / "check-all.sh", scripts / "check.sh", docker):
        executable.chmod(0o755)

    result = subprocess.run(
        [str(scripts / "check-all.sh")],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CHECK_ALL_FAIL_FAST": "1",
            "DEVSANDBOX_VALIDATOR_IMAGE": "stub",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert re.search(r"^check\s+FAIL", result.stdout, flags=re.MULTILINE)
    assert "1 stage(s) failed: check" in result.stderr
