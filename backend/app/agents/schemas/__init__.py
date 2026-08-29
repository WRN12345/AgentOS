"""Agent 结构化输出 Schema。"""

from app.agents.schemas.suggestion import (
    AgentSuggestionEnvelope,
    AgentSuggestionOutput,
    SuggestionContent,
    SuggestionValidationError,
    parse_suggestion_output,
)

__all__ = [
    "AgentSuggestionEnvelope",
    "AgentSuggestionOutput",
    "SuggestionContent",
    "SuggestionValidationError",
    "parse_suggestion_output",
]
