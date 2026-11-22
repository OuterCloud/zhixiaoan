#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线自测：不连服务器，用假数据验证核心逻辑。

跑法：
    python3 zhixiaoan/selftest.py

覆盖：
  1. 请假还没结束的才批，已结束的不碰
  2. 请假类型过滤
  3. 同一轮里已批过的不会重复提交（守护模式每 60 秒扫一次，这条很关键）
  4. 失败会重试，连续 3 次后才放弃
  5. 审计日志确实落盘
"""

import datetime
import json
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approve  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}")


def mkargs(**kw):
    base = dict(for_real=True, date=None, types=None, interval=1, heartbeat=9999)
    base.update(kw)
    return types.SimpleNamespace(**base)


def rec(rid, create, name, vtype="事假", back=None):
    return {"id": rid, "studentName": name, "gradeName": "高中11年级",
            "className": "7班", "vacateTypeName": vtype, "createTime": create,
            "applyLeaveSchoolTime": create,
            "applyBackSchoolTime": back or create,
            "vacateNote": f"{name}的理由"}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="zxatest-"))
    # 两个全局路径都要指到临时目录，否则测试会写坏真的日志和 config.json
    approve.LOG_DIR = tmp
    approve.CONFIG_PATH = tmp / "config.json"
    # 保险丝：万一哪天改漏了，这里会立刻拦住，不至于写坏真实配置
    assert str(approve.CONFIG_PATH).startswith(str(tmp)), "测试必须隔离，不能碰真实配置"
    assert str(approve.LOG_DIR).startswith(str(tmp)), "测试必须隔离，不能碰真实日志"

    day_fmt = "%Y-%m-%d %H:%M:%S"
    today = approve.today_str()
    yday = (approve.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    future = (approve.now() + datetime.timedelta(days=1)).strftime(day_fmt)
    past = (approve.now() - datetime.timedelta(days=1)).strftime(day_fmt)

    data = [
        rec("t1", f"{today} 08:00:00", "今天的甲", "事假", back=future),
        rec("t2", f"{today} 09:00:00", "今天的乙", "病假", back=future),
        rec("y1", f"{yday} 20:00:00", "昨天的丙", "事假", back=past),
        rec("y2", f"{yday} 09:00:00", "昨天的丁", "事假", back=future),
    ]
    approve.fetch_pending = lambda cfg: [dict(x) for x in data]

    submitted = []

    def ok_approve(cfg, vid):
        submitted.append(vid)
        return {"success": True, "message": "操作成功！"}

    cfg = {"token": "x", "personId": "p", "organCode": "o",
           "userMode": "157", "approveNote": "同意"}

    print("\n场景一：只批请假还没结束的")
    approve.approve_one = ok_approve
    seen, attempts = set(), {}
    approve.scan_and_approve(cfg, mkargs(), seen, attempts)
    check("请假未结束的都提交了（含昨天提交的）", submitted == ["t1", "t2", "y2"])
    check("已结束的昨天那条没被碰", "y1" not in submitted)

    print("\n场景二：再扫一轮不重复提交")
    before = list(submitted)
    approve.scan_and_approve(cfg, mkargs(), seen, attempts)
    check("没有产生新的提交", submitted == before)

    print("\n场景三：按请假类型过滤")
    submitted.clear()
    seen3, att3 = set(), {}
    approve.scan_and_approve(cfg, mkargs(types=["病假"]), seen3, att3)
    check("只提交了病假那条", submitted == ["t2"])

    print("\n场景四：失败重试，连续 3 次后放弃")
    calls = []

    def bad_approve(cfg, vid):
        calls.append(vid)
        return {"success": False, "message": "实体不存在"}

    approve.approve_one = bad_approve
    seen4, att4 = set(), {}
    for _ in range(5):                          # 扫 5 轮
        approve.scan_and_approve(cfg, mkargs(types=["事假"], date=today), seen4, att4)
    check(f"只尝试了 3 次（实际 {len(calls)} 次）", len(calls) == 3)
    check("放弃后第 4、5 轮不再提交", ("157", "t1") in seen4)

    print("\n场景五：审计日志落盘")
    logs = list(tmp.glob("approve-*.jsonl"))
    check("生成了审计日志文件", len(logs) == 1)
    if logs:
        lines = [json.loads(x) for x in logs[0].read_text(encoding="utf-8").splitlines() if x.strip()]
        check(f"日志条数对得上（{len(lines)} 条）", len(lines) == 7)   # 3 + 0 + 1 + 3
        check("成功与失败都被记录", any(l["ok"] for l in lines) and any(not l["ok"] for l in lines))
        check("记录了学生与请假类型", all("student" in l and "type" in l for l in lines))

    print("\n场景六：登录提示（交互粘贴链接）")
    import builtins
    link = ("https://cloud.zuyxa.com/zyycloudattendance/zyycloudphone/leave/approve/info"
            "?id=x&token=aaa.bbb.ccc&personId=someone&userType=157&organCode=A03A00A00A00")
    answers = iter(["", "这是一段跟请假无关的话", link])
    builtins.input = lambda prompt="": next(answers)
    got = approve.prompt_for_link("测试")
    check("空输入和无关内容会被拒绝，最终接受链接", got is True)
    saved = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
    check("登录信息已写入配置", saved.get("token") == "aaa.bbb.ccc")
    check("从链接里解析出了账号与机构",
          saved.get("personId") == "someone" and saved.get("organCode") == "A03A00A00A00")
    check("tenant 由机构码前三位推出", saved.get("tenant") == "A03")

    print("\n场景七：确认程序不读剪贴板")
    check("已移除 read_clipboard", not hasattr(approve, "read_clipboard"))
    check("已移除 clipboard_watcher", not hasattr(approve, "clipboard_watcher"))
    src = Path(approve.__file__).read_text(encoding="utf-8")
    check("源码里没有 pbpaste（不碰系统剪贴板）", "pbpaste" not in src)
    check("源码里没有 threading（没有后台监听线程）", "threading" not in src)

    print("\n场景八：终端状态栏（让「还在扫」看得见）")
    import contextlib
    import io
    real_live, real_sleep = approve._LIVE, approve.time.sleep
    approve._LIVE = True
    approve.time.sleep = lambda s: None          # 别真等
    sbuf = io.StringIO()
    try:
        with contextlib.redirect_stdout(sbuf):
            approve.wait_with_status(3, "已扫 5 轮")
    finally:
        approve._LIVE, approve.time.sleep = real_live, real_sleep
    sout = sbuf.getvalue()
    check("逐秒倒计时 3/2/1", all(f"还有 {n} 秒" in sout for n in (3, 2, 1)))
    check("状态行带轮次信息", "已扫 5 轮" in sout)
    check("原地刷新而非刷屏（无换行）", "\r" in sout and "\n" not in sout)

    print("\n场景九：状态行按终端宽度收尾（窄终端不折行）")
    import os
    saved_columns = os.environ.get("COLUMNS")
    approve._LIVE = True
    approve.time.sleep = lambda s: None
    try:
        for cols in (80, 40):
            os.environ["COLUMNS"] = str(cols)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                approve.wait_with_status(3, "已扫 5 轮｜待批 1｜通过 0｜剩 23.8h")
            frames = [f for f in buf.getvalue().split("\r") if f]
            check(f"{cols} 列下每帧不超过 {cols - 1} 列",
                  all(approve._display_cols(f) <= cols - 1 for f in frames))
            check(f"{cols} 列下不回绕（无换行）", "\n" not in buf.getvalue())
    finally:
        approve._LIVE, approve.time.sleep = real_live, real_sleep
        if saved_columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = saved_columns

    print("\n场景十：没有可批的单时会说明原因，且只说一次")
    original_fetch = approve.fetch_pending
    approve.fetch_pending = lambda cfg: [dict(data[2])]      # 只剩已结束的那条
    noted = set()
    first = io.StringIO()
    with contextlib.redirect_stdout(first):
        counts = approve.scan_and_approve(cfg, mkargs(), set(), {}, noted)
    second = io.StringIO()
    with contextlib.redirect_stdout(second):
        approve.scan_and_approve(cfg, mkargs(), set(), {}, noted)
    approve.fetch_pending = original_fetch
    check("返回待批 1 条、可批 0 条", counts[0] == 1 and counts[1] == 0)
    check("首轮说明了不批的原因", "请假已于" in first.getvalue())
    check("第二轮不再重复说明", second.getvalue() == "")

    print("\n场景十一：多角色")
    check("没配 userModes 时只扫登录角色",
          approve.effective_modes({"userMode": "157"}) == ["157"])
    check("配了 userModes 时按配置扫",
          approve.effective_modes({"userMode": "157", "userModes": ["101", "157"]})
          == ["101", "157"])

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  ✗ {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
