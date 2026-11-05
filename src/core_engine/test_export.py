"""Compile validated plans to fixed-behavior Playwright tests; never generate code with an LLM."""

import json
from dataclasses import asdict
from urllib.parse import urlsplit

from .checks import Check
from .observation import BrowserOptions
from .plan import BrowserPlan, TextCheck
from .redaction import redact_text


def required_test_environment(plan, options):
    names = ["QA_RESET_MODULE"]
    names += ["QA_ENTRY_URL" if index == 0 else "QA_NAVIGATE_" + str(index)
              for index, step in enumerate(plan.steps) if step.action == "navigate" and urlsplit(step.target).query]
    if options.session_headers:
        names.append("QA_AUTHORIZATION or QA_COOKIE or QA_STORAGE_STATE")
    if options.login:
        names.extend(["QA_LOGIN_ID", "QA_LOGIN_PASSWORD"])
    return names


def export_playwright_test(plan: BrowserPlan, *, policy, options=None, test_name="신고된 버그의 수정 확인") -> str:
    options = options or BrowserOptions()
    if not isinstance(plan, BrowserPlan) or plan.expected is None:
        raise ValueError("a validated plan with both outcomes is required")
    if not isinstance(test_name, str) or not 1 <= len(test_name) <= 200:
        raise ValueError("invalid test name")
    plan.validate_scope(policy)
    options.validate_scope(policy)
    navigation_env, steps = {}, []
    for index, step in enumerate(plan.steps):
        if step.action in {"fill", "assert_text"} and options.redact(step.value) != step.value:
            raise ValueError("sensitive action value cannot be exported")
        if step.action == "navigate" and urlsplit(step.target).query:
            navigation_env[str(index)] = "QA_ENTRY_URL" if index == 0 else "QA_NAVIGATE_" + str(index)
        steps.append({"action": step.action, "target": options.redact(step.target), "value": step.value or ""})

    def encode(check):
        if isinstance(check, TextCheck):
            check = Check("text", check.selector, check.text)
        data = asdict(check)
        if any(options.redact(value) != value for value in data.values()):
            raise ValueError("sensitive predicate cannot be exported")
        return data

    data = {"steps": steps, "ready": encode(plan.ready), "expected": encode(plan.expected), "symptom": encode(plan.symptom),
        "timeout": plan.step_timeout_ms, "browser": options.browser_name, "responseDelayMs": options.response_delay_ms,
        "viewport": {"width": options.width, "height": options.height}, "sessionRequired": bool(options.session_headers),
        "allowedPaths": list(policy.allowed_path_prefixes), "excludedPaths": list(policy.excluded_path_prefixes),
        "methods": list(policy.allowed_methods), "navigationEnv": navigation_env,
        "login": options.login.configuration() if options.login else None}
    return ("// Generated from a validated scenario. Assert NORMAL behavior: buggy fails, fixed passes.\n"
            "// Run only in your approved test environment; configure a trusted reset hook.\n"
            "// This standalone browser test does not implement the engine's DNS/IP-pinning transport.\n"
            "import { test, expect, type Page } from '@playwright/test';\n"
            "import { pathToFileURL } from 'node:url';\nimport { resolve } from 'node:path';\n"
            "const scenario = " + json.dumps(data, ensure_ascii=True, indent=2) + ";\n"
            "const testName = " + json.dumps(options.redact(test_name), ensure_ascii=True) + ";\n" + _RUNTIME)


_RUNTIME = r'''
// Authentication must never be captured in Playwright's unmasked traces or recordings.
test.use({ browserName: scenario.browser as 'chromium' | 'firefox' | 'webkit', trace: 'off', video: 'off', screenshot: 'off' });

async function observe(page: Page, check: any, responses: any[]): Promise<boolean> {
  if (check.kind === 'response_status') {
    const found = responses.filter(r => r.method === check.other_selector && r.path === check.selector);
    if (!found.length) throw new Error('RESPONSE_NOT_OBSERVED');
    return found[found.length - 1].status === Number(check.value);
  }
  const element = page.locator('css=' + check.selector);
  const count = await element.count();
  if (check.kind === 'count') return count === Number(check.value);
  if (check.kind === 'visible') {
    if (count > 1) throw new Error('AMBIGUOUS_CHECK_TARGET');
    return (count === 1 && await element.isVisible()) === (check.value === 'true');
  }
  if (count !== 1 || !await element.isVisible()) throw new Error('CHECK_TARGET_UNAVAILABLE');
  if (check.kind === 'text') return (await element.innerText()).trim() === check.value.trim();
  let actual: boolean;
  if (check.kind === 'enabled') actual = await element.isEnabled();
  else if (check.kind === 'overlap') {
    const other = page.locator('css=' + check.other_selector);
    if (await other.count() !== 1 || !await other.isVisible()) throw new Error('CHECK_TARGET_UNAVAILABLE');
    const a = await element.boundingBox(), b = await other.boundingBox();
    if (!a || !b) throw new Error('CHECK_TARGET_UNAVAILABLE');
    actual = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x) > 1
      && Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y) > 1;
  } else if (check.kind === 'clipped') {
    actual = await element.evaluate(el => {
      const r = el.getBoundingClientRect();
      return r.left < -1 || r.top < -1 || r.right > innerWidth + 1 || r.bottom > innerHeight + 1
        || el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
    });
  } else throw new Error('UNSUPPORTED_CHECK');
  return actual === (check.value === 'true');
}

test(testName, async ({ browser }) => {
  test.setTimeout(180_000);
  const resetPath = process.env.QA_RESET_MODULE;
  if (!resetPath) throw new Error('Set QA_RESET_MODULE to a trusted module exporting async reset(attempt): true');
  const resetModule = await import(pathToFileURL(resolve(resetPath)).href);
  const reset = resetModule.reset || resetModule.default;
  if (typeof reset !== 'function') throw new Error('RESET_HOOK_REQUIRED');
  const entry = new URL(process.env.QA_ENTRY_URL || scenario.steps[0].target);
  if (!['http:', 'https:'].includes(entry.protocol) || entry.username || entry.password) throw new Error('INVALID_ENTRY_URL');
  if ((scenario.sessionRequired || scenario.login) && entry.protocol !== 'https:') throw new Error('CREDENTIALS_REQUIRE_HTTPS');
  const within = (path: string, prefix: string) => prefix === '/' || path === prefix || path.startsWith(prefix + '/');
  const allowed = (value: string, method: string) => {
    const url = new URL(value);
    let path: string;
    try { path = decodeURIComponent(url.pathname); } catch { return false; }
    return url.origin === entry.origin && scenario.methods.includes(method) && !url.username && !url.password
      && !path.includes('\\') && !path.includes('//') && !path.split('/').some(x => x === '.' || x === '..')
      && !/%/.test(path) && !/(?:^|\/)(?:logout|signout|delete|remove|purchase|checkout|unsubscribe)(?:\/|$)/i.test(path)
      && ![...url.searchParams.keys()].some(k => /token|secret|pass|auth|session|cookie|action|delete|redirect|callback|url/i.test(k))
      && scenario.allowedPaths.some(prefix => within(path, prefix))
      && !scenario.excludedPaths.some(prefix => within(path, prefix));
  };
  const declaredErrors = [scenario.expected, scenario.symptom].filter(c => c.kind === 'response_status');
  const outcomes: any[] = [];
  for (const attempt of [1, 2]) {
    if (await reset(attempt) !== true) throw new Error('RESET_FAILED');
    const headers: Record<string, string> = {};
    if (scenario.sessionRequired) {
      if (process.env.QA_AUTHORIZATION) headers.Authorization = process.env.QA_AUTHORIZATION;
      if (process.env.QA_COOKIE) headers.Cookie = process.env.QA_COOKIE;
      if (!Object.keys(headers).length && !process.env.QA_STORAGE_STATE) throw new Error('TEST_SESSION_REQUIRED');
    }
    const context = await browser.newContext({ viewport: scenario.viewport, serviceWorkers: 'block', acceptDownloads: false,
      extraHTTPHeaders: headers, ...(scenario.sessionRequired && process.env.QA_STORAGE_STATE
        ? { storageState: process.env.QA_STORAGE_STATE } : {}) });
    const responses: any[] = [];
    let loggingIn = false, loginPostUsed = false, loginRedirect: string | null = null;
    let blocker = '';
    const guard = () => { if (blocker) throw new Error(blocker); };
    try {
      await context.route('**/*', async route => {
        const request = route.request(), auth = scenario.login;
        const loginPost = loggingIn && auth && request.method() === 'POST'
          && request.url() === new URL(auth.submit_path || auth.login_path, entry.origin).href;
        if (loginPost && loginPostUsed) { blocker = 'LOGIN_FAILED'; await route.abort(); return; }
        if (loggingIn && process.env.QA_LOGIN_PASSWORD && decodeURIComponent(request.url()).includes(process.env.QA_LOGIN_PASSWORD)) {
          blocker = 'LOGIN_FAILED'; await route.abort(); return;
        }
        if (!allowed(request.url(), loginPost ? 'GET' : request.method())) {
          blocker = 'REQUEST_OUT_OF_SCOPE'; await route.abort(); return;
        }
        if (loginPost) {
          loginPostUsed = true;
          const contentType = (request.headers()['content-type'] || '').split(';')[0].trim().toLowerCase();
          if (!['application/x-www-form-urlencoded', 'application/json'].includes(contentType)
              || !request.postDataBuffer()?.length || request.postDataBuffer()!.length > 16384) {
            blocker = 'LOGIN_FAILED'; await route.abort(); return;
          }
          const response = await route.fetch({ maxRedirects: 0 });
          if ([301, 302, 303].includes(response.status())) {
            const location = response.headers()['location'];
            if (!location || !request.isNavigationRequest()) { blocker = 'LOGIN_FAILED'; await route.abort(); return; }
            const target = new URL(location, request.url()).href;
            if (!allowed(target, 'GET')) { blocker = 'LOGIN_FAILED'; await route.abort(); return; }
            loginRedirect = target;
            const responseHeaders = { ...response.headers() };
            delete responseHeaders.location;
            delete responseHeaders['content-length'];
            await route.fulfill({ status: 200, headers: responseHeaders, body: '<!doctype html>' });
          } else if ([307, 308].includes(response.status())) {
            blocker = 'LOGIN_FAILED'; await route.abort();
          } else await route.fulfill({ response });
          return;
        }
        if (scenario.responseDelayMs || scenario.sessionRequired || scenario.login) {
          const response = await route.fetch({ maxRedirects: 0 });
          if ((scenario.sessionRequired || scenario.login) && [301, 302, 303, 307, 308].includes(response.status())) {
            // Native redirect chains can bypass routing after their first hop.
            // Only the explicit login POST redirect above is supported with credentials.
            const location = response.headers()['location'];
            blocker = location && new URL(location, request.url()).protocol !== 'https:'
              ? 'CREDENTIALS_REQUIRE_HTTPS' : 'AUTHENTICATED_REDIRECT_UNSUPPORTED';
            await route.abort(); return;
          }
          await new Promise(resolve => setTimeout(resolve, scenario.responseDelayMs));
          await route.fulfill({ response });
        } else await route.continue();
      });
      await context.routeWebSocket('**/*', socket => { blocker = 'WEBSOCKET_UNSUPPORTED'; socket.close(); });
      await context.addInitScript(() => {
        for (const key of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'mozRTCPeerConnection', 'WebTransport']) {
          try { Object.defineProperty(globalThis, key, { value: undefined, configurable: false, writable: false }); } catch {}
        }
      });
      context.on('response', response => {
        const path = new URL(response.url()).pathname, method = response.request().method(), status = response.status();
        if (loggingIn) {
          if (status >= 400 || (method === 'POST' && [307, 308].includes(status))) blocker = 'LOGIN_FAILED';
          return;
        }
        responses.push({ path, method, status });
        if (status >= 400 && !declaredErrors.some(c => c.selector === path && c.other_selector === method && Number(c.value) === status))
          blocker = 'UNEXPECTED_HTTP_ERROR';
      });
      const page = await context.newPage();
      page.setDefaultTimeout(scenario.timeout);
      page.on('pageerror', () => { blocker = 'PAGE_SCRIPT_ERROR'; });
      page.on('popup', () => { blocker = 'POPUP_UNSUPPORTED'; });
      page.on('download', () => { blocker = 'DOWNLOAD_UNSUPPORTED'; });
      if (scenario.login) {
        const auth = scenario.login, id = process.env.QA_LOGIN_ID, password = process.env.QA_LOGIN_PASSWORD;
        if (!id || !password) throw new Error('LOGIN_CREDENTIALS_REQUIRED');
        loggingIn = true;
        try {
          page.setDefaultTimeout(auth.timeout_ms);
          await page.goto(new URL(auth.login_path, entry.origin).href, { waitUntil: 'load' });
          const username = page.locator('css=' + auth.username_selector);
          const secret = page.locator('css=' + auth.password_selector);
          const submit = page.locator('css=' + auth.submit_selector);
          for (const target of [username, secret, submit]) {
            if (await target.count() !== 1 || !await target.isVisible() || !await target.isEnabled()) throw new Error('LOGIN_FAILED');
          }
          if (![null, 'text', 'email'].includes(await username.getAttribute('type')) || await secret.getAttribute('type') !== 'password')
            throw new Error('LOGIN_FAILED');
          if (auth.success_selector && await page.locator('css=' + auth.success_selector).isVisible()) throw new Error('LOGIN_FAILED');
          await username.fill(id);
          await secret.fill(password);
          guard();
          await submit.click();
          if (loginRedirect) await page.goto(loginRedirect, { waitUntil: 'load' });
          if (auth.success_path) await page.waitForURL(url => url.origin === entry.origin && url.pathname === auth.success_path,
            { waitUntil: 'load', timeout: auth.timeout_ms });
          if (auth.success_selector) await page.locator('css=' + auth.success_selector).waitFor({ state: 'visible', timeout: auth.timeout_ms });
          guard();
          if (!loginPostUsed || !allowed(page.url(), 'GET')) throw new Error('LOGIN_FAILED');
        } catch {
          // Discard Playwright call logs containing fill values; do not attach the cause.
          throw new Error('LOGIN_FAILED');
        } finally {
          loggingIn = false;
          page.setDefaultTimeout(scenario.timeout);
          responses.length = 0;
        }
      }
      for (const [index, step] of scenario.steps.entries()) {
        guard();
        if (step.action === 'navigate') {
          const variable = (scenario.navigationEnv as Record<string, string>)[String(index)];
          if (variable && !process.env[variable]) throw new Error('A navigation URL requires its configured environment variable');
          const original = new URL(step.target);
          const target = variable ? process.env[variable]! : index === 0 ? entry.href : new URL(original.pathname + original.search, entry.origin).href;
          if (!allowed(target, 'GET')) throw new Error('NAVIGATION_OUT_OF_SCOPE');
          await page.goto(target, { waitUntil: 'load' });
        } else {
          const target = page.locator('css=' + step.target);
          if (step.action === 'click') await target.click();
          else if (step.action === 'fill') {
            if (['password', 'hidden'].includes(await target.getAttribute('type') || '')) throw new Error('SENSITIVE_INPUT_UNSUPPORTED');
            await target.fill(step.value);
          } else await expect(target).toHaveText(step.value, { useInnerText: true, timeout: scenario.timeout });
        }
        guard();
      }
      await expect.poll(async () => { guard(); try { return await observe(page, scenario.ready, responses); } catch { return false; } },
        { timeout: scenario.timeout }).toBe(true);
      const expected = await observe(page, scenario.expected, responses);
      const symptom = await observe(page, scenario.symptom, responses);
      guard();
      outcomes.push({ attempt, expected, symptom });
    } finally { await context.close(); }
  }
  await test.info().attach('observed-outcomes', { body: JSON.stringify(outcomes), contentType: 'application/json' });
  expect(outcomes).toEqual([{ attempt: 1, expected: true, symptom: false }, { attempt: 2, expected: true, symptom: false }]);
});
'''
