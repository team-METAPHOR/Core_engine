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
    return names


def export_playwright_test(plan: BrowserPlan, *, policy, options=None, test_name="신고된 버그의 수정 확인") -> str:
    options = options or BrowserOptions()
    if not isinstance(plan, BrowserPlan) or plan.expected is None:
        raise ValueError("a validated plan with both outcomes is required")
    if not isinstance(test_name, str) or not 1 <= len(test_name) <= 200:
        raise ValueError("invalid test name")
    plan.validate_scope(policy)
    navigation_env, steps = {}, []
    for index, step in enumerate(plan.steps):
        if step.action in {"fill", "assert_text"} and redact_text(step.value) != step.value:
            raise ValueError("sensitive action value cannot be exported")
        if step.action == "navigate" and urlsplit(step.target).query:
            navigation_env[str(index)] = "QA_ENTRY_URL" if index == 0 else "QA_NAVIGATE_" + str(index)
        steps.append({"action": step.action, "target": redact_text(step.target), "value": step.value or ""})

    def encode(check):
        if isinstance(check, TextCheck):
            check = Check("text", check.selector, check.text)
        data = asdict(check)
        if any(redact_text(value) != value for value in data.values()):
            raise ValueError("sensitive predicate cannot be exported")
        return data

    data = {"steps": steps, "ready": encode(plan.ready), "expected": encode(plan.expected), "symptom": encode(plan.symptom),
        "timeout": plan.step_timeout_ms, "browser": options.browser_name, "responseDelayMs": options.response_delay_ms,
        "viewport": {"width": options.width, "height": options.height}, "sessionRequired": bool(options.session_headers),
        "allowedPaths": list(policy.allowed_path_prefixes), "excludedPaths": list(policy.excluded_path_prefixes),
        "methods": list(policy.allowed_methods), "navigationEnv": navigation_env}
    return ("// Generated from a validated scenario. Assert NORMAL behavior: buggy fails, fixed passes.\n"
            "// Run only in your approved test environment; configure a trusted reset hook.\n"
            "// This standalone browser test does not implement the engine's DNS/IP-pinning transport.\n"
            "import { test, expect, type Page } from '@playwright/test';\n"
            "import { pathToFileURL } from 'node:url';\nimport { resolve } from 'node:path';\n"
            "const scenario = " + json.dumps(data, ensure_ascii=True, indent=2) + ";\n"
            "const testName = " + json.dumps(redact_text(test_name), ensure_ascii=True) + ";\n" + _RUNTIME)


_RUNTIME = r'''
test.use({ browserName: scenario.browser as 'chromium' | 'firefox' | 'webkit' });

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
    let blocker = '';
    const guard = () => { if (blocker) throw new Error(blocker); };
    try {
      await context.route('**/*', async route => {
        if (!allowed(route.request().url(), route.request().method())) {
          blocker = 'REQUEST_OUT_OF_SCOPE'; await route.abort(); return;
        }
        if (scenario.responseDelayMs) {
          const response = await route.fetch({ maxRedirects: 0 });
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
        responses.push({ path, method, status });
        if (status >= 400 && !declaredErrors.some(c => c.selector === path && c.other_selector === method && Number(c.value) === status))
          blocker = 'UNEXPECTED_HTTP_ERROR';
      });
      const page = await context.newPage();
      page.setDefaultTimeout(scenario.timeout);
      page.on('pageerror', () => { blocker = 'PAGE_SCRIPT_ERROR'; });
      page.on('popup', () => { blocker = 'POPUP_UNSUPPORTED'; });
      page.on('download', () => { blocker = 'DOWNLOAD_UNSUPPORTED'; });
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
