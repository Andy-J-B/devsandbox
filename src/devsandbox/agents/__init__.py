"""Agent adapters: OpenCode, deterministic fake, registry, and model budget."""

from __future__ import annotations

from .fake import FakeScriptAdapter
from .interfaces import AgentAdapter, AgentRunRequest, AgentRunResult
from .models import ModelProvider, UsageLimiter, UsageSnapshot
from .opencode import OpenCodeAdapter
from .prompt import build_prompt
from .registry import AgentCapabilities, AgentRegistry, default_registry
from .secrets import SecretProvider

__all__ = [
    "AgentAdapter",
    "AgentCapabilities",
    "AgentRegistry",
    "AgentRunRequest",
    "AgentRunResult",
    "FakeScriptAdapter",
    "ModelProvider",
    "OpenCodeAdapter",
    "SecretProvider",
    "UsageLimiter",
    "UsageSnapshot",
    "build_prompt",
    "default_registry",
]
