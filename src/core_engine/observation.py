"""Sanitized browser snapshots and local evidence artifacts."""

from dataclasses import dataclass, field
import asyncio
from datetime import datetime, timezone
import re
from uuid import uuid4

from .artifacts import JobStore
from .images import Screenshot
from .login import LoginOptions
from .policy import require_https_for_credentials
from .redaction import redact_text


@dataclass(frozen=True)
class BrowserOptions:
    width: int = 1280
    height: int = 800
    app_version: str = "unspecified"
    test_context_id: str = "anonymous"
    step_timeout_ms: int = 3000
    mask_selectors: tuple[str, ...] = ()
    session_headers: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    browser_name: str = "chromium"
    record_video: bool = False
    video_fps: int = 4
    video_max_seconds: int = 60
    response_delay_ms: int = 0
    login: LoginOptions | None = field(default=None, repr=False, compare=False)
    navigation_timeout_ms: int = 15000

    def __post_init__(self):
        if self.login is not None and not isinstance(self.login, LoginOptions):
            raise ValueError("invalid trusted login options")
        if self.login and (self.session_headers or self.test_context_id == "anonymous"):
            raise ValueError("form login requires an explicit test context and cannot mix fixed headers")
        if self.browser_name not in {"chromium", "firefox", "webkit"}:
            raise ValueError("unsupported browser engine")
        if type(self.response_delay_ms) is not int or not 0 <= self.response_delay_ms <= 2000:
            raise ValueError("response delay must be 0..2000 ms")
        if type(self.record_video) is not bool or type(self.video_fps) is not int or not 1 <= self.video_fps <= 10 or type(self.video_max_seconds) is not int or not 1 <= self.video_max_seconds <= 120:
            raise ValueError("invalid video options")
        if type(self.width) is not int or type(self.height) is not int or not 320 <= self.width <= 1920 or not 320 <= self.height <= 1080:
            raise ValueError("viewport must be between 320x320 and 1920x1080")
        if not isinstance(self.app_version, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", self.app_version):
            raise ValueError("invalid app version")
        if not isinstance(self.test_context_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", self.test_context_id):
            raise ValueError("invalid test context ID")
        if type(self.step_timeout_ms) is not int or not 100 <= self.step_timeout_ms <= 5000:
            raise ValueError("step timeout must be 100..5000 ms")
        if type(self.navigation_timeout_ms) is not int or not 100 <= self.navigation_timeout_ms <= 30000:
            raise ValueError("navigation timeout must be 100..30000 ms")
        if not isinstance(self.mask_selectors, tuple) or len(self.mask_selectors) > 30 or any(
            not isinstance(value, str) or not 1 <= len(value) <= 256 for value in self.mask_selectors
        ):
            raise ValueError("invalid sensitive selectors")
        if not isinstance(self.session_headers, dict) or any(
            key not in {"Authorization", "Cookie"} or not isinstance(value, str) or not 1 <= len(value) <= 4096
            or any(ord(c) < 32 or ord(c) > 126 for c in value) for key, value in self.session_headers.items()
        ):
            raise ValueError("invalid trusted session headers")
        object.__setattr__(self, "session_headers", dict(self.session_headers))
        if self.session_headers and self.test_context_id == "anonymous":
            raise ValueError("trusted session headers require an explicit synthetic test context ID")

    def public(self):
        return {"width": self.width, "height": self.height, "app_version": self.app_version,
                "test_context_id": self.test_context_id, "session_mode": "form_login" if self.login else "fixed_headers" if self.session_headers else "anonymous",
                "browser_name": self.browser_name, "response_delay_ms": self.response_delay_ms,
                "navigation_timeout_ms": self.navigation_timeout_ms}

    def validate_scope(self, policy):
        if self.session_headers or self.login:
            require_https_for_credentials(policy.base_url)
        if self.login:
            self.login.validate_scope(policy)

    def redact(self, value):
        return self.login.redact(value) if self.login else redact_text(value)


# Snapshot extraction executes only this fixed engine script. Page text remains untrusted.
_ELEMENTS = """sensitive => {
    const excluded = sensitive.join(',');
    function safeText(el) {
        const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
        let node, text = '', visited = 0;
        while ((node = walker.nextNode()) && visited++ < 200 && text.length < 200) {
            if (node.parentElement && !node.parentElement.closest(excluded + ',script,style,input,textarea,select')) {
                text += node.textContent + ' ';
            }
        }
        return text.trim().slice(0,200);
    }
    function selector(el) {
        if (el.id) return '#' + CSS.escape(el.id);
        if (el.dataset.testid) return '[data-testid=' + JSON.stringify(el.dataset.testid) + ']';
        const parts = [];
        for (let node = el; node && node !== document.body && parts.length < 8; node = node.parentElement) {
            let segment = node.localName;
            if (!segment) break;
            const peers = Array.from(node.parentElement?.children || []).filter(x => x.localName === node.localName);
            if (peers.length > 1) segment += ':nth-of-type(' + (peers.indexOf(node) + 1) + ')';
            parts.unshift(segment);
        }
        return parts.join(' > ');
    }
    return Array.from(document.querySelectorAll('input,button,select,textarea,a,[role],[id],[data-testid],h1,h2,p,label,span,li'))
        .slice(0,600).filter(el => !el.closest(excluded) && el.getClientRects().length)
        .slice(0,80).map(el => {
            const r = el.getBoundingClientRect();
            const round = n => Math.round(n * 100) / 100;
            return {selector: selector(el), tag: el.localName,
            role: el.getAttribute('role') || '', name: (el.getAttribute('aria-label') || (el.labels?.[0] ? safeText(el.labels[0]) : '')).slice(0,120),
            text: /^(input|textarea|select)$/.test(el.localName) ? '' : safeText(el),
            enabled: !el.disabled, input_type: el.localName === 'input' ? el.type : '',
            bounds: {x:round(r.x), y:round(r.y), width:round(r.width), height:round(r.height)},
            client_size: {width:el.clientWidth, height:el.clientHeight},
            scroll_size: {width:el.scrollWidth, height:el.scrollHeight}};
        });
}"""


class Recorder:
    def __init__(self, store: JobStore, job_id: str, options: BrowserOptions, *, live=None):
        self.store, self.job_id, self.options = store, job_id, options
        self.live = live
        self.snapshots: list[dict] = []
        self.total_image_bytes = 0
        self.revision = -1
        self.plan_sha256 = None
        self.browser_version = None
        self.videos = []
        self.total_video_bytes = 0
        self._capture_lock = asyncio.Lock()
        self.active_video = None

    def open_video(self, session):
        from .video import VideoCapture
        capture = VideoCapture(self, session.capture_video_frame, attempt=session.attempt, origin=session.started)
        self.videos.append(capture.metadata)
        self.active_video = capture.start(sample=self.live is None)
        return self.active_video

    def progress(self, *, attempt, phase, step_index=None, action=None, total_steps=None):
        if self.live:
            self.live._progress(attempt=attempt, revision=self.revision, phase=phase,
                                step_index=step_index, action=action, total_steps=total_steps)

    async def masked_image(self, page):
        async with self._capture_lock:
            return await self._masked_image(page)

    async def _masked_image(self, page):
        sensitive = ["[data-private]", "[data-sensitive]", "input[type=password]", "input[type=hidden]", "iframe",
                     *self.options.mask_selectors]
        elements = await page.evaluate(_ELEMENTS, sensitive)
        masks = [page.locator("css=" + selector) for selector in sensitive]
        masks.append(page.locator("input,textarea,[contenteditable=true]"))
        if self.options.login:
            # Also mask credentials accidentally echoed by the authenticated application.
            masks.extend(page.get_by_text(secret, exact=False) for secret in self.options.login.secret_values())
        for item in elements:
            for key in ("name", "text", "selector"):
                cleaned = self.options.redact(item[key])
                if cleaned != item[key]:
                    masks.append(page.locator("css=" + item["selector"]))
                item[key] = cleaned
        elements = [item for item in elements if item["selector"] and len(item["selector"]) <= 256 and "REDACTED" not in item["selector"]]
        data = await page.screenshot(type="png", full_page=False, mask=masks, animations="disabled", timeout=3000)
        return Screenshot(data), elements

    async def capture(self, page, *, attempt, step, phase, network, errors, check=None):
        if len(self.snapshots) >= 256:
            raise ValueError("snapshot limit exceeded")
        image, elements = await self.masked_image(page)
        if check and check() is False:
            return None
        if self.active_video:
            self.active_video.add(image)
        if self.live:
            self.live._frame(image)
        self.total_image_bytes += len(image.data)
        if self.total_image_bytes > 64 * 1024 * 1024:
            raise ValueError("job screenshot budget exceeded")
        snapshot = {
            "id": uuid4().hex, "attempt": attempt, "step": step, "phase": phase,
            "revision": self.revision, "plan_sha256": self.plan_sha256,
            "created_at": datetime.now(timezone.utc).isoformat(), "url": self.options.redact(page.url),
            "elements": elements, "image": self.store.image(self.job_id, image),
            "network": list(network[-60:]), "errors": list(errors[-20:]),
        }
        self.snapshots.append(snapshot)
        self.store.write_json(self.job_id, "snapshots.json", self.snapshots)
        return snapshot

    def image(self, snapshot) -> Screenshot:
        return self.store.read_image(self.job_id, snapshot["image"])
