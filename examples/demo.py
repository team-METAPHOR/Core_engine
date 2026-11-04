"""Run a stateless, loopback-only bug fixture without API keys."""

import argparse
import asyncio
from dataclasses import asdict
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from core_engine.browser import BrowserRunner
from core_engine.plan import BrowserPlan, Step, TextCheck
from core_engine.policy import RunPolicy


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


async def execute(origin, fixed, analysis=None):
    policy = RunPolicy("local-demo", 1, origin, ("/search_bug.html",),
                       allow_private_targets=True, private_cidrs=("127.0.0.1/32",))
    url = origin + "/search_bug.html" + ("?fixed=1" if fixed else "")
    plan = BrowserPlan((
        Step("navigate", url), Step("fill", "#query", "cat"), Step("click", "#search"),
        Step("assert_text", "#result", "cat"),
        Step("fill", "#query", "missing"), Step("click", "#search"),
    ), ready=TextCheck("#ready", "done"), symptom=TextCheck("#result", "cat"))

    async def reset(_attempt):
        # This fixture has no server state. Each fresh browser context starts empty.
        return True

    result = await BrowserRunner(policy).run(plan, reset=reset, is_authorized=lambda: True)
    output = asdict(result)
    if analysis:
        from analyze_report import analyze_example
        assessment = await asyncio.to_thread(analyze_example, run=result, live=analysis == "openai")
        output = {"browser_run": output, "analysis_mode": analysis, "analysis": asdict(assessment)}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if analysis and assessment.status != "completed":
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixed", action="store_true", help="수정된 화면을 검증")
    parser.add_argument("--analysis", choices=("fixture", "openai"),
                        help="fixture: 고정 응답 데모 / openai: 실제 API 1회 호출")
    args = parser.parse_args()
    handler = partial(QuietHandler, directory=str(Path(__file__).parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        asyncio.run(execute(f"http://127.0.0.1:{server.server_port}", args.fixed, args.analysis))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
