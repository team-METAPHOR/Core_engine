"""Trusted deterministic form login; credentials never belong to an AI plan."""

from dataclasses import dataclass, field
import hashlib
import html
import json
import re
from urllib.parse import quote, quote_plus, urlsplit

from .policy import PolicyError, canonical_url, require_https_for_credentials
from .redaction import redact_text


@dataclass(frozen=True)
class LoginOptions:
    login_path: str
    login_id: str = field(repr=False, compare=False)
    password: str = field(repr=False, compare=False)
    success_path: str | None = None
    success_selector: str | None = None
    username_selector: str = 'input[autocomplete="username"],input[type="email"],input[name="username"]'
    password_selector: str = 'input[type="password"]'
    submit_selector: str = 'button[type="submit"],button:not([type]),input[type="submit"]'
    submit_path: str | None = None
    timeout_ms: int = 10000

    def __post_init__(self):
        for value, maximum in ((self.login_id, 320), (self.password, 1024)):
            if not isinstance(value, str) or not 1 <= len(value) <= maximum or '\x00' in value:
                raise ValueError('invalid trusted login credentials')
            value.encode('utf-8')
        if type(self.timeout_ms) is not int or not 100 <= self.timeout_ms <= 15000:
            raise ValueError('login timeout must be 100..15000 ms')
        if not self.success_path and not self.success_selector:
            raise ValueError('an explicit login success path or selector is required')
        for name in ('login_path', 'submit_path', 'success_path'):
            path = getattr(self, name)
            if path is None:
                continue
            if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or '?' in path:
                raise ValueError('login paths must be same-origin paths without query values')
            object.__setattr__(self, name, urlsplit(canonical_url('https://scope.invalid' + path)).path)
        if self.success_path == self.login_path:
            raise ValueError('login success path must differ from login path')
        for selector in (self.username_selector, self.password_selector, self.submit_selector, self.success_selector):
            if selector is not None and (not isinstance(selector, str) or not 1 <= len(selector) <= 256):
                raise ValueError('invalid trusted login selector')
        config = json.dumps(self.configuration(), ensure_ascii=False)
        if any(len(secret) >= 4 and secret in config for secret in (self.login_id, self.password)):
            raise ValueError('login configuration cannot embed credential values')

    def configuration(self):
        """Host configuration only; callers must not send this to a model."""
        return {name: getattr(self, name) for name in (
            'login_path', 'success_path', 'success_selector', 'username_selector',
            'password_selector', 'submit_selector', 'submit_path', 'timeout_ms')}

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.configuration(), sort_keys=True).encode()).hexdigest()

    def validate_scope(self, policy):
        require_https_for_credentials(policy.base_url)
        for path in (self.login_path, self.submit_path, self.success_path):
            if path is not None:
                policy.check_url(policy.base_url.rstrip('/') + path)

    def secret_values(self):
        return tuple(sorted({value for secret in (self.login_id, self.password) for value in
                             (secret, quote(secret, safe=''), quote_plus(secret), html.escape(secret))}, key=len, reverse=True))

    def redact(self, value):
        for secret in self.secret_values():
            if len(secret) < 4:
                value = re.sub(r'(?<!\w)' + re.escape(secret) + r'(?!\w)', '[REDACTED]', value)
            else:
                value = value.replace(secret, '[REDACTED]')
        return redact_text(value)


async def perform_login(session):
    """Runs once per fresh context; no model, screenshot, video or login logs."""
    from playwright.async_api import Error

    login = session.options.login
    login.validate_scope(session.transport.policy)
    origin = session.transport.policy.base_url.rstrip('/')
    session.login_in_progress = True
    session.login_post_used = False
    try:
        session.page.set_default_timeout(login.timeout_ms)
        await session.navigate(origin + login.login_path)
        username = session.page.locator('css=' + login.username_selector)
        password = session.page.locator('css=' + login.password_selector)
        submit = session.page.locator('css=' + login.submit_selector)
        for target in (username, password, submit):
            if await target.count() != 1 or not await target.is_visible() or not await target.is_enabled():
                raise PolicyError('LOGIN_FAILED')
        # Credentials are filled only into text/email and password controls selected by the host.
        if await username.get_attribute('type') not in (None, 'text', 'email'):
            raise PolicyError('LOGIN_FAILED')
        if await password.get_attribute('type') != 'password':
            raise PolicyError('LOGIN_FAILED')
        # The success marker cannot already be present on the login page.
        if login.success_selector and await session.page.locator('css=' + login.success_selector).is_visible():
            raise PolicyError('LOGIN_FAILED')
        await username.fill(login.login_id)
        await password.fill(login.password)
        session.check()
        await submit.click()
        if session.login_redirect_url:
            await session.navigate(session.login_redirect_url)
        if login.success_path:
            expected = origin + login.success_path
            await session.page.wait_for_url(lambda url: str(url).split('?', 1)[0] == expected,
                                            wait_until='load', timeout=login.timeout_ms)
        if login.success_selector:
            await session.page.locator('css=' + login.success_selector).wait_for(state='visible', timeout=login.timeout_ms)
        session.check()
        session.transport.policy.check_url(session.page.url)
        if not session.login_post_used:
            raise PolicyError('LOGIN_FAILED')
    except (Error, PolicyError) as exc:
        if isinstance(exc, PolicyError) and exc.code in {
            'CANCELLED', 'AUTHORIZATION_REVOKED', 'AUTHORIZATION_UNAVAILABLE',
            'RUNTIME_LIMIT', 'REQUEST_LIMIT', 'RATE_LIMIT', 'CONCURRENCY_LIMIT',
            'RESPONSE_LIMIT', 'DOWNLOAD_LIMIT', 'CREDENTIALS_REQUIRE_HTTPS'}:
            raise
        raise PolicyError('LOGIN_FAILED') from None
    finally:
        session.login_in_progress = False
        session.page.set_default_timeout(session.timeout_ms)
        # Authentication traffic/console output is deliberately not an evidence source.
        session.network.clear()
        session.errors.clear()
