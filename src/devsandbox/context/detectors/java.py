"""Java ecosystem detector (Maven and Gradle)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ...common.types import CommandConfidence, DetectedCommand
from .base import Detection, RepoFiles

_FRAMEWORKS: dict[str, str] = {
    "spring-boot": "Spring Boot",
    "spring-web": "Spring",
    "quarkus": "Quarkus",
    "micronaut": "Micronaut",
    "jakarta.servlet": "Jakarta EE",
    "junit": "JUnit",
}


def _junit_artifacts(text: str | None) -> list[str]:
    return [name for name in _FRAMEWORKS if text and name in text]


class JavaDetector:
    """Detect Maven/Gradle projects and their standard commands."""

    name = "java"

    def detect(self, files: RepoFiles) -> Detection:
        """Return Java findings for ``files``."""
        has_java = any(path.endswith(".java") for path in files.paths)
        poms = files.by_name("pom.xml")
        gradles = files.by_name("build.gradle", "build.gradle.kts")
        if not poms and not gradles and not has_java:
            return Detection(ecosystem=self.name)

        manifests: list[str] = []
        frameworks: set[str] = set()
        test_commands: list[DetectedCommand] = []
        build_commands: list[DetectedCommand] = []
        lint_commands: list[DetectedCommand] = []
        package_managers: list[str] = []

        if poms:
            manifests.extend(poms)
            package_managers.append("maven")
            full_text = "\n".join(files.read(path) or "" for path in poms)
            artifact_ids: list[str] = []
            for path in poms:
                text = files.read(path) or ""
                try:
                    root = ET.fromstring(text)
                except ET.ParseError:
                    continue
                artifact_ids.extend(
                    element.text or ""
                    for element in root.iter()
                    if element.tag.endswith("artifactId")
                )
            joined = " ".join(artifact_ids)
            for artifact in _junit_artifacts(joined):
                frameworks.add(_FRAMEWORKS[artifact])
            test_commands.append(
                DetectedCommand(
                    command="mvn test",
                    source=f"{poms[0]} found",
                    confidence=CommandConfidence.HIGH,
                )
            )
            build_commands.append(
                DetectedCommand(
                    command="mvn package",
                    source=f"{poms[0]} found",
                    confidence=CommandConfidence.HIGH,
                )
            )
            if "checkstyle" in full_text or "spotless" in full_text:
                lint_commands.append(
                    DetectedCommand(
                        command="mvn verify",
                        source="pom.xml lint plugin found",
                        confidence=CommandConfidence.MEDIUM,
                    )
                )

        if gradles:
            manifest = gradles[0]
            manifests.append(manifest)
            package_managers.append("gradle")
            text = "\n".join(files.read(path) or "" for path in gradles)
            if "org.springframework.boot" in text:
                frameworks.add(_FRAMEWORKS["spring-boot"])
            command = "./gradlew" if files.by_name("gradlew") else "gradle"
            test_commands.append(
                DetectedCommand(
                    command=f"{command} test",
                    source=f"{manifest} found",
                    confidence=CommandConfidence.HIGH,
                )
            )
            build_commands.append(
                DetectedCommand(
                    command=f"{command} build",
                    source=f"{manifest} found",
                    confidence=CommandConfidence.HIGH,
                )
            )

        test_layout: list[str] = []
        if any(path.startswith("src/test/java/") for path in files.paths):
            test_layout.append("src/test/java/")
        if any(path.startswith("src/test/kotlin/") for path in files.paths):
            test_layout.append("src/test/kotlin/")

        conventions: list[str] = []
        if files.by_name("checkstyle.xml"):
            conventions.append("checkstyle")
        if files.by_name(".editorconfig"):
            conventions.append("editorconfig")

        environment_hints: dict[str, str] = {}
        if package_managers:
            environment_hints["package_manager"] = package_managers[0]

        return Detection(
            ecosystem=self.name,
            package_managers=sorted(set(package_managers)),
            manifests=sorted(set(manifests)),
            frameworks=sorted(frameworks),
            build_commands=build_commands,
            test_commands=test_commands,
            lint_commands=lint_commands,
            test_layout=test_layout,
            conventions=conventions,
            environment_hints=environment_hints,
            key_files=sorted(set(manifests)),
        )


__all__ = ["JavaDetector"]
