#!/bin/sh
# 智校安请假自动审批 —— 后台常驻管理
#
#   ./run.sh login        登录：会提示你粘贴微信里的链接（只在需要时做一次）
#   ./run.sh start        后台启动（每 60 秒扫一次，自动审批）
#   ./run.sh start 30     同上，改成每 30 秒
#   ./run.sh stop         停止
#   ./run.sh status       看状态
#   ./run.sh log          实时看日志（Ctrl+C 退出，不影响运行）
#
# 登录信息约 24 小时有效。启动时若发现没登录或已过期，会直接提示你粘贴链接；
# 后台运行时没法弹提示，就另开终端跑一次 ./run.sh login。
#
# 前台直接跑也行：python3 approve.py --for-real

set -e

DIR=$(cd "$(dirname "$0")" && pwd)
PIDFILE="$DIR/.daemon.pid"
LOGFILE="$DIR/logs/daemon.log"
PY=python3

running() {
    [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

case "$1" in
    login)
        cd "$DIR"
        "$PY" approve.py --login
        ;;
    start)
        if running; then
            echo "已经在跑了（PID $(cat "$PIDFILE")）。要重启先 ./run.sh stop"
            exit 0
        fi
        INTERVAL=${2:-60}
        mkdir -p "$DIR/logs"
        cd "$DIR"
        # 后台跑没法弹交互提示，所以先检查登录状态，缺了就先去登录
        if ! "$PY" -c "import approve,sys; c=approve.load_config(require=False); e=approve.token_expiry(c.get('token') or ''); sys.exit(0 if (all(c.get(k) for k in approve.REQUIRED) and e and e>approve.now()) else 1)"; then
            echo "✗ 还没有有效的登录信息，后台运行没法提示你粘贴。"
            echo "  请先执行：  ./run.sh login"
            echo "  登录完再跑：./run.sh start"
            exit 1
        fi
        nohup "$PY" approve.py --for-real --interval "$INTERVAL" >> "$LOGFILE" 2>&1 &
        echo $! > "$PIDFILE"
        sleep 1
        if running; then
            echo "✓ 已启动（PID $(cat "$PIDFILE")），每 ${INTERVAL} 秒扫一次"
            echo "  日志：$LOGFILE"
            echo "  停止：./run.sh stop"
        else
            echo "✗ 启动失败，看日志：$LOGFILE"
            tail -20 "$LOGFILE"
            exit 1
        fi
        ;;
    stop)
        if running; then
            PID=$(cat "$PIDFILE")
            kill "$PID" 2>/dev/null || true
            sleep 1
            kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null || true
            rm -f "$PIDFILE"
            echo "✓ 已停止（PID $PID）"
        else
            rm -f "$PIDFILE"
            echo "没在跑。"
        fi
        ;;
    status)
        if running; then
            echo "● 运行中（PID $(cat "$PIDFILE")）"
            echo
            tail -6 "$LOGFILE" 2>/dev/null
        else
            echo "○ 没在跑。启动：./run.sh start"
        fi
        ;;
    log)
        tail -f "$LOGFILE"
        ;;
    token)
        [ -z "$2" ] && { echo "用法：./run.sh token '<微信里复制的链接>'"; exit 1; }
        cd "$DIR"
        "$PY" approve.py --set-token "$2"
        ;;
    *)
        sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
