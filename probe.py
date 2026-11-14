#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口探针：用真实浏览器 + 现成的登录态打开某个页面，把它调用的所有 /api/ 接口
（方法、请求体、响应体）打出来。用来确定接口的真实参数格式。

用法：
    .venv/bin/python zhixiaoan/probe.py "/zyycloudattendance/zyycloudphone/leave/approve/list?id=...&token=...&personId=...&userType=157&organCode=..."
    .venv/bin/python zhixiaoan/probe.py "<完整 URL>" --wait 12
"""

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
ORIGIN = "https://cloud.zuyxa.com"

USERINFO = {
    # 占位值：换成你自己账号的信息，否则页面拿不到数据
    "id": "00000000000000000000000000000000",
    "realname": "示例教师",
    "orgCode": "A03A00A00A00",
    "orgName": "示例学校",
    "imgPath": None,
    "queryNumber": "00000000000000000000000000000000",
    "documentType": None,
    "phone": None,
    "userType": "157",
    "feeStatus": None,
    "permissionList": [],
}


def jload(s):
    """localStorage 里的值是 {value:..., expire:...} 包装过的，解开它。"""
    try:
        return json.loads(s).get("value")
    except Exception:
        return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--wait", type=float, default=10)
    ap.add_argument("--token", default=None, help="不传则从上次抓包里取")
    args = ap.parse_args()

    tokfile = ROOT / "token.txt"
    token = args.token
    if token is None:
        if not tokfile.exists():
            print("缺少 token：用 --token 传，或先跑一次 capture.py")
            return 2
        token = tokfile.read_text(encoding="utf-8").strip()

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
            % json.dumps(USERINFO)
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
