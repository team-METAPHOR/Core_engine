"""Provider-neutral structured generation contract and strict JSON decoding."""

from dataclasses import dataclass
import json
import math
import re
from typing import Protocol

from .images import Screenshot


class ModelError(Exception):
    """Only stable codes may cross the provider boundary; never response bodies."""

    CODES = frozenset({
        "MODEL_AUTH_FAILED", "MODEL_RATE_LIMITED", "MODEL_REQUEST_TOO_LARGE", "MODEL_HTTP_ERROR",
        "MODEL_TIMEOUT", "MODEL_UNAVAILABLE", "MODEL_RESPONSE_TOO_LARGE",
        "MODEL_INVALID_RESPONSE", "MODEL_REFUSED", "MODEL_INCOMPLETE",
        "MODEL_INVALID_OUTPUT",
    })

    def __init__(self, code: str, *, retry_after_seconds: float | None = None):
        self.code = code if code in self.CODES else "MODEL_UNAVAILABLE"
        self.retry_after_seconds = retry_after_seconds if (
            self.code == "MODEL_RATE_LIMITED" and type(retry_after_seconds) in (int, float)
            and math.isfinite(retry_after_seconds) and 0 <= retry_after_seconds <= 86400
        ) else None
        super().__init__(self.code)


def retry_after_seconds(header):
    """Keep only bounded delta seconds; never expose arbitrary provider header text."""
    if not isinstance(header, str) or not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,6})?", header.strip()):
        return None
    value = float(header.strip())
    return value if value <= 86400 else None


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Generation:
    text: str
    model: str
    usage: TokenUsage | None = None


class JsonModel(Protocol):
    def generate(self, *, instructions: str, context: dict, schema: dict,
                 images: tuple[Screenshot, ...] = ()) -> Generation:
        """One bounded request. Raise ModelError on provider failures."""
        ...


def strict_json(text: str):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("non-finite JSON number")

    return json.loads(text, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
