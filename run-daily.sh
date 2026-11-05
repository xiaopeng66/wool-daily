#!/usr/bin/env bash
# 每日羊毛推荐表：抓取 → 生成 HTML → 发布静态网站 → cron 投递消息到 QQ。
#
# ⚠️ 本文件是**部署副本的源**：cron 执行的是 ~/.hermes/scripts/wool-daily.sh，
#    不是仓库里的这份。改完必须同步 + 验证，否则线上行为一点不变：
#      cp /mnt/e/AI/Zcode/projects/wool-daily/run-daily.sh ~/.hermes/scripts/wool-daily.sh
#      chmod +x ~/.hermes/scripts/wool-daily.sh
#      python scripts/preflight.py     # 语法/换行/部署一致性/测试 一次过
#    注意：本文件必须是 LF。用 Python 写回会变成 CRLF，WSL bash 直接 syntax error。
#
# 2026-10-24 v5（用户定）：**推送走 cron 的 deliver=qqbot，脚本只负责把消息打到
# stdout**。v4 曾在脚本内直调 qqbot-send-file.py，与 cron 投递叠加会发两条重复
# 消息——用户明确要「QQ 推送」，cron 侧一条通道就够，消息内容也统一从这里出。
#   · 消息带「本轮条数 + 本次新增」——每天推 3 次，用户最想知道这次多了什么。
#   · 不再有 AI 点评——cron 是 no-agent 模式，零 token。
#   · 网站布局：仓库只放 index.html（最新一期），历史页面留在本地 out/（gitignore），
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

    # 0) 同步远端（2026-11-05 修）：远端只是前进 → ff 合并；远端历史被重写（如「干净导入」）
    #    会让本地与 origin/main 分叉，pull --ff-only 永远失败、push 每轮被拒，网站停在旧版。
    #    分叉时以远端为基线：reset --mixed 只动 HEAD/index、不动工作区，今天的页面文件还在。
    #    原历史先打 backup/pre-sync-<时间> 分支，找回用 git branch --list 'backup/*'。
    if git fetch origin main >/dev/null 2>&1; then
        if git merge-base --is-ancestor origin/main HEAD 2>/dev/null; then
            git merge --ff-only origin/main >/dev/null 2>&1 || echo "[git] ff 合并失败（继续）"
        else
            echo "[git] origin/main 与本地分叉（远端被重写？），以远端为基线"
            git branch -f "backup/pre-sync-$(date +%Y%m%d-%H%M)" HEAD >/dev/null 2>&1 || true
            git reset --mixed origin/main >/dev/null 2>&1 \
                || echo "[git] reset 到 origin/main 失败（继续，本轮 push 可能被拒）"
        fi
    else
        echo "[git] fetch 失败（网络/凭据），本轮推送可能失败"
    fi

    # 0.5) 备份分支只留最近 5 个：分叉自愈每次会打一个 backup/pre-sync-<YYYYMMDD-HHMM>，
    #      名字里带时间戳，按 refname 倒序就是新旧顺序（不用 creatordate——git branch -f
    #      建出来的分支，creatordate 是所指向提交的日期，不是分支创建时间）。
    git for-each-ref --sort=-refname --format='%(refname:short)' 'refs/heads/backup/pre-sync-*' \
        | tail -n +6 | while read -r b; do
            [ -n "$b" ] || continue
            git branch -D "$b" >/dev/null 2>&1 && echo "[git] 清理旧备份分支 $b"
        done

    # 1) 生成（抓取失败不阻断：有几条源就出几条）
    python3 wool_daily.py --days 3 --date "$DAY" --no-pdf
    rc=$?
    if [ $rc -ne 0 ]; then
        echo "[fatal] generator exit=$rc"
        exit $rc
    fi
    PAGE="$PROJ/out/$DAY.html"
    [ -f "$PAGE" ] || { echo "[fatal] page missing: $PAGE"; exit 1; }

    # 2) 发布静态网站：仓库只保留 index.html（最新一期），历史页面不进 git
    cp "$PAGE" index.html
    git add index.html
    SITE_STATE="ok"
    if git diff --cached --quiet; then
        echo "[site] no change to publish"
    else
        git -c user.name="$GIT_NAME" -c user.email="$GIT_MAIL" \
            commit -m "wool update $DAY $(date +%H:%M)" >/dev/null
        # push 失败不阻断推送消息：下轮 pull+push 会补上（或手工补）
        # 判据用退出码：以前是 `git push | grep -q "main"`，而 push 的**失败**信息
        # （"failed to push some refs ... main"）同样含 "main"，会把失败当成功。
        if PUSH_OUT="$(git push origin main 2>&1)"; then
            echo "[site] pushed"
        else
            # 把 git 的真实报错留在日志里（原来全吞了，只能看到 "push FAILED"，查不出原因）
            echo "[site] push FAILED: $(printf '%s' "$PUSH_OUT" | tail -c 300 | tr '\n' ' ')"
            echo "[site] 网站仍是旧版，下轮自动重试"
            SITE_STATE="push-failed"
        fi
    fi
    # Pages 构建要 30-60s：消息里带 ?v=<时间戳> 绕 CDN 缓存，点开就是新版
    BUST="$SITE_URL?v=$(date +%H%M)"

    # 3) 组装消息：本块只写日志；消息本身在块外用 stdout 输出，由 cron 投递到 QQ。
    #    消息带「本轮条数 + 本次新增 + 补抓」——每天推 3 次，用户最想知道这次多了什么；
    #    源抓取失败也要说（2026-10-28）：上轮漏抓的条目这轮会以「补抓」出现，原因在这。
    #    ITEMS=总条数，KEEP=在榜续留（2026-11-04 修：原来把总条数当「在榜」报，
    #    3 新增 + 9 在榜 = 12 总，消息却写成「在榜 12 条」，读者会以为新增没算进去）。
    read -r ITEMS NEWROUND CATCH KEEP SRCS < <(python3 -c "
import json
d = json.load(open('$PROJ/data/$DAY.json', encoding='utf-8'))
it = d['items']
new = sum(1 for x in it if x.get('is_new_round') and not x.get('is_catchup'))
catch = sum(1 for x in it if x.get('is_catchup'))
keep = len(it) - new - catch
fe = d.get('fetch_errors') or []
print(len(it), new, catch, keep, '、'.join(fe))
" 2>/dev/null || echo "? ? ? ? -")
    if [ "$NEWROUND" = "?" ]; then
        # 数据读不出来（生成失败/JSON 损坏）：别瞎报数，只报已更新
        MSG="🐑 羊毛日报已更新（$DAY $(date +%H:%M)）
👉 $BUST"
    elif [ "$NEWROUND" = "0" ] && [ "$CATCH" = "0" ]; then
        MSG="🐑 羊毛日报已更新（$DAY $(date +%H:%M)）
本轮无新增 · 在榜 $KEEP 条（共 $ITEMS 条）
👉 $BUST"
    else
        NEWPART="本次新增 $NEWROUND 条"
        [ "$CATCH" != "0" ] && NEWPART="$NEWPART · 补抓 $CATCH 条"
        MSG="🐑 羊毛日报已更新（$DAY $(date +%H:%M)）
$NEWPART · 在榜 $KEEP 条（共 $ITEMS 条）
👉 $BUST"
    fi
    if [ -n "$SRCS" ] && [ "$SRCS" != "-" ]; then
        MSG="$MSG
⚠ 本轮有源抓取失败（$SRCS），相关线索可能下一轮才补上。"
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
