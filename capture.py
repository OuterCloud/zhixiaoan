#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智校安 抓包器（第一步工具）

干什么：
    用一个独立的 Chrome 窗口打开智校安的 H5 链接，把页面上所有的
    fetch / XHR 请求连同**请求体和响应体**完整录下来，存成 NDJSON。

为什么需要它：
    只有看清「待审批列表」和「同意」这两个动作到底调了哪个接口、
    带了什么参数，才可能写出稳定的自动化脚本（比模拟点击可靠得多）。

用法：
    .venv/bin/python zhixiaoan/capture.py "https://.../xxxx"
    .venv/bin/python zhixiaoan/capture.py                 # 开空白页，自己粘地址
    .venv/bin/python zhixiaoan/capture.py "URL" --seconds 120   # 2 分钟后自动收工

收工方式：
    Ctrl+C，或者关掉浏览器窗口，或者 --seconds 到点。
    收工时会额外把页面 HTML 和整页截图存到 capture/ 下面。
"""

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / ".profile"
OUTDIR = ROOT / "capture"

# 注入到页面里的钩子：拦截 fetch 和 XMLHttpRequest，把请求/响应记到 window.__cap
INJECT = r"""
(() => {
  if (window.__capInstalled) return;
  window.__capInstalled = true;
  window.__cap = [];
  const MAX = 200000;

  const of = window.fetch;
  if (of) {
    window.fetch = function (...args) {
      let url = '', method = 'GET', body = null;
      try {
        const a = args[0];
        url = (typeof a === 'string') ? a : ((a && a.url) || '');
        method = ((args[1] && args[1].method) || (a && a.method) || 'GET').toUpperCase();
        const b = args[1] && args[1].body;
        if (typeof b === 'string') body = b.slice(0, MAX);
      } catch (x) {}
      const e = { t: Date.now(), kind: 'fetch', url, method, body };
      window.__cap.push(e);
      return of.apply(this, args).then((res) => {
        e.status = res.status;
        try {
          res.clone().text().then((t) => { e.resp = t.slice(0, MAX); }).catch(() => {});
        } catch (x) {}
        return res;
      }).catch((err) => { e.error = String(err); throw err; });
    };
  }

  const oo = XMLHttpRequest.prototype.open;
  const os = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) {
    this.__m = m; this.__u = u; return oo.call(this, m, u, ...rest);
  };
  XMLHttpRequest.prototype.send = function (b) {
    const e = {
      t: Date.now(), kind: 'xhr', url: this.__u,
      method: String(this.__m || 'GET').toUpperCase(),
      body: (typeof b === 'string') ? b.slice(0, MAX) : null
    };
    window.__cap.push(e);
    this.addEventListener('load', () => {
      e.status = this.status;
      try { e.resp = String(this.responseText).slice(0, MAX); } catch (x) {}
    });
    return os.call(this, b);
  };
})();
"""


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


class Recorder:
    """把每个页面 window.__cap 里新增的条目追加进 NDJSON。"""

    def __init__(self, path):
        self.path = path
        self.fh = open(path, "a", encoding="utf-8")
        self.counts = {}   # page -> 已导出的条数

    def sweep(self, page):
        key = id(page)
        try:
            items = page.evaluate("() => window.__cap || []")
        except Exception:
            return 0
        if not isinstance(items, list):
            return 0
        seen = self.counts.get(key, 0)
        if len(items) < seen:
            seen = 0                      # 页面跳转过，计数清零
        fresh = items[seen:]
        self.counts[key] = len(items)
        if not fresh:
            return 0
        try:
            page_url = page.url
        except Exception:
            page_url = ""
        for it in fresh:
            it["page"] = page_url
            self.fh.write(json.dumps(it, ensure_ascii=False) + "\n")
        self.fh.flush()
        return len(fresh)

    def close(self):
        try:
            self.fh.close()
        except Exception:
            pass


def interesting(e):
    """过滤掉静态资源，只留接口流量。"""
    u = (e.get("url") or "").lower()
    if not u:
        return False
    if e.get("kind") in ("fetch", "xhr"):
        return True
    return not any(u.endswith(ext) for ext in
                   (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
                    ".css", ".woff", ".woff2", ".ttf", ".mp4", ".map"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="智校安 H5 链接；不传则开空白页")
    ap.add_argument("--seconds", type=int, default=0, help="多少秒后自动收工，0=手动")
    args = ap.parse_args()

    OUTDIR.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    ndjson = OUTDIR / f"capture-{stamp}.ndjson"
    rec = Recorder(ndjson)
    log(f"抓包文件：{ndjson}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            channel="chrome",            # 用你本机装好的 Chrome，不额外下载
            headless=False,
            viewport={"width": 420, "height": 900},   # 手机尺寸，贴近微信里的观感
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(INJECT)

        def wire(page):
            page.on("console", lambda m: log(f"  console[{m.type}] {m.text()[:200]}"))
            page.on("pageerror", lambda e: log(f"  pageerror {str(e)[:200]}"))
            page.on("framenavigated",
                    lambda f: log(f"  导航 → {f.url[:160]}") if f == page.main_frame else None)
            page.on("request", lambda r: log(f"  {r.method} {r.url[:160]}")
                    if r.resource_type in ("document", "xhr", "fetch") else None)

        for pg in ctx.pages:
            wire(pg)
        ctx.on("page", wire)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if args.url:
            log(f"打开：{args.url}")
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                log(f"打开报错（不致命，继续录）：{str(e)[:200]}")
        else:
            log("没给链接：请在窗口里自己粘贴地址并回车。")

        log("=" * 60)
        log("现在请在那个 Chrome 窗口里操作：")
        log("  1) 如果出现登录页，正常登录 / 微信扫码")
        log("  2) 进到「待审批」列表，点开一条请假单")
        log("  3) 如果有「同意/通过」按钮，点一次（想保险就先别点）")
        log("完成后回来按 Ctrl+C 收工。")
        log("=" * 60)

        t0 = time.time()
        try:
            while True:
                pages = list(ctx.pages)
                if not pages:
                    log("浏览器已关闭，收工。")
                    break
                n = sum(rec.sweep(pg) for pg in pages)
                if n:
                    log(f"  +{n} 条接口记录")
                if args.seconds and time.time() - t0 > args.seconds:
                    log("到点了，收工。")
                    break
                time.sleep(1.5)
        except KeyboardInterrupt:
            log("收到 Ctrl+C，收工。")

        # 收工快照：HTML + 截图
        for i, pg in enumerate(list(ctx.pages)):
            try:
                html = pg.content()
                (OUTDIR / f"page-{stamp}-{i}.html").write_text(html, encoding="utf-8")
                pg.screenshot(path=str(OUTDIR / f"page-{stamp}-{i}.png"), full_page=True)
                log(f"快照已存：page-{stamp}-{i}.html / .png")
            except Exception as e:
                log(f"存快照失败：{str(e)[:120]}")
            rec.sweep(pg)

        rec.close()
        try:
            ctx.close()
        except Exception:
            pass

    log(f"全部完成。抓包结果：{ndjson}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
