#!/usr/bin/env bash
# 每日羊毛推荐表：抓取 → 生成 HTML → 发布静态网站 → cron 投递消息到 QQ。
#
# 2026-10-24 v5（用户定）：**推送走 cron 的 deliver=qqbot，脚本只负责把消息打到
# stdout**。v4 曾在脚本内直调 qqbot-send-file.py，与 cron 投递叠加会发两条重复
# 消息——用户明确要「QQ 推送」，cron 侧一条通道就够，消息内容也统一从这里出。
#   · 消息带「本轮条数 + 本次新增」——每天推 3 次，用户最想知道这次多了什么。
#   · 不再有 AI 点评——cron 是 no-agent 模式，零 token。
#   · 网站布局：仓库根目录 index.html = 最新一期；YYYY-MM-DD.html = 归档，
#     用 git push 发到 GitHub Pages（https://xiaopeng66.github.io/wool-daily/）。
#   · 往期未选：wool_daily.py 每轮只展示上轮之后的新线索（用户 2026-10-24 定）。
set -uo pipefail

PROJ="/mnt/e/AI/Zcode/projects/wool-daily"
LOG="$HOME/.hermes/logs/wool-daily.log"
DAY="$(date +%F)"
SITE_URL="https://xiaopeng66.github.io/wool-daily/"
GIT_NAME="xiaopeng66"
GIT_MAIL="xiaopeng66@users.noreply.github.com"
mkdir -p "$(dirname "$LOG")"

# 消息暂存文件：块内（重定向到日志）生成，块外打印到 stdout 交给 cron 投递。
# 每轮先清掉上一轮残留，避免生成失败时把旧消息又发一遍。
MSGFILE="$(mktemp /tmp/wool-msg.XXXXXX)"
trap 'rm -f "$MSGFILE"' EXIT
: > "$MSGFILE"

{
    echo "=== $(date '+%F %T') wool-daily start ==="
    cd "$PROJ" || { echo "project missing: $PROJ"; exit 1; }

    # 0) 部署前先同步远端（防止上一轮 push 没成功导致这轮 push 被拒）
    git pull --ff-only origin main >/dev/null 2>&1 || echo "[git] pull skipped/failed (continuing)"

    # 1) 生成（抓取失败不阻断：有几条源就出几条）
    python3 wool_daily.py --days 3 --date "$DAY" --no-pdf
    rc=$?
    if [ $rc -ne 0 ]; then
        echo "[fatal] generator exit=$rc"
        exit $rc
    fi
    PAGE="$PROJ/out/$DAY.html"
    [ -f "$PAGE" ] || { echo "[fatal] page missing: $PAGE"; exit 1; }

    # 2) 发布静态网站：index.html=最新，$DAY.html=归档，一起提交推送
    cp "$PAGE" index.html
    cp "$PAGE" "$DAY.html"
    # 归档去重：同一天多轮只留最新（文件名相同直接覆盖）
    git add index.html "$DAY.html"
    SITE_STATE="ok"
    if git diff --cached --quiet; then
        echo "[site] no change to publish"
    else
        git -c user.name="$GIT_NAME" -c user.email="$GIT_MAIL" \
            commit -m "wool update $DAY $(date +%H:%M)" >/dev/null
        # push 失败不阻断推送消息：下轮 pull+push 会补上（或手工补）
        # 判据用退出码：以前是 `git push | grep -q "main"`，而 push 的**失败**信息
        # （"failed to push some refs ... main"）同样含 "main"，会把失败当成功。
        if git push origin main >/dev/null 2>&1; then
            echo "[site] pushed"
        else
            echo "[site] push FAILED — 网站仍是旧版，下轮自动重试"
            SITE_STATE="push-failed"
        fi
    fi
    # Pages 构建要 30-60s：消息里带 ?v=<时间戳> 绕 CDN 缓存，点开就是新版
    BUST="$SITE_URL?v=$(date +%H%M)"

    # 3) 组装消息：本块只写日志；消息本身在块外用 stdout 输出，由 cron 投递到 QQ。
    #    消息带「本轮条数 + 本次新增」——每天推 3 次，用户最想知道这次多了什么。
    read -r ITEMS NEWROUND < <(python3 -c "
import json
d = json.load(open('$PROJ/data/$DAY.json', encoding='utf-8'))
it = d['items']
new = sum(1 for x in it if x.get('is_new_round'))
print(len(it), new)
" 2>/dev/null || echo "? 0")
    if [ "$NEWROUND" != "0" ] && [ "$NEWROUND" != "?" ]; then
        MSG="🐑 羊毛日报已更新（$DAY $(date +%H:%M)）
本次新增 $NEWROUND 条 · 在榜 $ITEMS 条
👉 $BUST"
    else
        MSG="🐑 羊毛日报已更新（$DAY $(date +%H:%M)）
本轮无新增 · 在榜 $ITEMS 条
👉 $BUST"
    fi
    if [ "$SITE_STATE" = "push-failed" ]; then
        MSG="$MSG
⚠ 网站推送失败，上面链接可能仍是旧版，下轮自动重试。"
    fi

    # 消息落盘给块外的 stdout 使用（子 shell 变量出不了管道作用域）
    printf '%s\n' "$MSG" > "$MSGFILE"
    echo "[msg] 已生成待投递消息（cron deliver=qqbot）"
    echo "=== $(date '+%F %T') wool-daily done site=$SITE_STATE ==="
} >>"$LOG" 2>&1

# 投递：stdout 就是 cron no_agent 的最终消息（deliver=qqbot → 发到 QQ）。
if [ -s "$MSGFILE" ]; then
    cat "$MSGFILE"
    rm -f "$MSGFILE"
else
    # 生成阶段就失败时 MSGFILE 不存在 → 非零退出，cron 发 QQ 失败告警
    echo "⚠️ 羊毛日报本轮失败，详见 ~/.hermes/logs/wool-daily.log" >&2
    exit 2
fi
