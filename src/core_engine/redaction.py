"""Best-effort text redaction, not a substitute for upstream data minimization."""

import re
from urllib.parse import urlsplit, urlunsplit


_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_HEADERS = re.compile(r"^(\s*(?:authorization|cookie|set-cookie)\s*:)\s*[^\r\n]*", re.IGNORECASE | re.MULTILINE)
_AUTH = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_KEY = re.compile(r"\b(?:sk-|gsk_)[A-Za-z0-9_-]{8,}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_SECRET = re.compile(
    r"(\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"authorization|cookie|set-cookie)\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n,;}]+)", re.IGNORECASE,
)
_EMAIL = re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def redact_text(text: str) -> str:
    def safe_url(match):
        try:
            url = urlsplit(match.group())
            # Remove userinfo and all query/fragment values, regardless of key name.
            host = url.netloc.rsplit("@", 1)[-1]
            return urlunsplit((url.scheme, host, url.path, "", ""))
        except ValueError:
            return "[REDACTED_URL]"

    text = _HEADERS.sub(lambda match: match[1] + " [REDACTED]", text)
    text = _URL.sub(safe_url, text)
    text = _AUTH.sub("[REDACTED_AUTH]", text)
    text = _KEY.sub("[REDACTED_KEY]", text)
    text = _JWT.sub("[REDACTED_TOKEN]", text)
    text = _SECRET.sub(lambda match: match[1] + "[REDACTED]", text)
    return _EMAIL.sub("[REDACTED_EMAIL]", text)
