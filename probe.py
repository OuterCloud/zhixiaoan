#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口探针：用真实浏览器 + 现成的登录态打开某个页面，把它调用的所有 /api/ 接口
（方法、请求体、响应体）打出来。用来确定接口的真实参数格式。

登录态取自 config.json（先跑一次 ./run.sh login），不在这里写死任何账号信息。

用法：
    .venv/bin/python zhixiaoan/probe.py "<页面路径或完整 URL>" --wait 12
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import approve  # noqa: E402

ORIGIN = "https://cloud.zuyxa.com"


def build_userinfo(cfg):
    """页面的 localStorage 需要这份用户信息；姓名和学校只用于显示，用占位值。"""
    return {
        "id": cfg["personId"],
        "realname": "示例教师",
        "orgCode": cfg["organCode"],
        "orgName": "示例学校",
        "imgPath": None,
        "queryNumber": cfg["personId"],
        "documentType": None,
        "phone": None,
        "userType": cfg["userMode"],
        "feeStatus": None,
        "permissionList": [],
    }


def main():
    ap = argparse.ArgumentParser(description="打开页面，打印它调用的 /api/ 接口")
    ap.add_argument("path")
    ap.add_argument("--wait", type=float, default=10)
    ap.add_argument("--token", default=None, help="覆盖 config.json 里的 token")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "probe.py 需要 Playwright：python3 -m venv .venv && .venv/bin/pip install playwright"
        ) from None

    cfg = approve.load_config()
    token = args.token or cfg["token"]
    userinfo = build_userinfo(cfg)

    url = args.path if args.path.startswith("http") else ORIGIN + args.path

    calls = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context(locale="zh-CN", viewport={"width": 420, "height": 900})
        # 提前把登录态塞进 localStorage，等价于「已登录」
        ctx.add_init_script(
            "localStorage.setItem('pro__Access-Token', "
            "JSON.stringify({value: %s, expire: null}));" % json.dumps(token)
        )
        ctx.add_init_script(
            "localStorage.setItem('pro__Login_Userinfo', JSON.stringify({value: %s, expire: null}));"
            % json.dumps(userinfo)
        )
        pg = ctx.new_page()

        def on_request(r):
            if r.resource_type in ("xhr", "fetch"):
                calls.append({"method": r.method, "url": r.url,
                              "post": r.post_data, "resp": None, "status": None})

        def on_response(r):
            if r.request.resource_type not in ("xhr", "fetch"):
                return
            for c in reversed(calls):
                if c["url"] == r.url and c["resp"] is None:
                    c["status"] = r.status
                    try:
                        c["resp"] = r.text()[:4000]
                    except Exception as e:
                        c["resp"] = f"<读不到: {e}>"
                    break

        pg.on("request", on_request)
        pg.on("response", on_response)

        pg.goto(url, wait_until="load", timeout=60000)
        time.sleep(args.wait)

        print("=" * 70)
        print(f"页面：{url}")
        print(f"标题：{pg.title()}")
        print("=" * 70)
        for i, c in enumerate(calls, 1):
            print(f"\n--- [{i}] {c['method']} {c['url']}")
            if c["post"]:
                print(f"    请求体: {c['post'][:1500]}")
            print(f"    状态: {c['status']}")
            print(f"    响应: {(c['resp'] or '')[:1500]}")
        if not calls:
            print("\n（没有捕获到 /api/ 调用）")
            print("可见文本：", pg.evaluate("() => document.body.innerText")[:500])

        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
