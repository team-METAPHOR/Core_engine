"""Shared browser isolation, guarded routing and bounded observation hooks."""

import asyncio

from .policy import PolicyError
from .redaction import redact_text


async def launch_browser(pw):
    return await pw.chromium.launch(
        headless=True, proxy={"server": "http://127.0.0.1:1"},
        args=["--proxy-bypass-list=<-loopback>", "--disable-background-networking",
              "--disable-dns-prefetch", "--disable-quic",
              "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
    )


class GuardedPage:
    def __init__(self, browser, transport, options, recorder=None, timeout_ms=3000):
        self.browser, self.transport, self.options, self.recorder = browser, transport, options, recorder
        self.timeout_ms = timeout_ms
        self.blocked, self.network, self.errors = [], [], []
        self.workers = set()
        self.context = None
        self.page = None

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
            await self.context.route("**/*", self.route)
            await self.context.route_web_socket("**/*", self.websocket)
            self.page = await self.context.new_page()
            self.page.set_default_timeout(self.timeout_ms)
            self.page.on("pageerror", self.page_error)
            self.page.on("console", self.console)
            self.page.on("download", lambda _: self.block("DOWNLOAD_UNSUPPORTED"))
            self.page.on("popup", lambda _: self.block("POPUP_UNSUPPORTED"))
            return self
        except BaseException:
            await self.context.close()
            raise

    async def __aexit__(self, *_):
        try:
            if self.context:
                await self.context.close()
        finally:
            if self.workers:
                await asyncio.gather(*self.workers, return_exceptions=True)

    def page_error(self, error):
        self.block("PAGE_SCRIPT_ERROR")
        if len(self.errors) < 20:
            self.errors.append({"type": "pageerror", "message": redact_text(str(error))[:300]})

    def console(self, message):
        if message.type == "error" and len(self.errors) < 20:
            self.errors.append({"type": "console", "message": redact_text(message.text)[:300]})

    async def websocket(self, ws):
        self.block("WEBSOCKET_UNSUPPORTED")
        await ws.close()

    async def route(self, route):
        from playwright.async_api import Error

        task = None
        item = {"url": redact_text(route.request.url)[:2048], "method": route.request.method, "status": None, "blocker": None}
        try:
            self.check()
            task = asyncio.create_task(asyncio.to_thread(self.transport.fetch, route.request.url, route.request.method))
            self.workers.add(task)
            response = await asyncio.shield(task)
            self.check()
            item["status"] = response.status
            await route.fulfill(status=response.status, headers=response.headers, body=response.body)
            if response.status >= 400:
                self.block("HTTP_ERROR")
        except PolicyError as exc:
            item["blocker"] = exc.code
            self.block(exc.code)
            await route.abort("blockedbyclient")
        except Error:
            item["blocker"] = "BROWSER_REQUEST_FAILED"
            self.block("BROWSER_REQUEST_FAILED")
        finally:
            if len(self.network) < 120:
                self.network.append(item)
            if task is not None and task.done():
                self.workers.discard(task)

    async def navigate(self, url):
        self.check()
        self.transport.policy.check_url(url)
        response = await self.page.goto(url, wait_until="load", timeout=self.timeout_ms)
        if response is None or response.status >= 400:
            raise PolicyError("NAVIGATION_FAILED")
        self.check()

    async def capture(self, attempt, step, phase):
        if self.recorder is None or self.page.url == "about:blank":
            return None
        self.transport.check()
        self.transport.policy.check_url(self.page.url)
        try:
            return await self.recorder.capture(self.page, attempt=attempt, step=step, phase=phase,
                                               network=self.network, errors=self.errors)
        except (ValueError, OSError):
            raise PolicyError("EVIDENCE_CAPTURE_FAILED") from None
