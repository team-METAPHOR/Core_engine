"""Provider-neutral structured generation contract and strict JSON decoding."""

from dataclasses import dataclass
import json
from typing import Protocol

from .images import Screenshot


class ModelError(Exception):
    """Only stable codes may cross the provider boundary; never response bodies."""

    CODES = frozenset({
        "MODEL_AUTH_FAILED", "MODEL_RATE_LIMITED", "MODEL_HTTP_ERROR",
        "MODEL_TIMEOUT", "MODEL_UNAVAILABLE", "MODEL_RESPONSE_TOO_LARGE",
        "MODEL_INVALID_RESPONSE", "MODEL_REFUSED", "MODEL_INCOMPLETE",
        "MODEL_INVALID_OUTPUT",
    })

    def __init__(self, code: str):
        self.code = code if code in self.CODES else "MODEL_UNAVAILABLE"
        super().__init__(self.code)


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
