#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智校安 · 请假申请自动审批

干什么：
    拉取「待审批」列表，把**请假还没结束**的请假单逐条同意。
    默认只演示不写操作；加 --for-real 才真的提交。

为什么是逐条而不是批量接口：
    系统有个 bathApprove 批量接口，但它会同意**所有**待审批（包括以前积压的），
    跟「只通过请假还没结束的」不符，所以这里只走单条 approve，逐条判断。

依赖：
    只用 Python 标准库，系统自带 python3 就能跑，不需要装任何东西。

用法：
    python3 zhixiaoan/approve.py                 # 预览这一轮会批哪些（不提交）
    python3 zhixiaoan/approve.py --for-real      # 真的提交
    python3 zhixiaoan/approve.py --watch --for-real --interval 60   # 常驻轮询

配置：
    zhixiaoan/config.json，首次运行会自动生成模板。
    换 token：微信里重新点开一次请假提醒，点右上角「...」→「复制链接」，
    把链接里的 token 段替换进去即可（token 有效期约 24 小时）。
"""

import argparse
import base64
import datetime
import json
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "logs"

BASE = "https://cloud.zuyxa.com/api"
LIST_URL = BASE + "/vacate-attendance-service/vacate/studentVacate/vacateApproveList"
APPROVE_URL = BASE + "/vacate-attendance-service/vacate/studentVacate/approve"

CONFIG_TEMPLATE = {
    "token": "",
    "personId": "",
    "organCode": "",
    "userMode": "",
    "userModes": [],
    "tenant": "",
    "approveNote": "同意",
}

# 平台的 status 取值
STATUS_PENDING = 0
STATUS_APPROVED = 1
STATUS_REJECTED = 2

REQUIRED = ("token", "personId", "organCode", "userMode", "tenant")


# ---------------------------------------------------------------- 基础工具

def now():
    return datetime.datetime.now()


def today_str():
    return now().strftime("%Y-%m-%d")


_LIVE = sys.stdout.isatty()      # 在终端里跑：用一行就地刷新的状态栏
_FALLBACK_COLS = 118             # 拿不到终端宽度时按这个算


def _char_cols(ch):
    """一个字符在终端里占几列：全角/宽字符占 2 列，组合字符不占列。"""
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _display_cols(text):
    return sum(_char_cols(ch) for ch in text)


def _term_cols():
    """当前终端列数；拿不到就退回固定值。"""
    return max(20, shutil.get_terminal_size((_FALLBACK_COLS, 24)).columns)


def _fit_line(text):
    """按显示列数截断并补空格，保证一行写满也不会回绕。

    一旦让终端自动折行，下一帧就会从新的一行开始，状态栏会一路往下刷。
    """
    width = _term_cols() - 1     # 留最后一列，避开「写满即换行」的边界
    kept, used = [], 0
    for ch in text:
        cols = _char_cols(ch)
        if used + cols > width:
            break
        kept.append(ch)
        used += cols
    return "".join(kept) + " " * (width - used)


def _clear_status():
    """把就地的状态行擦掉，好让正式输出从行首开始。"""
    if _LIVE:
        sys.stdout.write("\r" + " " * (_term_cols() - 1) + "\r")
        sys.stdout.flush()


def log(msg):
    _clear_status()
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


def status(msg):
    """状态行：终端里原地刷新（不刷屏），写日志时按调用方节奏落盘。"""
    if _LIVE:
        sys.stdout.write("\r" + _fit_line(msg))
        sys.stdout.flush()
    else:
        log(msg)


def wait_with_status(seconds, line):
    """等待下一轮，同时原地显示倒计时——让「还活着」这件事看得见。"""
    for remaining in range(int(seconds), 0, -1):
        status(f"{line}｜下次检查还有 {remaining} 秒")
        time.sleep(1)


def load_config(require=True):
    """读配置。require=False 时允许还没有登录态（守护模式会交互式问用户要）。"""
    if not CONFIG_PATH.exists():
        save_config(dict(CONFIG_TEMPLATE))
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        cfg = dict(CONFIG_TEMPLATE)
    if require:
        missing = [k for k in REQUIRED if not cfg.get(k)]
        if missing:
            raise SystemExit(
                f"配置里还缺这些字段：{', '.join(missing)}（文件：{CONFIG_PATH}）\n"
                "提示：直接运行守护模式，它会提示你粘贴登录链接，不用手填。"
            )
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def effective_modes(cfg):
    """要扫的角色。config 里写了 userModes 就用它，否则只用登录链接带的 userMode。"""
    modes = [str(m) for m in (cfg.get("userModes") or []) if m]
    return modes or [str(cfg.get("userMode") or "")]


def parse_link(link):
    """从微信「查看详情」链接里提取登录信息。

    链接形如：
      https://cloud.zuyxa.com/zyycloudattendance/zyycloudphone/leave/approve/info
          ?id=...&token=<JWT>&personId=...&userType=...&organCode=...
    一个链接就带齐了全部所需信息，所以不需要用户额外填任何东西。
    """
    link = (link or "").strip().strip('"').strip("'")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
    got = {k: (query.get(k) or [None])[0]
           for k in ("token", "personId", "userType", "organCode")}
    if not got["token"] or not got["personId"]:
        raise ValueError("链接里没有 token / personId")
    organ = got["organCode"] or ""
    return {
        "token": got["token"],
        "personId": got["personId"],
        "userMode": got["userType"] or "",
        "organCode": organ,
        "tenant": organ[:3],
    }


def merge_link(cfg, link):
    """把链接里的信息合并进现有配置（拿不到的字段保留原值）。"""
    new = parse_link(link)
    out = dict(cfg)
    for key, val in new.items():
        if val:
            out[key] = val
    out.setdefault("approveNote", "同意")
    return out


def set_token_from_link(link):
    """命令行手动刷新登录态（不进入交互，直接传链接）。"""
    try:
        cfg = merge_link(load_config(require=False), link)
    except ValueError as e:
        raise SystemExit(f"{e}，确认复制的是请假提醒的「查看详情」链接") from None
    save_config(cfg)

    exp = token_expiry(cfg["token"])
    log(f"✓ 已更新 {CONFIG_PATH.name}")
    log(f"  账号 {cfg['personId']}｜机构 {cfg['organCode']}｜角色 {cfg['userMode']}")
    if exp:
        log(f"  新 token 有效期至 {exp:%Y-%m-%d %H:%M}")
    return 0


# ------------------------------------------------- 登录态获取（交互式）

LINK_RE = re.compile(r'https?://[^\s"\'<>]*zuyxa\.com[^\s"\'<>]*')


def extract_link(text):
    """从用户粘贴的内容里挑出智校安链接。

    微信「复制链接」有时会夹带前后文字，所以这里做一次宽松匹配。
    注意：只在用户主动粘贴时调用，程序不会去读剪贴板。
    """
    if not text:
        return None
    m = LINK_RE.search(text)
    return m.group(0) if m else None


def token_expiry(token):
    """从 JWT 里读出过期时间；读不到就返回 None。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.datetime.fromtimestamp(data["exp"])
    except Exception:
        return None


def prompt_for_link(reason):
    """在终端里直接让用户粘贴链接。

    返回 True 表示拿到了并已写入；False 表示用户放弃（Ctrl+C / Ctrl+D）。
    """
    print()
    print("=" * 62)
    print(f"【{reason}】")
    print()
    print("请按下面两步操作：")
    print("  第 1 步：到微信里，对着那条「请假条提醒」点右键")
    print("            → 选「复制链接」")
    print("  第 2 步：回到这个窗口，粘贴（⌘V），然后按回车")
    print("=" * 62)
    while True:
        try:
            raw = input("请粘贴链接后回车：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not raw:
            print("  ↑ 没收到内容。请先在微信里「复制链接」，再回来粘贴。", flush=True)
            continue
        link = extract_link(raw)
        if not link:
            print("  ↑ 这看起来不是智校安的请假提醒链接，请重新复制一次。", flush=True)
            continue
        try:
            cfg = merge_link(load_config(require=False), link)
        except ValueError as e:
            print(f"  ↑ 链接里缺必要信息（{e}），请重新复制。", flush=True)
            continue
        save_config(cfg)
        exp = token_expiry(cfg["token"])
        log("✓ 登录成功" + (f"，有效期到 {exp:%m-%d %H:%M}" if exp else ""))
        return True


def request_json(url, cfg, method="GET", params=None, body=None, timeout=40):
    """发请求。平台偶尔会很慢，所以读超时给到 40 秒；超时单独提示，别甩英文异常。"""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("authorization", "Bearer " + cfg["token"])
    req.add_header("tenant", cfg["tenant"])
    req.add_header("accept", "application/json")
    if data is not None:
        req.add_header("content-type", "application/json;charset=UTF-8")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {raw[:300]}") from None
    except TimeoutError:
        raise RuntimeError(f"请求超时（{timeout} 秒没有响应），稍后重试") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"连不上服务器：{e.reason}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"返回的不是 JSON：{raw[:300]}") from None


# ---------------------------------------------------------------- 业务动作

def fetch_pending(cfg, page_size=50):
    """拉全量待审批列表。"""
    items, page = [], 1
    while True:
        payload = request_json(LIST_URL, cfg, params={
            "_t": int(time.time()),
            "queryPersonId": cfg["personId"],
            "organCode": cfg["organCode"],
            "pageNo": page,
            "pageSize": page_size,
            "status": STATUS_PENDING,
            "studentName": "",
            "userMode": cfg["userMode"],
        })
        if not payload.get("success"):
            raise RuntimeError(f"拉列表失败：{payload.get('message')}（code={payload.get('code')}）")
        result = payload.get("result") or {}
        records = result.get("records") or []
        items.extend(records)
        if len(items) >= (result.get("total") or 0) or not records:
            break
        page += 1
    return items


def approve_one(cfg, vacate_id):
    """同意单条请假。"""
    return request_json(APPROVE_URL, cfg, method="POST", body={
        "approveCode": 1,
        "vacateId": vacate_id,
        "personId": cfg["personId"],
        "userMode": cfg["userMode"],
        "approveNote": cfg.get("approveNote") or "同意",
    })


def write_audit(record):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"approve-{today_str()}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_time(value):
    """解析平台的 "YYYY-MM-DD HH:MM:SS"；解析不了返回 None。"""
    try:
        return datetime.datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def leave_open(it, when=None):
    """请假还没结束（返校时间还没到）。时间缺失或不认识格式时返回 False。"""
    end = parse_time(it.get("applyBackSchoolTime"))
    return end is not None and end > (when or now())


def pick_pending(items, day=None, types=None):
    """挑出该批的：给了日期就按提交日期筛，否则只批请假还没结束的。"""
    out = []
    for it in items:
        if day:
            if not str(it.get("createTime") or "").startswith(day):
                continue
        elif not leave_open(it):
            continue
        if types and it.get("vacateTypeName") not in types:
            continue
        out.append(it)
    return out


def skip_reason(it, day=None, types=None):
    """说明这条为什么没进待批队列，日志里给用户一个明确交代。"""
    if day and not str(it.get("createTime") or "").startswith(day):
        return f"提交日期不是 {day}"
    if types and it.get("vacateTypeName") not in types:
        return f"类型「{it.get('vacateTypeName')}」不在筛选范围内"
    end = it.get("applyBackSchoolTime")
    if not leave_open(it):
        return f"请假已于 {end} 结束" if end else "缺少返校时间，无法判断是否结束"
    return "之前尝试过，暂不重复提交"


def describe(it):
    return (f"{it.get('studentName','?')} "
            f"({it.get('gradeName','')}{it.get('className','')}) "
            f"{it.get('vacateTypeName','')} "
            f"{it.get('applyLeaveSchoolTime','')} → {it.get('applyBackSchoolTime','')} "
            f"提交于 {it.get('createTime','')}")


# ---------------------------------------------------------------- 主流程

MAX_ATTEMPTS = 3          # 同一条连续失败多少次后暂时放弃


def scan_and_approve(cfg, args, seen, attempts, noted=None):
    """扫一轮：拉列表 → 挑出该批的 → 逐条同意。

    默认只批请假还没结束的；给了 --date 就按提交日期精确筛。
    返回 (待审批总数, 本轮可批条数, 成功数, 失败数)。
    """
    mode = str(cfg.get("userMode") or "")
    items = fetch_pending(cfg)
    targets = pick_pending(items, args.date, args.types)

    target_ids = {t.get("id") for t in targets}
    todo = [t for t in targets if (mode, t.get("id")) not in seen]

    if not todo:
        # 没有可批的：把被过滤掉的单子说明一次，免得用户看着「待批」却不知道原因
        if noted is not None:
            for it in items:
                key = (mode, it.get("id"))
                if key in noted:
                    continue
                noted.add(key)
                log(f"不动 {describe(it)}：{skip_reason(it, args.date, args.types)}")
        return len(items), 0, 0, 0

    skipped = [i for i in items if i.get("id") not in target_ids]
    if skipped:
        log(f"跳过 {len(skipped)} 条（不会动它们）：")
        for it in skipped[:5]:
            log(f"    - {describe(it)}（{skip_reason(it, args.date, args.types)}）")
        if len(skipped) > 5:
            log(f"    …… 还有 {len(skipped) - 5} 条")

    log("—" * 62)
    log(f"发现 {len(todo)} 条可批的待审批：")
    for it in todo:
        log(f"  · {describe(it)}")
        if it.get("vacateNote"):
            log(f"    理由：{it['vacateNote']}")
    log("—" * 62)

    if not args.for_real:
        log("预览模式，未提交。加 --for-real 才会真的审批。")
        return len(items), len(todo), 0, 0

    ok = fail = 0
    for it in todo:
        vid = it.get("id")
        good, msg = False, ""
        try:
            resp = approve_one(cfg, vid)
            good = bool(resp.get("success"))
            msg = resp.get("message") or ""
        except RuntimeError as e:
            msg = str(e)

        if good:
            ok += 1
            seen.add((mode, vid))           # 已通过，不再重复提交
            log(f"  ✓ {it.get('studentName','?')} — {msg}")
        else:
            fail += 1
            key = (mode, vid)
            attempts[key] = attempts.get(key, 0) + 1
            n = attempts[key]
            log(f"  ✗ {it.get('studentName','?')} — {msg}（第 {n} 次失败）")
            if n >= MAX_ATTEMPTS:
                seen.add(key)               # 别无限重试，留给人工看一眼
                log(f"    连续 {MAX_ATTEMPTS} 次失败，暂时放弃这条。")

        write_audit({
            "time": str(now()), "ok": good, "vacateId": vid, "userMode": mode,
            "student": it.get("studentName"), "grade": it.get("gradeName"),
            "className": it.get("className"), "type": it.get("vacateTypeName"),
            "createTime": it.get("createTime"),
            "leaveTime": f"{it.get('applyLeaveSchoolTime')} → {it.get('applyBackSchoolTime')}",
            "note": it.get("vacateNote"), "message": msg,
            "attempt": attempts.get((mode, vid), 0),
        })
    return len(items), len(todo), ok, fail


def run_daemon(args):
    """守护模式：一直挂着，定时扫，有新假条就走自动审批。"""
    seen, attempts = set(), {}          # 本次运行内已处理 / 已重试过的单号
    noted = set()                       # 已解释过「为什么不批」的单号，避免每轮重复提示
    scans = ok_total = fail_total = 0
    last_beat = 0.0
    token_state = None
    interactive = sys.stdin.isatty()    # 终端里跑才问得出来，后台跑不行

    try:
        while True:
            cfg = load_config(require=False)   # 每轮重读，登录态一变就生效
            exp = token_expiry(cfg["token"]) if cfg.get("token") else None
            expired = exp is not None and exp <= now()
            no_login = any(not cfg.get(k) for k in REQUIRED)

            # --- 没登录 / 登录过期 ---
            if no_login or expired:
                if interactive:
                    reason = "登录信息已过期" if expired else "还没有登录信息"
                    if not prompt_for_link(reason):
                        log("没有得到登录信息，程序退出。")
                        return 1
                    continue                     # 立刻重读配置，进入正常扫描
                # 后台运行时没法弹提示，等用户在外面登录
                if token_state != "waiting":
                    log("=" * 62)
                    log(f"【{'登录已过期' if expired else '还没登录'}】审批暂停，程序保持运行。")
                    log("  请另开一个终端执行：  ./run.sh login")
                    log("  登录完这里会自动继续，不用重启。")
                    log("=" * 62)
                    token_state = "waiting"
                time.sleep(min(args.interval, 5))
                continue

            if token_state == "waiting":
                log(f"✓ 登录信息已更新（有效期到 {exp:%m-%d %H:%M}），继续审批。")
            token_state = "ok"

            try:
                n_all = n_todo = ok = fail = 0
                for mode in effective_modes(cfg):
                    mcfg = dict(cfg)
                    mcfg["userMode"] = mode
                    a, b, o, f = scan_and_approve(mcfg, args, seen, attempts, noted)
                    n_all += a
                    n_todo += b
                    ok += o
                    fail += f
                ok_total += ok
                fail_total += fail
                scans += 1
            except KeyboardInterrupt:
                break
            except Exception as e:
                log(f"✗ 本轮出错，{args.interval} 秒后重试：{e}")
                time.sleep(args.interval)
                continue

            # 状态显示
            left = (exp - now()) if exp else None
            # 终端一般只有 80 列，中文还占两列，所以这里只留关键项，别写太长
            line = (f"· 已扫 {scans} 轮｜待批 {n_all}｜可批 {n_todo}｜通过 {ok_total}"
                    + (f"｜剩 {left.total_seconds() / 3600:.1f}h" if left else ""))
            if _LIVE:
                wait_with_status(args.interval, line)      # 终端里带倒计时原地刷新
            else:
                if time.time() - last_beat >= args.heartbeat:
                    log(line)                              # 写日志时按心跳间隔落盘
                    last_beat = time.time()
                time.sleep(args.interval)
    except KeyboardInterrupt:
        _clear_status()

    log(f"已停止。共扫 {scans} 轮，累计通过 {ok_total} 条，失败 {fail_total} 条。")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="智校安请假自动审批",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("常用：\n"
                "  python3 approve.py --for-real            挂着，每 60 秒扫一次并自动审批\n"
                "  python3 approve.py                      只预览一轮，不提交\n"
                "  python3 approve.py --for-real --once    只扫一轮就退出\n"
                "  python3 approve.py --login              只登录，不审批\n"),
    )
    ap.add_argument("--for-real", action="store_true",
                    help="真的提交审批；不加则只预览")
    ap.add_argument("--once", action="store_true", help="只扫一轮就退出，不常驻")
    ap.add_argument("--watch", action="store_true",
                    help="强制常驻（带 --for-real 时本来就是常驻）")
    ap.add_argument("--login", action="store_true",
                    help="只做登录（提示你粘贴链接），完了就退出")
    ap.add_argument("--interval", type=int, default=60,
                    help="扫描间隔秒数，默认 60")
    ap.add_argument("--heartbeat", type=int, default=600,
                    help="心跳日志间隔秒数，默认 600（10 分钟）")
    ap.add_argument("--date", default=None,
                    help="只处理该日期提交的申请（默认不按日期，只批请假还没结束的）")
    ap.add_argument("--types", default="",
                    help="只通过这些请假类型，逗号分隔，如「病假,事假」；默认不限")
    ap.add_argument("--set-token", metavar="链接",
                    help="直接把链接作为参数传入，不进入交互")
    args = ap.parse_args()

    if args.set_token:
        return set_token_from_link(args.set_token)

    # --login：只登录，完了就走
    if args.login:
        cfg = load_config(require=False)
        exp = token_expiry(cfg["token"]) if cfg.get("token") else None
        if exp and exp > now():
            log(f"当前登录信息还有效（到 {exp:%m-%d %H:%M}），不需要重新登录。")
            log("想强制换一个，用：./run.sh token '<链接>'")
            return 0
        if not sys.stdin.isatty():
            log("✗ 这里不是终端，没法提示你粘贴。请在终端里直接运行：python3 approve.py --login")
            return 1
        return 0 if prompt_for_link("需要登录信息") else 1

    args.types = [t.strip() for t in args.types.split(",") if t.strip()] or None
    args.interval = max(5, args.interval)              # 别把人家服务器扫爆

    daemon_mode = args.watch or (args.for_real and not args.once)

    cfg = load_config(require=False)
    has_token = bool(cfg.get("token"))
    exp = token_expiry(cfg["token"]) if has_token else None
    needs_login = (not has_token) or (exp is not None and exp <= now())
    scope = f"只处理 {args.date} 提交的" if args.date else "只处理请假还没结束的"

    log("=" * 62)
    if cfg.get("personId"):
        log(f"账号    ：{cfg['personId']}")
        log(f"机构    ：{cfg['organCode']}｜角色 {'/'.join(effective_modes(cfg))}")
    log(f"范围    ：{scope}申请"
        + (f"，类型限 {'/'.join(args.types)}" if args.types else ""))
    log(f"模式    ：{'★ 真实提交 ★' if args.for_real else '预览（不提交）'}"
        + (f"｜常驻，每 {args.interval} 秒扫一次" if daemon_mode else "｜单次"))
    if not has_token:
        log("登录信息：还没有")
    elif exp:
        left = exp - now()
        log(f"登录信息：有效期到 {exp:%m-%d %H:%M}（还剩 {left.total_seconds() / 3600:.1f} 小时）")
        if left.total_seconds() <= 0:
            log("登录信息：已过期")
        elif left.total_seconds() < 3600:
            log("⚠ 一小时内会过期，到时程序会提示你重新登录")
    log("=" * 62)

    # 需要登录：终端里就直接问；后台跑则提示去别的终端登录
    if needs_login:
        if sys.stdin.isatty():
            reason = "登录信息已过期" if has_token else "还没有登录信息"
            if not prompt_for_link(reason):
                log("没有得到登录信息，退出。")
                return 1
            cfg = load_config(require=not daemon_mode)
            exp = token_expiry(cfg["token"]) if cfg.get("token") else None
        elif daemon_mode:
            log("⚠ 当前不在终端里，没法提示你粘贴链接。")
            log("  请另开一个终端执行：  ./run.sh login")
            log("  登录完这个进程会自动接上，不用重启。")
        else:
            log("✗ 需要登录信息，但这里不是终端。请在终端里运行，或用 --set-token '<链接>'。")
            return 1

    if daemon_mode:
        log("已启动。有新的请假申请会自动审批；想停下来按 Ctrl+C（后台的用 ./run.sh stop）。")
        return run_daemon(args)

    try:
        seen = set()
        for mode in effective_modes(cfg):
            mcfg = dict(cfg)
            mcfg["userMode"] = mode
            scan_and_approve(mcfg, args, seen, {})
    except RuntimeError as e:
        log(f"✗ {e}")
        return 1
    if not args.for_real:
        log("确认无误后加 --for-real 再跑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
