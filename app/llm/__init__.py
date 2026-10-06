"""LLM adapters. Proposals only — never authority (ADR-0002)."""

from app.llm.prompts import PromptNotFound, available_prompts, load_prompt, prompt_version
from app.llm.provider import (
    DeterministicReasoner,
    OpenAICompatibleReasoner,
    Reasoner,
    ReasoningRequest,
    build_reasoner,
)

__all__ = [
    "DeterministicReasoner",
    "OpenAICompatibleReasoner",
    "PromptNotFound",
    "Reasoner",
    "ReasoningRequest",
    "available_prompts",
    "build_reasoner",
    "load_prompt",
    "prompt_version",
]
