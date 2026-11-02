"""Built-in ecosystem detector plugins."""

from __future__ import annotations

from .base import Detection, Detector, RepoFiles
from .generic import GenericManifestDetector
from .go import GoDetector
from .java import JavaDetector
from .makefile import MakefileDetector
from .node import NodeDetector
from .python import PythonDetector
from .rust import RustDetector

DEFAULT_DETECTORS: tuple[Detector, ...] = (
    PythonDetector(),
    NodeDetector(),
    GoDetector(),
    RustDetector(),
    JavaDetector(),
    GenericManifestDetector(),
    MakefileDetector(),
)

__all__ = [
    "DEFAULT_DETECTORS",
    "Detection",
    "Detector",
    "GenericManifestDetector",
    "GoDetector",
    "JavaDetector",
    "MakefileDetector",
    "NodeDetector",
    "PythonDetector",
    "RepoFiles",
    "RustDetector",
]
