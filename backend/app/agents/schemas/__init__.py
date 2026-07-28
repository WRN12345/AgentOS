"""Agent 结构化输出 Schema（10.2 节，T5.3）。"""

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
