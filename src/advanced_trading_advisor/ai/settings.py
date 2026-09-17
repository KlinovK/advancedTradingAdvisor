import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from advanced_trading_advisor.configuration import local_environment


class AIConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OpenAISettings:
    enabled: bool = False
    api_key: str = field(default="", repr=False)
    model: str = "gpt-5.6-terra"
    reasoning_effort: str = "low"
    timeout_seconds: int = 45

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise AIConfigurationError("OPENAI_ENABLED must be true or false.")
        if not self.enabled:
            return
        if (
            not isinstance(self.api_key, str)
            or not self.api_key
            or not all(33 <= ord(c) <= 126 for c in self.api_key)
        ):
            raise AIConfigurationError(
                "AI explicitly enabled: configure OPENAI_API_KEY or use --no-ai."
            )
        if not isinstance(self.model, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,99}", self.model
        ):
            raise AIConfigurationError("Invalid OPENAI_MODEL.")
        if self.reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise AIConfigurationError("Invalid OPENAI_REASONING_EFFORT.")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 120:
            raise AIConfigurationError("OPENAI_TIMEOUT_SECONDS must be an integer from 1 to 120.")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None, *, enabled=None):
        env = environment if environment is not None else local_environment()
        if enabled is None:
            raw = env.get("OPENAI_ENABLED", "false").lower()
            if raw not in {"true", "false"}:
                raise AIConfigurationError("OPENAI_ENABLED must be true or false.")
            enabled = raw == "true"
        if not enabled:
            return cls()
        timeout = env.get("OPENAI_TIMEOUT_SECONDS", "45")
        if not timeout.isascii() or not timeout.isdigit() or len(timeout) > 3:
            raise AIConfigurationError("Invalid OPENAI_TIMEOUT_SECONDS.")
        return cls(
            True,
            env.get("OPENAI_API_KEY", ""),
            env.get("OPENAI_MODEL", "gpt-5.6-terra"),
            env.get("OPENAI_REASONING_EFFORT", "low"),
            int(timeout),
        )
