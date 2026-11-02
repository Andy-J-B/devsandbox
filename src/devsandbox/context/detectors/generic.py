"""Generic Ruby/PHP manifest detector (manifest discovery, best effort)."""

from __future__ import annotations

import json

from ...common.types import CommandConfidence, DetectedCommand
from .base import Detection, RepoFiles


class GenericManifestDetector:
    """Recognise Gemfile/composer.json even when no dedicated detector exists."""

    name = "generic"

    def detect(self, files: RepoFiles) -> Detection:
        """Return Ruby/PHP manifest findings."""
        manifests: list[str] = []
        package_managers: list[str] = []
        frameworks: set[str] = set()
        test_commands: list[DetectedCommand] = []
        lint_commands: list[DetectedCommand] = []
        conventions: list[str] = []

        gemfile = files.read("Gemfile")
        if gemfile is not None or files.exists("Gemfile"):
            manifests.append("Gemfile")
            if files.exists("Gemfile.lock"):
                manifests.append("Gemfile.lock")
            package_managers.append("bundler")
            if "rspec" in (gemfile or "") or files.glob("spec/**/*_spec.rb"):
                test_commands.append(
                    DetectedCommand(
                        command="bundle exec rspec",
                        source="Gemfile/spec directory",
                        confidence=CommandConfidence.MEDIUM,
                    )
                )
            if files.exists(".rubocop.yml"):
                lint_commands.append(
                    DetectedCommand(
                        command="bundle exec rubocop",
                        source=".rubocop.yml config found",
                        confidence=CommandConfidence.HIGH,
                    )
                )
                conventions.append("rubocop")
            if "rails" in (gemfile or ""):
                frameworks.add("Ruby on Rails")

        composer_text = files.read("composer.json")
        if composer_text is not None or files.exists("composer.json"):
            manifests.append("composer.json")
            if files.exists("composer.lock"):
                manifests.append("composer.lock")
            package_managers.append("composer")
            try:
                data = json.loads(composer_text or "{}")
            except (json.JSONDecodeError, ValueError):
                data = {}
            if isinstance(data, dict):
                require = data.get("require")
                if isinstance(require, dict):
                    if "laravel/framework" in require:
                        frameworks.add("Laravel")
                    if "symfony/framework-bundle" in require:
                        frameworks.add("Symfony")
                scripts = data.get("scripts")
                if isinstance(scripts, dict) and "test" in scripts:
                    test_commands.append(
                        DetectedCommand(
                            command="composer test",
                            source="composer.json scripts.test",
                            confidence=CommandConfidence.HIGH,
                        )
                    )

        return Detection(
            ecosystem=self.name if manifests else "",
            package_managers=sorted(set(package_managers)),
            manifests=sorted(set(manifests)),
            frameworks=sorted(frameworks),
            test_commands=test_commands,
            lint_commands=lint_commands,
            conventions=sorted(set(conventions)),
            key_files=sorted(set(manifests)),
        )


__all__ = ["GenericManifestDetector"]
