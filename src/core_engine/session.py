"""Shared browser isolation, guarded routing and bounded observation hooks."""

import asyncio
from contextlib import asynccontextmanager
import os
import tempfile
import time
from urllib.parse import unquote, urlsplit

from .policy import PolicyError


@asynccontextmanager
async def browser_driver():
    from playwright.async_api import async_playwright
    manager = async_playwright()
    try:
        driver = await manager.start()
    except Exception:
        # Playwright can raise a plain Exception if its driver exits before startup.
        raise PolicyError("BROWSER_UNAVAILABLE") from None
    try:
        yield driver
    finally:
        await manager.__aexit__(None, None, None)


async def launch_browser(pw, options=None):
    from playwright.async_api import Error
    name = options.browser_name if options else "chromium"
    arguments = {"headless": True, "proxy": {"server": "http://127.0.0.1:1"}}
    app_data = None
    if name == "chromium":
        arguments["args"] = ["--proxy-bypass-list=<-loopback>", "--disable-background-networking",
              "--disable-dns-prefetch", "--disable-quic",
              "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    elif name == "firefox":
        # -profile alone still consults the user's Firefox app-data directory.
        # Isolate that bookkeeping as well (Playwright issue #42768).
        app_data = tempfile.TemporaryDirectory(prefix="core-qa-firefox-", ignore_cleanup_errors=True)
        arguments["env"] = {**os.environ, "MOZ_APP_DATA": app_data.name, "MOZ_LOCAL_APP_DATA": app_data.name}
        arguments["firefox_user_prefs"] = {"media.peerconnection.enabled": False,
            "network.dns.disablePrefetch": True, "network.prefetch-next": False}
    try:
        browser = await getattr(pw, name).launch(**arguments)
        if app_data:
            browser.on("disconnected", lambda *_: app_data.cleanup())
        return browser
    except Error:
        if app_data:
            app_data.cleanup()
        raise PolicyError("BROWSER_UNAVAILABLE") from None
    except BaseException:
        if app_data:
            app_data.cleanup()
        raise


class GuardedPage:
    def __init__(self, browser, transport, options, recorder=None, timeout_ms=3000, observed_responses=(), attempt=0):
        self.browser, self.transport, self.options, self.recorder = browser, transport, options, recorder
        self.timeout_ms = timeout_ms
        self.blocked, self.network, self.errors = [], [], []
        self.workers = set()
        self.context = None
        self.page = None
        self.started = time.monotonic()
        self.observed_responses = frozenset(observed_responses)
        self.video = None
        self.preview = None
        self.attempt = attempt
        self.login_in_progress = False
        self.login_post_used = False
        self.login_redirect_url = None

    def block(self, code):
        if not self.blocked:
            self.blocked.append(code)

    def check(self):
        self.transport.check()
        if self.blocked:
            raise PolicyError(self.blocked[0])

    async def __aenter__(self):
        self.context = await self.browser.new_context(
            service_workers="block", accept_downloads=False,
            viewport={"width": self.options.width, "height": self.options.height},
        )
        try:
            await self.context.add_init_script("""for (const key of ['RTCPeerConnection','webkitRTCPeerConnection','mozRTCPeerConnection','WebTransport']) {
                try { Object.defineProperty(globalThis, key, {value:undefined, configurable:false, writable:false}); } catch {}
            }""")
            await self.context.route("**/*", self.route)
            await self.context.route_web_socket("**/*", self.websocket)
            self.page = await self.context.new_page()
            self.page.set_default_timeout(self.timeout_ms)
            self.page.on("pageerror", self.page_error)
            self.page.on("console", self.console)
            self.page.on("download", lambda _: self.block("DOWNLOAD_UNSUPPORTED"))
            self.page.on("popup", lambda _: self.block("POPUP_UNSUPPORTED"))
            if self.options.login:
                from .login import perform_login
                await perform_login(self)
            if self.options.record_video and self.recorder:
                self.video = self.recorder.open_video(self)
            if self.recorder and self.recorder.live:
                from .live import PreviewCapture
                self.preview = PreviewCapture(self.recorder, self)
            return self
        except BaseException:
            await self.context.close()
            raise

    async def __aexit__(self, *_):
        try:
            try:
                if self.preview:
                    await self.preview.finish()
                if self.video:
                    await self.video.finish()
            finally:
                if self.context:
                    await self.context.close()
        finally:
            if self.recorder and self.recorder.active_video is self.video:
                self.recorder.active_video = None
            if self.workers:
                await asyncio.gather(*self.workers, return_exceptions=True)

    async def capture_video_frame(self):
        from playwright.async_api import Error

        if self.page is None or self.page.url == "about:blank" or self._login_screen():
            return None
        self.transport.check()
        self.transport.policy.check_url(self.page.url)
        try:
            image, _ = await self.recorder.masked_image(self.page)
        except Error as exc:
            if "Execution context was destroyed" in str(exc):
                # A navigation can replace the DOM between samples. Discard this
                # frame and resume with a freshly masked frame on the next tick.
                return None
            raise
        if not self._capture_allowed():
            return None
        return image

    def _capture_allowed(self):
        self.transport.check()
        self.transport.policy.check_url(self.page.url)
        return not self._login_screen()

    def _login_screen(self):
        if self.login_in_progress:
            return True
        login = self.options.login
        return bool(login and self.page and urlsplit(self.page.url).path in {
            urlsplit(login.login_path).path, urlsplit(login.submit_path or login.login_path).path})

    def page_error(self, error):
        if self.login_in_progress:
            self.block("LOGIN_FAILED")
            return
        self.block("PAGE_SCRIPT_ERROR")
        if len(self.errors) < 20:
            self.errors.append({"type": "pageerror", "message": self.options.redact(str(error))[:300]})

    def console(self, message):
        if self.login_in_progress:
            return
        if message.type == "error" and len(self.errors) < 20:
            self.errors.append({"type": "console", "message": self.options.redact(message.text)[:300]})

    async def websocket(self, ws):
        self.block("WEBSOCKET_UNSUPPORTED")
        await ws.close()

    async def route(self, route):
        from playwright.async_api import Error

        task = None
        item = {"url": self.options.redact(route.request.url)[:2048], "method": route.request.method, "status": None, "blocker": None}
        item["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        if not self.login_in_progress and len(self.network) < 120:
            self.network.append(item)
        try:
            self.check()
            arguments = {}
            if self.options.login:
                if self.options.login.password in unquote(route.request.url):
                    raise PolicyError("LOGIN_FAILED")
                raw = await route.request.all_headers()
                names = {"cookie": "Cookie", "authorization": "Authorization"}
                if self.login_in_progress and route.request.method == "POST":
                    if self.login_post_used:
                        raise PolicyError("LOGIN_FAILED")
                    self.login_post_used = True
                    names.update({"content-type": "Content-Type", "x-csrftoken": "X-CSRFToken",
                                  "x-csrf-token": "X-CSRF-Token", "origin": "Origin", "referer": "Referer"})
                    content_type = raw.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type not in {"application/x-www-form-urlencoded", "application/json"}:
                        raise PolicyError("LOGIN_FAILED")
                    arguments["login_body"] = route.request.post_data_buffer or b""
                arguments["browser_headers"] = {name: raw[key] for key, name in names.items() if key in raw}
            task = asyncio.create_task(asyncio.to_thread(self.transport.fetch, route.request.url, route.request.method, **arguments))
            self.workers.add(task)
            response = await asyncio.shield(task)
            if self.options.response_delay_ms:
                await asyncio.sleep(self.options.response_delay_ms / 1000)
            self.check()
            item["status"] = response.status
            if self.login_in_progress and route.request.method == "POST" and response.status in {301, 302, 303}:
                if not route.request.is_navigation_request():
                    raise PolicyError("LOGIN_FAILED")
                # Playwright does not intercept every hop of a native redirect chain.
                # Finish the form response, import its cookies, then navigate explicitly
                # through the guarded route rather than sending a browser-native hop.
                self.login_redirect_url = response.headers["location"]
                await route.fulfill(status=200, headers={k: v for k, v in response.headers.items() if k != "location"},
                                    body=b"<!doctype html>")
                return
            await route.fulfill(status=response.status, headers=response.headers, body=response.body)
            observed = (route.request.method, urlsplit(route.request.url).path, response.status) in self.observed_responses
            if response.status >= 400 and not observed:
                self.block("LOGIN_FAILED" if self.login_in_progress else "HTTP_ERROR")
        except PolicyError as exc:
            item["blocker"] = exc.code
            self.block(exc.code)
            await route.abort("blockedbyclient")
        except Error:
            item["blocker"] = "BROWSER_REQUEST_FAILED"
            self.block("BROWSER_REQUEST_FAILED")
        finally:
            if task is not None and task.done():
                self.workers.discard(task)

    async def navigate(self, url):
        self.check()
        self.transport.policy.check_url(url)
        timeout = self.options.login.timeout_ms if self.login_in_progress else self.timeout_ms
        response = await self.page.goto(url, wait_until="load", timeout=timeout)
        if response is None or response.status >= 400:
            raise PolicyError("NAVIGATION_FAILED")
        self.check()

    async def capture(self, attempt, step, phase):
        if self.recorder is None or self.page.url == "about:blank" or self._login_screen():
            return None
        self.transport.check()
        self.transport.policy.check_url(self.page.url)
        try:
            return await self.recorder.capture(self.page, attempt=attempt, step=step, phase=phase,
                                               network=self.network, errors=self.errors, check=self._capture_allowed)
        except (ValueError, OSError):
            raise PolicyError("EVIDENCE_CAPTURE_FAILED") from None
