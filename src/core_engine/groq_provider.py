"""Text-only Groq critic adapter; one request, no tools, redirects or retries."""

import http.client
import json
import math
import os
import re
import ssl

from .model import Generation, ModelError, TokenUsage, strict_json


_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}")


class GroqModel:
    def __init__(self, *, api_key: str, model: str, timeout_seconds: float = 30,
                 max_output_tokens: int = 4096):
        if not isinstance(api_key, str) or not re.fullmatch(r"[!-~]+", api_key):
            raise ValueError("a valid GROQ_API_KEY is required")
        if not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise ValueError("a valid GROQ_MODEL is required")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be in (0, 60]")
        if type(max_output_tokens) is not int or not 512 <= max_output_tokens <= 8192:
            raise ValueError("max_output_tokens must be between 512 and 8192")
        self._api_key, self.model = api_key, model
        self.timeout_seconds, self.max_output_tokens = timeout_seconds, max_output_tokens

    @classmethod
    def from_env(cls):
        return cls(api_key=os.environ.get("GROQ_API_KEY", ""), model=os.environ.get("GROQ_MODEL", ""))

    def generate(self, *, instructions: str, context: dict, schema: dict, images=()) -> Generation:
        if images != ():
            raise ValueError("the critic accepts text only")
        request = {
            "model": self.model,
            "messages": [{"role": "system", "content": instructions},
                         {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "qa_plan_review_v1", "strict": True, "schema": schema}},
            "max_completion_tokens": self.max_output_tokens,
        }
        if self.model in {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}:
            request.update(reasoning_effort="medium", include_reasoning=False)
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(body) > 128 * 1024:
            raise ValueError("model input exceeds 128 KiB")
        connection = http.client.HTTPSConnection("api.groq.com", timeout=self.timeout_seconds,
                                                context=ssl.create_default_context())
        try:
            connection.request("POST", "/openai/v1/chat/completions", body=body, headers={
                "Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise ModelError({401: "MODEL_AUTH_FAILED", 403: "MODEL_AUTH_FAILED",
                                  429: "MODEL_RATE_LIMITED"}.get(response.status, "MODEL_HTTP_ERROR"))
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
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
            raise ModelError("MODEL_INVALID_RESPONSE") from None

    @staticmethod
    def _decode(response):
        choices = response["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("expected one choice")
        choice = choices[0]
        if choice.get("finish_reason") == "content_filter":
            raise ModelError("MODEL_REFUSED")
        if choice.get("finish_reason") != "stop":
            raise ModelError("MODEL_INCOMPLETE")
        message = choice["message"]
        if message.get("refusal"):
            raise ModelError("MODEL_REFUSED")
        if message.get("role") != "assistant" or message.get("tool_calls") or message.get("function_call"):
            raise ValueError("unexpected message")
        content, model = message["content"], response["model"]
        if not isinstance(content, str) or not content.strip() or not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise ValueError("invalid content or model metadata")
        usage = response.get("usage")
        if usage is not None:
            values = [usage[key] for key in ("prompt_tokens", "completion_tokens")]
            if any(type(value) is not int or not 0 <= value <= 10_000_000 for value in values):
                raise ValueError("invalid usage")
            usage = TokenUsage(*values)
        return Generation(content, model, usage)
