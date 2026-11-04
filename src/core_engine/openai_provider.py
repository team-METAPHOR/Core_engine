"""OpenAI Responses API adapter. No SDK, tools, redirects, or implicit retries."""

import http.client
import json
import math
import os
import re
import ssl

from .model import Generation, ModelError, TokenUsage, strict_json


class OpenAIModel:
    def __init__(self, *, api_key: str, model: str, timeout_seconds: float = 30,
                 max_output_tokens: int = 3000):
        if not isinstance(api_key, str) or not re.fullmatch(r"[!-~]+", api_key):
            raise ValueError("a valid OPENAI_API_KEY is required")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", model):
            raise ValueError("a valid OPENAI_MODEL is required")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be in (0, 60]")
        if type(max_output_tokens) is not int or not 512 <= max_output_tokens <= 8192:
            raise ValueError("max_output_tokens must be between 512 and 8192")
        self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens

    @classmethod
    def from_env(cls):
        # Deliberately no default model and no automatic .env loading.
        return cls(api_key=os.environ.get("OPENAI_API_KEY", ""), model=os.environ.get("OPENAI_MODEL", ""))

    def generate(self, *, instructions: str, context: dict, schema: dict) -> Generation:
        body = json.dumps({
            "model": self.model,
            "instructions": instructions,
            "input": [{"role": "user", "content": [{
                "type": "input_text", "text": json.dumps(context, ensure_ascii=True),
            }]}],
            "text": {"format": {"type": "json_schema", "name": "bug_analysis_v1",
                                "strict": True, "schema": schema}},
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }, ensure_ascii=True).encode("utf-8")
        if len(body) > 128 * 1024:
            raise ValueError("model input exceeds 128 KiB")
        connection = http.client.HTTPSConnection(
            "api.openai.com", timeout=self.timeout_seconds, context=ssl.create_default_context(),
        )
        try:
            connection.request("POST", "/v1/responses", body=body, headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json",
            })
            response = connection.getresponse()
            if response.status != 200:
                code = {401: "MODEL_AUTH_FAILED", 403: "MODEL_AUTH_FAILED", 429: "MODEL_RATE_LIMITED"}.get(
                    response.status, "MODEL_HTTP_ERROR",
                )
                raise ModelError(code)
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ModelError("MODEL_RESPONSE_TOO_LARGE")
        except TimeoutError:
            raise ModelError("MODEL_TIMEOUT") from None
        except (OSError, http.client.HTTPException):
            raise ModelError("MODEL_UNAVAILABLE") from None
        finally:
            connection.close()
        try:
            return self._decode(strict_json(data.decode("utf-8")))
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise ModelError("MODEL_INVALID_RESPONSE") from None

    @staticmethod
    def _decode(response: dict) -> Generation:
        if response["status"] != "completed":
            raise ModelError("MODEL_INCOMPLETE")
        texts = []
        for item in response["output"]:
            if item["type"] == "reasoning":
                continue
            if item["type"] != "message" or item["role"] != "assistant":
                raise ValueError("unexpected output item")
            for content in item["content"]:
                if content["type"] == "refusal":
                    raise ModelError("MODEL_REFUSED")
                if content["type"] != "output_text" or not isinstance(content["text"], str):
                    raise ValueError("unexpected content")
                texts.append(content["text"])
        if not texts or not "".join(texts).strip():
            raise ValueError("missing output")
        model = response["model"]
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", model):
            raise ValueError("invalid model metadata")
        usage = response.get("usage")
        if usage is not None:
            values = [usage[key] for key in ("input_tokens", "output_tokens")]
            if any(type(value) is not int or value < 0 for value in values):
                raise ValueError("invalid usage")
            usage = TokenUsage(*values)
        return Generation("".join(texts), model, usage)
