"""Export options from the modal window, parsed once at startup."""

import os
from dataclasses import dataclass
from typing import Mapping

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


@dataclass(frozen=True)
class Settings:
    # True: captures linked from cloud storage are listed in links.json instead of copied
    links_only: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        return cls(links_only=_parse_bool(env.get("modal.state.linksOnly", "false"), "linksOnly"))


def _parse_bool(value: str, name: str) -> bool:
    lowered = str(value).strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"modal.state.{name} must be true or false, got {value!r}")
