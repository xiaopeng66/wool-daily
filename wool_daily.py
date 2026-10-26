#!/usr/bin/env python3
"""每日羊毛推荐表：抓 24-72h 内的福利线索，打分、分类、生成 HTML + PDF。

设计口径（2026-10-17 与用户确认）：
  · **只做推荐表，不写单篇稿**——先让人一眼看出"今天有哪些值得写"。
  · 题材：生活羊毛（会员/立减金/证件照/打车券）+ 云厂商活动 + 域名 + 软件限免。
  · **VPS / 梯子（机场）类不要**（2026-10-24 定）：VPS/主机商优惠整体排除，
    云厂商官方活动（代金券/免费额度）不受影响。
  · 硬排除：交易（收/出）、已结束、抽奖/概率性、aff/邀请码、VPS/主机、梯子/机场。
  · 打分只回答一个问题：**这条值不值得花时间做成帖子**。
      确定性 > 新鲜度 > 入口清晰 > 无付费前置 > 受众广度。

输出：
  data/YYYY-MM-DD.json    结构化结果（给 LLM/后续流程用）
  out/YYYY-MM-DD.html     推荐表（自包含，可直接看，也是推送的附件）
  out/YYYY-MM-DD.pdf      同上的 A4 打印版（仅 --days 手动跑时生成，推送已不发 PDF）

跑：python3 wool_daily.py [--days 3] [--date 2026-10-17] [--no-pdf]
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "out"
CACHE = DATA / "cache"
SEEN = DATA / "seen.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# ------------------------------------------------------------------ 抓取层
# 2026-10-24 单一引擎：抓取直接复用 baipiao-daily skill 的 fetch_sources.py——
# 全部 26 个源、RSS/Atom/V2EX JSON/TG 预览页四套解析器、代理回退（默认→直连→Clash）、
# 同域限速，全部一处收口。本文件只保留 打分/排除/HTML 生成。
# WSL（cron）与 Windows（手动跑）都能 import：两个路径都试一遍。
_SKILL_SCRIPTS = None
for _p in ("/mnt/e/AI/Zcode/.agents/skills/baipiao-daily/scripts",
           "E:/AI/Zcode/.agents/skills/baipiao-daily/scripts"):
    if Path(_p, "fetch_sources.py").exists():
        _SKILL_SCRIPTS = _p
        break
if _SKILL_SCRIPTS is None:
    sys.exit("找不到 baipiao-daily skill 的 scripts/（fetch_sources.py）——路径变了要同步改这里")
sys.path.insert(0, _SKILL_SCRIPTS)
import fetch_sources as fs  # noqa: E402

# 抓取失败源名（main() 里按「没产出」回填，页面告警条用）
FETCH_ERRORS: list[str] = []


def fetch_all() -> list[dict]:
    """抓全部源，转成本文件的行格式（published 统一成 tz-aware datetime）。

    NodeSeek 的「WSL 直连被 reset」老问题由引擎的 Clash 回退解决
    （2026-10-24 实测 WSL 可直连 127.0.0.1:7897），win_fetch.py 那套不再需要。
    """
    raw = fs.fetch_rows(list(fs.SOURCES))
    rows = []
    for r in raw:
        rows.append({
            "title": r["title"],
            "url": r["url"],
            "summary": r.get("summary", ""),
            "published": fs.parse_published(r.get("published", "")),
            "published_raw": r.get("published", ""),
            "source": r.get("source", ""),
            "source_label": r.get("source_label")
                            or fs.SOURCE_LABELS.get(r.get("source", ""), r.get("source", "")),
        })
    return rows

# ------------------------------------------------------------------ 题材口径
# 分类/标记/打分/硬排除的唯一出处是 skill 里的 wool_policy.py（两个引擎共用，
# 改口径只改那一个文件）。这里只保留本引擎特有的展示常量。
import wool_policy as policy  # noqa: E402  # 与 fetch_sources 同目录（已入 sys.path）


# ------------------------------------------------------------------ 富化：补正文摘要
def enrich(name: str, row: dict, timeout: int = 15) -> None:
    """给 jike 这类 RSS 无描述的条目补 `meta description`（含领取路径/门槛）。"""
    if row.get("summary") or name != "jike":
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^0-9]", "", row["url"]) or str(abs(hash(row["url"])))
    cp = CACHE / f"desc-{key}.json"
    if cp.exists() and time.time() - cp.stat().st_mtime < 12 * 3600:
        try:
            row["summary"] = json.loads(cp.read_text(encoding="utf-8")).get("summary", "")
            return
        except Exception:
            pass
    try:
        r = requests.get(row["url"], headers={"User-Agent": UA}, timeout=timeout)
        r.encoding = "utf-8"
        m = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', r.text)
        desc = html.unescape(m.group(1)).strip() if m else ""
        # 太短的 description 没有信息量：Discourse 的 meta description 有时取的是
        # **某条回复**（实测 Goodnotes 那条抓到「领不了吗」4 个字），喂给简报只会误导。
        # 20 字以下一律当没有。
        if len(desc) < 20:
            desc = ""
        row["summary"] = desc[:800]
        cp.write_text(json.dumps({"summary": row["summary"]}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return
    time.sleep(0.25)


# ------------------------------------------------------------------ 去重 / 台账
# 分类/打分/置信度/标题归一全部来自 wool_policy（见文件头「题材口径」注释）。


# 论坛发帖台账（baipiao-daily skill）：发过的福利不再上推荐表（方案二互通）。
# src_url 是新字段（2026-10-24 起 draft.py 骨架带溯源注释、forum_api 记账时提取）；
# 老条目没有 src_url，用归一化标题兜底。
POSTED_LEDGER = Path("E:/AI/Zcode/.agents/skills/baipiao-daily/data/posted.jsonl")


def load_posted_marks() -> tuple[set, set]:
    src_urls, titles = set(), set()
    try:
        for line in POSTED_LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("src_url"):
                src_urls.add(e["src_url"])
            t = policy.norm_title(e.get("title") or "")
            if t:
                titles.add(t)
    except FileNotFoundError:
        pass  # 台账不存在就当没发过
    return src_urls, titles


def load_seen() -> dict:
    try:
        return json.loads(SEEN.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_seen(seen: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = SEEN.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, SEEN)


# 每轮推送的条目快照：{date, generated_at, urls:[...]}。
# 用来回答「哪些是**本次推送**新出现的」——注意这和「今日新线索」是两个口径：
#   · 今日新线索：今天第一次进榜（按天，跨轮不变）
#   · 本次新增   ：本轮推送才有、上一轮没有（按轮，每 3 小时刷新一次）
# 用户要的是后者：每天推 3 次，得能看出这次比上次多了什么。
ROUND_SNAPSHOT = DATA / "last-round.json"


def load_round_snapshot() -> set[str]:
    """上一轮推送的 URL 集合。没有快照（首次跑/搬机器）返回空集，
    此时把全部条目当「本轮新增」——宁可标多，也别漏标成旧的。"""
    try:
        d = json.loads(ROUND_SNAPSHOT.read_text(encoding="utf-8"))
        return set(d.get("urls") or [])
    except Exception:
        return set()


def save_round_snapshot(day: str, generated: datetime, items: list[dict]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    payload = {"date": day, "generated_at": generated.isoformat(),
               "urls": [it["url"] for it in items]}
    tmp = ROUND_SNAPSHOT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, ROUND_SNAPSHOT)


# ------------------------------------------------------------------ HTML
def esc(x: str) -> str:
    return html.escape(x or "")


# 标签 → (显示文案, 样式类)。good=加分项，warn=门槛，bad=风险。
# 这些标签来自 FLAG_RULES 的实际命中，直接告诉读者「这条有什么坑」——
# 比让读者自己从摘要里找要清楚得多。
FLAG_LABELS = {
    "free": ("免费/可领", "good"),
    "permanent": ("永久/长期", "good"),
    "entry": ("入口清晰", "good"),
    "official": ("官方域名", "good"),
    "region": ("限地区", "warn"),
    "bank": ("限银行卡", "warn"),
    "carrier": ("限运营商", "warn"),
    "paid": ("付费前置", "warn"),
    "threshold": ("有满减门槛", "warn"),
    "aff": ("含推广/邀请", "bad"),
    "lottery": ("概率性", "bad"),
}

# 分数构成（+25 免费可领…）与硬排除原因都在 wool_policy.py——唯一出处。

GROUP_ICONS = {"今日优先": "🔥", "生活羊毛": "🧧", "云资源": "☁️", "域名建站": "🌐",
               "服务器 VPS": "🖥️", "AI 额度": "✨", "其他": "📦"}

CONF_CLASS = {"确定": "ok", "需满足条件": "warn", "概率性": "bad"}

CSS = """
:root{
  color-scheme:light;
  --bg:#f6f7fa; --surface:#fff; --text:#1c2736; --muted:#516079; --hint:#69788c;
  --line:#e3e9f0; --primary:#4e52c9; --soft:#eff0ff; --hover:#f6f7fb;
  --shadow:0 10px 32px rgba(25,45,80,.06); --radius:16px;
  --good-bg:#e9f9ef; --good-fg:#177245; --good-bd:#bfe8cf;
  --warn-bg:#fff6e6; --warn-fg:#95610b; --warn-bd:#f3d9a4;
  --bad-bg:#fdeeee;  --bad-fg:#a32020;  --bad-bd:#f3c4c4;
}
html[data-theme=dark]{
  color-scheme:dark;
  --bg:#111827; --surface:#1b2636; --text:#f3f5f9; --muted:#b8c4d5; --hint:#9cacc0;
  --line:#354256; --primary:#b2b5ff; --soft:#303457; --hover:#263448;
  --shadow:0 10px 32px rgba(0,0,0,.12);
  --good-bg:#183e34; --good-fg:#a7e9c8; --good-bd:#30684f;
  --warn-bg:#4a3820; --warn-fg:#f6d492; --warn-bd:#775a2a;
  --bad-bg:#49242a;  --bad-fg:#f7b4b4;  --bad-bd:#7a3a3a;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;background:var(--bg);color:var(--text);
  font:15px/1.65 "PingFang SC","Microsoft YaHei","Noto Sans CJK SC",system-ui,sans-serif;
  -webkit-text-size-adjust:100%;overflow-x:hidden;
}
.shell{max-width:1040px;margin:0 auto;padding:0 18px 64px}
a{color:var(--primary);text-decoration:none}
a:hover{text-decoration:underline}

/* 顶栏 */
.top{
  position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:space-between;
  gap:12px;margin:0 -18px;padding:11px 18px;background:color-mix(in srgb,var(--bg) 82%,transparent);
  backdrop-filter:blur(12px);border-bottom:1px solid var(--line);
}
.brand{display:inline-flex;align-items:center;gap:8px;color:var(--text);font-size:15px}
.brand .logo{display:inline-grid;place-items:center;width:30px;height:30px;border-radius:9px;
  background:linear-gradient(180deg,var(--primary),color-mix(in srgb,var(--primary) 70%,#000));
  color:#fff;font-size:15px}
.top-actions{display:flex;align-items:center;gap:10px}
.top-date{color:var(--hint);font-size:13px}
.mode{min-height:36px;padding:0 12px;border:1px solid var(--line);border-radius:10px;
  background:var(--surface);color:var(--muted);font-size:13px;cursor:pointer}
.mode:hover{background:var(--hover)}

/* Hero + 统计带 */
.hero{margin:28px 0 14px}
.eyebrow{color:var(--primary);font-size:12.5px;font-weight:700;letter-spacing:.06em}
.hero h1{font-size:clamp(1.55rem,3vw,2.05rem);line-height:1.2;letter-spacing:-.02em;margin:6px 0 8px}
.hero p{margin:0;color:var(--muted);font-size:13.5px;max-width:680px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:16px 0 20px}
.stat{border:1px solid var(--line);border-radius:14px;background:var(--surface);
  padding:12px 14px;box-shadow:0 3px 14px rgba(25,45,80,.035)}
.stat strong{display:block;font-size:26px;line-height:1.15;letter-spacing:-.02em}
.stat span{display:block;color:var(--hint);font-size:12px;margin-top:2px}
.stat.hl strong{color:var(--primary)}
/* 「本次新增」格：绿色，和「条可写」的靛蓝区分开，一眼找到这次的新货 */
.stat.hot strong{color:var(--good-fg)}
.hl-num{color:var(--good-fg)}
@media (max-width:640px){.stats{grid-template-columns:repeat(2,1fr)}}

/* 筛选面板 */
.panel{border:1px solid var(--line);border-radius:18px;background:var(--surface);
  box-shadow:var(--shadow);padding:13px 15px;margin-bottom:22px}
.search{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:12px;
  background:var(--bg);padding:0 12px;min-height:42px}
.search span{color:var(--hint);font-size:16px}
.search input{flex:1;border:0;background:transparent;color:var(--text);font-size:14px;outline:none;min-width:0}
.filters{display:flex;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap}
.fname{color:var(--hint);font-size:12.5px;flex:none;width:34px}
.chips{display:flex;gap:7px;flex-wrap:wrap;flex:1;min-width:0}
.chip{min-height:32px;padding:0 11px;border:1px solid var(--line);border-radius:9px;
  background:var(--surface);color:var(--muted);font-size:12.5px;cursor:pointer;white-space:nowrap}
.chip:hover{background:var(--hover)}
.chip.active{background:var(--primary);border-color:var(--primary);color:#fff}
html[data-theme=dark] .chip.active{color:#171d3a}
.count{color:var(--hint);font-size:12.5px;margin-left:auto;white-space:nowrap}

/* 分组 */
.group{margin:26px 0}
.group[hidden]{display:none}
.group-head{display:flex;align-items:center;gap:10px;margin-bottom:12px;
  padding-left:10px;border-left:4px solid var(--primary)}
.group-head h2{margin:0;font-size:16.5px;font-weight:800;color:var(--text);white-space:nowrap}
.group-head .line{flex:1;height:1px;background:var(--line)}
.group-head .portion{color:var(--hint);font-size:12.5px;white-space:nowrap}
/* 关键：不设 align-items:start，同行卡片等高；卡内 .card-foot{margin-top:auto} 才能把
   底部信息钉在同一条线上 —— 之前的「参差不齐」就是 start + margin-top:auto 互相抵消。 */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(318px,1fr));gap:14px}
@media (max-width:640px){.cards{grid-template-columns:1fr}}

/* 卡片：结构固定五层，等高对齐 */
.card{display:flex;flex-direction:column;gap:8px;padding:14px 16px;border:1px solid var(--line);
  border-radius:var(--radius);background:var(--surface);box-shadow:0 3px 16px rgba(25,45,80,.035);
  transition:border-color .14s,box-shadow .14s;min-width:0}
.card:hover{border-color:color-mix(in srgb,var(--primary) 45%,var(--line));
  box-shadow:0 8px 24px rgba(25,45,80,.07)}
.card[hidden]{display:none}
/* 「本次新增」的卡片左边描一道绿线：滚动长列表时不用逐张看标签 */
.card.is-new{box-shadow:inset 3px 0 0 var(--good-fg),0 3px 16px rgba(25,45,80,.035)}
.pick-card{background:linear-gradient(180deg,var(--soft),var(--surface) 52%)}
.card-top{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
/* 帖子编号（点击复制）：一帖一号，由来源 URL 哈希得到，跨天不变 */
.card-no{font:inherit;font-size:11.5px;font-weight:800;line-height:1.5;cursor:pointer;
  padding:1px 7px;border-radius:7px;white-space:nowrap;letter-spacing:.02em;
  font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
  color:var(--primary);background:var(--soft);
  border:1px solid color-mix(in srgb,var(--primary) 32%,var(--line));
  transition:opacity .14s,background .14s,color .14s}
.card-no:hover{opacity:.68}
.card-no:focus-visible{outline:2px solid var(--primary);outline-offset:1px}
.card-no.copied{background:var(--good-bg);color:var(--good-fg);border-color:var(--good-bd)}
.cat-badge{font-size:11.5px;font-weight:700;color:var(--muted);background:var(--hover);
  border:1px solid var(--line);border-radius:7px;padding:1px 8px;white-space:nowrap}
.pick-card .cat-badge{color:var(--primary);background:var(--soft);
  border-color:color-mix(in srgb,var(--primary) 30%,var(--line))}
.conf{font-size:11px;padding:1px 7px;border-radius:7px;white-space:nowrap;border:1px solid transparent}
.conf.ok{background:var(--good-bg);color:var(--good-fg);border-color:var(--good-bd)}
.conf.warn{background:var(--warn-bg);color:var(--warn-fg);border-color:var(--warn-bd)}
.conf.bad{background:var(--bad-bg);color:var(--bad-fg);border-color:var(--bad-bd)}
.score{margin-left:auto;flex:none;min-width:34px;text-align:center;padding:1px 8px;border-radius:8px;
  background:var(--soft);color:var(--primary);font-size:12.5px;font-weight:800}
.pick-card .score{background:var(--primary);color:#fff}
html[data-theme=dark] .pick-card .score{color:#171d3a}
.card .title{margin:0;font-size:15px;line-height:1.45;font-weight:700}
.card .title a{color:var(--text)}
.card .title a:hover{color:var(--primary)}
.tags{display:flex;flex-wrap:wrap;gap:5px}
.tag{font-size:11.5px;line-height:1.5;padding:2px 8px;border-radius:7px;white-space:nowrap;
  border:1px solid var(--line);background:var(--hover);color:var(--muted)}
.tag.good{background:var(--good-bg);color:var(--good-fg);border-color:var(--good-bd)}
.tag.warn{background:var(--warn-bg);color:var(--warn-fg);border-color:var(--warn-bd)}
.tag.bad{background:var(--bad-bg);color:var(--bad-fg);border-color:var(--bad-bd)}
.tag.new{background:var(--soft);color:var(--primary);
  border-color:color-mix(in srgb,var(--primary) 35%,var(--line));font-weight:700}
/* 「今日首见」介于醒目与普通之间：今天第一次见，但上一轮已在榜 */
.tag.new-soft{background:color-mix(in srgb,var(--soft) 55%,var(--surface));
  color:color-mix(in srgb,var(--primary) 70%,var(--muted));
  border-color:color-mix(in srgb,var(--primary) 20%,var(--line));font-weight:600}
.tag.keep{background:var(--hover);color:var(--muted);font-weight:600}
/* 摘要最多 3 行，超长截断：卡片高度不再被超长正文撑爆 */
.desc{margin:0;color:var(--muted);font-size:12.8px;line-height:1.6;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
/* 分数构成：+25 免费可领 / −12 限地区，一眼看懂分数怎么来的 */
.why{display:flex;flex-wrap:wrap;gap:4px}
.why i{font-style:normal;font-size:11px;line-height:1.55;padding:1px 7px;border-radius:6px;
  font-variant-numeric:tabular-nums;white-space:nowrap}
.why i.p{background:var(--good-bg);color:var(--good-fg)}
.why i.n{background:var(--bad-bg);color:var(--bad-fg)}
.card-foot{margin-top:auto;padding-top:10px;border-top:1px solid var(--line);
  display:flex;align-items:center;gap:6px 10px;flex-wrap:wrap}
.source{color:var(--primary);font-size:12px;font-weight:700}
.time{color:var(--hint);font-size:12px}
.go{margin-left:auto;padding:4px 10px;border-radius:8px;background:var(--primary);color:#fff;
  font-size:12.5px;white-space:nowrap}
html[data-theme=dark] .go{color:#171d3a}
.go:hover{text-decoration:none;filter:brightness(1.06)}
/* 明文 URL 收进 foot 第二行，单行截断（title 属性带完整链接可复制） */
.urlline{flex-basis:100%;color:var(--hint);font:11px/1.5 ui-monospace,Consolas,monospace;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:.85}

/* 说明 / 排除区 / 页脚 / 浮动按钮 */
.hint{margin:26px 0 0;border:1px solid var(--line);border-radius:12px;background:var(--surface);
  padding:10px 14px;color:var(--muted);font-size:12.5px}
.hint summary{cursor:pointer;color:var(--primary);font-weight:600}
.hint p{margin:7px 0}
.exc-g{margin:10px 0 4px}
.exc-g h4{margin:0 0 6px;font-size:12.5px;color:var(--text)}
.exc-g h4 b{color:var(--hint);font-weight:600}
.exc-g h4 small{color:var(--hint);font-weight:400;margin-left:8px}
.exc-g ul{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:2px 16px}
.exc-g li{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.exc-g li a{color:var(--muted)}
.exc-g li a:hover{color:var(--primary)}
.exc-g li.more{color:var(--hint);font-style:normal}
.errbox{background:var(--bad-bg);border:1px solid var(--bad-bd);color:var(--bad-fg);
  border-radius:12px;padding:10px 13px;font-size:12.5px;margin:0 0 16px}
.footer{margin-top:26px;padding-top:14px;border-top:1px solid var(--line);
  color:var(--hint);font-size:11.5px;line-height:1.7}
.fab{position:fixed;right:18px;bottom:18px;width:42px;height:42px;border-radius:50%;
  border:1px solid var(--line);background:var(--surface);color:var(--muted);font-size:17px;
  cursor:pointer;box-shadow:0 6px 18px rgba(25,45,80,.16);z-index:30}
.fab[hidden]{display:none}
"""

JS = """
(() => {
  const root = document.documentElement;
  const tbtn = document.getElementById('theme');
  // file:// 下部分浏览器会禁用 localStorage（opaque origin），直接访问会抛异常并
  // 让后面的筛选/排序脚本一起挂掉。所以统一包一层容错，读不到就当没保存过。
  const store = {
    get() { try { return localStorage.getItem('wool-theme'); } catch (e) { return null; } },
    set(v) { try { localStorage.setItem('wool-theme', v); } catch (e) {} }
  };
  const saved = store.get();
  const apply = d => {
    root.dataset.theme = d ? 'dark' : 'light';
    tbtn.textContent = d ? '☀ 日间' : '☾ 夜间';
  };
  apply(saved ? saved === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches);
  tbtn.addEventListener('click', () => {
    const d = root.dataset.theme !== 'dark';
    apply(d); store.set(d ? 'dark' : 'light');
  });

  const cards = [...document.querySelectorAll('.card')];
  const groups = [...document.querySelectorAll('.group')];
  const q = document.getElementById('q');
  const countEl = document.getElementById('count');
  const cats = [...document.querySelectorAll('.chip[data-cat]')];
  const sorts = [...document.querySelectorAll('.chip[data-sort]')];
  let cat = 'all', sort = 'score', onlyNew = false;

  const refresh = () => {
    const term = q.value.trim().toLowerCase();
    cards.forEach(c => {
      const okCat = cat === 'all' || c.dataset.cat === cat;
      const okText = !term || c.dataset.text.includes(term);
      const okNew = !onlyNew || c.dataset.new === '1';
      c.hidden = !(okCat && okText && okNew);
    });
    groups.forEach(g => {
      g.hidden = ![...g.querySelectorAll('.card')].some(c => !c.hidden);
    });
    countEl.textContent = `显示 ${cards.filter(c => !c.hidden).length} / ${cards.length} 条`;
  };
  q.addEventListener('input', refresh);
  cats.forEach(ch => ch.addEventListener('click', () => {
    cats.forEach(x => x.classList.toggle('active', x === ch));
    cat = ch.dataset.cat; refresh();
  }));
  // 「只看本次新增」：和类别/搜索叠加，不是互斥
  const onlyNewBtn = document.getElementById('only-new');
  if (onlyNewBtn) onlyNewBtn.addEventListener('click', () => {
    onlyNew = !onlyNew;
    onlyNewBtn.classList.toggle('active', onlyNew);
    refresh();
  });
  sorts.forEach(ch => ch.addEventListener('click', () => {
    sorts.forEach(x => x.classList.toggle('active', x === ch));
    sort = ch.dataset.sort;
    document.querySelectorAll('.cards').forEach(box => {
      [...box.children].sort((a, b) => sort === 'time'
        ? String(b.dataset.ts || '').localeCompare(String(a.dataset.ts || ''))
        : (Number(b.dataset.score) || 0) - (Number(a.dataset.score) || 0)
      ).forEach(n => box.appendChild(n));
    });
  }));

  // —— 编号复制：点卡片左上角的数字，复制「羊毛日报 日期 #编号 标题 + 链接」 ——
  // navigator.clipboard 在 file:// 下可能被禁用（需安全上下文/用户手势），失败时
  // 自动退回临时 textarea + execCommand；两条路都不通才提示，绝不弹阻塞对话框。
  const legacyCopy = txt => new Promise((ok, bad) => {
    const ta = document.createElement('textarea');
    ta.value = txt;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:-1000px;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, txt.length);
    try { document.execCommand('copy') ? ok() : bad(new Error('copy failed')); }
    catch (err) { bad(err); }
    finally { ta.remove(); }
  });
  const copyText = txt => {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(txt).catch(() => legacyCopy(txt));
    }
    return legacyCopy(txt);
  };
  const issueDay = document.body.dataset.day || '';
  document.querySelectorAll('.card-no').forEach(btn => {
    btn.addEventListener('click', e => {
      e.preventDefault(); e.stopPropagation();
      const card = btn.closest('.card');
      const pid = btn.dataset.pid || '';
      const titleEl = card.querySelector('.title');
      const title = titleEl ? titleEl.textContent.trim() : '';
      const link = card.querySelector('.title a');
      const txt = `羊毛日报 ${issueDay} #${pid} ${title}\n${link ? link.href : ''}`;
      copyText(txt).then(() => {
        const old = btn.textContent;
        btn.textContent = '✓';
        btn.classList.add('copied');
        btn.title = '已复制：' + txt;
        setTimeout(() => { btn.textContent = old; btn.classList.remove('copied'); }, 1400);
      }).catch(() => {
        btn.title = '复制失败，请手动选中复制：' + txt;
      });
    });
  });

  const fab = document.getElementById('fab');
  fab.addEventListener('click', () => scrollTo({ top: 0, behavior: 'smooth' }));
  addEventListener('scroll', () => { fab.hidden = scrollY < 500; }, { passive: true });
})();
"""


# ---------------------------------------------------------------- 帖子编号
# 编号要「一帖一号、永不变」：用来源 URL 的 sha1 取前 6 位当 ID。
# 不能用 Python 内置 hash()——它每个进程随机加盐，同一 URL 每次生成都是新号。
# 字母表沿用 Crockford base32：去掉 0/1/I/L/O/U，避免和数字、彼此看混。
ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def short_id(key: str, length: int = 6) -> str:
    """同一 key（URL / slug）永远得到同一个短 ID。"""
    n = int.from_bytes(hashlib.sha1(key.strip().encode("utf-8")).digest()[:8], "big")
    out = []
    for _ in range(length):
        out.append(ID_ALPHABET[n & 31])
        n >>= 5
    return "".join(out)


def assign_post_ids(items: list[dict]) -> None:
    """给每条写上 _pid。同页万一撞号就加长一位（SHA1 前 6 位撞号概率极低，
    但真撞了必须拆开，否则「告诉 AI 写哪个」就指不准）。"""
    used: set[str] = set()
    for it in items:
        length, pid = 6, ""
        while True:
            pid = short_id(it["url"], length)
            if pid not in used:
                break
            length += 1
        used.add(pid)
        it["_pid"] = pid


def chips_html(it: dict) -> str:
    out = []
    # 两个口径的标记（2026-10-24 用户要求「标注出哪些是本次推送的」）：
    #   本次新增 = 上一轮推送没有、这一轮才有（亮蓝，最醒目）
    #   今日首见 = 今天第一次进榜，但上一轮已经在榜（灰）
    # 每天推 3 次，读者最关心的是「这次比上次多了什么」，所以「本次新增」排第一。
    if it.get("is_new_round"):
        out.append('<span class="tag new">本次新增</span>')
    elif it.get("is_today_new"):
        out.append('<span class="tag new-soft">今日首见</span>')
    else:
        out.append('<span class="tag keep">在榜续留</span>')
    if it.get("age_label") == "今日":
        out.append('<span class="tag good">帖子今日发布</span>')
    for f in it.get("flags") or []:
        label, kind = FLAG_LABELS.get(f, ("", ""))
        if label:
            out.append(f'<span class="tag {kind}">{esc(label)}</span>')
    return "".join(out)


def reasons_html(it: dict) -> str:
    """分数构成：+25 免费可领 / −12 限地区。reasons 里以「-」开头的是减分项。"""
    parts = []
    for r in it.get("reasons") or []:
        pts = policy.REASON_POINTS.get(r)
        if r.startswith("-"):
            label = r.lstrip("-")
            parts.append(f'<i class="n">−{abs(pts) if pts else "•"} {esc(label)}</i>'
                         if pts else f'<i class="n">{esc(label)}</i>')
        elif pts:
            parts.append(f'<i class="p">+{pts} {esc(r)}</i>')
    return f'<div class="why">{"".join(parts)}</div>' if parts else ""


def card_html(it: dict, pick: bool = False) -> str:
    """一张卡片，结构固定五层（等高对齐的前提）：

    顶行(类目+确定性+分数) / 标题 / 标签 / 摘要(3行截断) / 底部信息(来源·时间·按钮+明文URL)。
    明文 URL 是用户明确要的：手机上按钮可点，URL 可复制，两边都不丢。
    """
    summary = (it.get("summary") or "").strip()
    url = it["url"]
    short = url if len(url) <= 64 else url[:61] + "…"
    text = f"{it['title']} {summary} {it['category']} {it['source_label']} " \
           f"{' '.join(it.get('reasons') or [])}".lower()
    tags = chips_html(it)
    icon = GROUP_ICONS.get(it["category"], "📦")
    conf_cls = CONF_CLASS.get(it["confidence"], "")
    # 编号由 assign_post_ids 统一分配（URL 哈希），点一下就把
    # 「日期 + 编号 + 标题 + 链接」整条复制走，直接粘给 AI 让它写这条。
    pid = it.get("_pid") or ""
    pid_html = (f'<button class="card-no" type="button" data-pid="{esc(pid)}" '
                f'title="点击复制帖子编号、标题和链接">#{esc(pid)}</button>') if pid else ""
    return f"""
      <article class="card{' pick-card' if pick else ''}{' is-new' if it.get('is_new_round') else ''}"
               data-pid="{esc(pid)}" data-cat="{esc(it['category'])}" data-score="{it['score']}"
               data-new="{1 if it.get('is_new_round') else 0}"
               data-ts="{esc(it.get('published') or '')}" data-text="{esc(text)}">
        <div class="card-top">
          {pid_html}<span class="cat-badge">{icon} {esc(it['category'])}</span>
          <span class="conf {conf_cls}">{esc(it['confidence'])}</span>
          <span class="score">{it['score']}</span>
        </div>
        <h3 class="title"><a href="{esc(url)}" target="_blank" rel="noopener">{esc(it['title'])}</a></h3>
        {f'<div class="tags">{tags}</div>' if tags else ''}
        {f'<p class="desc">{esc(summary)}</p>' if summary else ''}
        {reasons_html(it)}
        <div class="card-foot">
          <span class="source">{esc(it['source_label'])}</span>
          <span class="time">{esc(it['age_label'])}</span>
          <a class="go" href="{esc(url)}" target="_blank" rel="noopener">打开原帖 →</a>
          <span class="urlline" title="{esc(url)}">{esc(short)}</span>
        </div>
      </article>"""


def pick_diverse(rows: list[dict], limit: int = 8, per_source: int = 3) -> list[dict]:
    """今日优先的多样性约束：同一来源最多 per_source 条。

    不限的话，一个限免 TG 频道能一次占满整个「今日优先」（实测 2026-10-26：
    TG·折腾App探索 的 iOS 内购限免占了前 11 条里的 6 条，AI 额度/云资源全被挤下去）。
    被挤掉的条目不会消失，仍在下面按类目分组里，只是不占「优先」位。
    """
    out: list[dict] = []
    seen: dict[str, int] = {}
    for r in rows:
        src = r.get("source") or ""
        if seen.get(src, 0) >= per_source:
            continue
        out.append(r)
        seen[src] = seen.get(src, 0) + 1
        if len(out) >= limit:
            break
    return out


def build_html(items: list[dict], excluded: list[dict], day: str, days: int,
               generated: datetime, fetch_errors: list[str] | None = None,
               totals: dict | None = None) -> str:
    errs = FETCH_ERRORS if fetch_errors is None else fetch_errors
    # 一帖一号：编号由来源 URL 决定，同一帖今天、明天、下周都是同一个号。
    assign_post_ids(items)
    errbox = (
        f'<div class="errbox">⚠️ 本期有来源抓取失败：<b>{esc("、".join(errs))}</b>，'
        f'表中可能缺该来源的内容，下一轮会自动重试。</div>' if errs else ""
    )
    totals = totals or {}
    n_uniq = totals.get("uniq", len(items) + len(excluded))
    # 「本次新增」= 上一轮推送没有、这一轮才出现的。用户要的就是这个数字：
    # 每天推 3 次，得能一眼看出这次比上次多了什么。
    n_new_round = sum(1 for i in items if i.get("is_new_round"))
    n_keep = len(items) - n_new_round
    if n_new_round:
        new_round_note = (f'<b class="hl-num">本次新增 {n_new_round} 条</b>'
                          f'（另有 {n_keep} 条在榜续留，标「在榜续留」）。')
    else:
        new_round_note = '<b class="hl-num">本轮无新增</b>（各源暂无新货，以下均为在榜续留）。'
    n_sources = totals.get("sources_ok", len(fs.SOURCES) - len(errs))

    # 今日优先：够 55 分取前 8；不足 6 条时用分数 Top 8 兜底，但**口径要如实写**——
    # 之前的 bug 是标签写「分数 ≥55」实际塞了 20 分的凑数卡。
    hi = [i for i in items if i["score"] >= 55]
    top = pick_diverse(hi)
    if len(top) < 6:
        top = pick_diverse(items)
        pick_label = f"分数 Top {len(top)}（≥55 分的只有 {len(hi)} 条）"
    else:
        pick_label = "分数 ≥55"
    pick_label += " · 同源最多 3 条"
    # 空态说明（2026-10-24「往期未选」口径后会出现真·空表）：告诉读者为什么空、
    # 下一轮什么时候有新货，别让人以为坏了。
    empty_note = ""
    if not items:
        empty_note = ('<div class="errbox" style="background:var(--soft);border-color:'
                      'color-mix(in srgb,var(--primary) 30%,var(--line));color:var(--muted)">'
                      '📭 本轮没有新候选——今日之前展示过的都发过或沉底（见排除区「往期未选」），'
                      f'排除区可核对。新线索要等各源更新后下一轮生成（每日 9/15/21 点，'
                      f'本轮更新于 {generated:%H:%M}）。</div>')
    top_urls = {i["url"] for i in top}

    # 「服务器 VPS」已整体排除（2026-10-24 口径），不进分组与筛选
    cat_order = ["生活羊毛", "云资源", "域名建站", "AI 额度", "其他"]
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it["category"], []).append(it)

    # 类别筛选 chips（带条数）
    cat_chips = ['<button class="chip active" type="button" data-cat="all">全部</button>']
    for cat in cat_order:
        n = len(groups.get(cat) or [])
        if n:
            cat_chips.append(
                f'<button class="chip" type="button" data-cat="{esc(cat)}">{esc(cat)} {n}</button>')

    sections = []
    for cat in cat_order:
        rows = groups.get(cat)
        if not rows:
            continue
        rest = [r for r in rows if r["url"] not in top_urls]
        if not rest:
            continue                      # 整类都在优先区，不重复开一节
        hidden = len(rows) - len(rest)
        note = f" · 另有 {hidden} 条在「今日优先」" if hidden else ""
        icon = GROUP_ICONS.get(cat, "📦")
        sections.append(f"""
    <section class="group" data-key="{esc(cat)}">
      <div class="group-head"><h2>{icon} {esc(cat)}</h2><div class="line"></div>
        <span class="portion">{len(rest)} 条{note}</span></div>
      <div class="cards">{''.join(card_html(r) for r in rest)}</div>
    </section>""")

    # 排除项按原因分组，每条一行带链接（可点开核对是否误杀）——
    # 之前是一坨 run-on 文字，标题截 26 字拼一起，既读不了也点不了。
    # 「往期未选」是日积月累的大类：条目带沉底日期（excluded 记 why_day），
    # 分组头注明最旧的沉底日，帮助判断这堆还能不能翻。
    exc_groups: dict[str, list[dict]] = {}
    for x in excluded:
        exc_groups.setdefault(x.get("why") or "其他", []).append(x)
    exc_parts = []
    for k, v in sorted(exc_groups.items(), key=lambda kv: -len(kv[1])):
        shown = v[:12]
        lis = [f'<li><a href="{esc(x["url"])}" target="_blank" rel="noopener">'
               f'{esc(x["title"])}</a></li>' for x in shown]
        if len(v) > 12:
            lis.append(f'<li class="more">…还有 {len(v) - 12} 条，完整清单见 '
                       f'data/{day}.json 的 excluded 段</li>')
        note = ""
        if k == "往期未选":
            oldest = min((x.get("why_day") or day) for x in v)
            note = (f" · 今日之前展示过、未发帖（最早沉到 {oldest}）。"
                    f"今天的候选仍在榜上")
        exc_parts.append(
            f'<div class="exc-g"><h4>{esc(k)} <b>（{len(v)}）</b>'
            f'{f"<small>{esc(note)}</small>" if note else ""}</h4>'
            f'<ul>{"".join(lis)}</ul></div>')
    exc_html = "".join(exc_parts) or "<p>本期没有排除项。</p>"

    return f"""<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f6f7fa">
<title>羊毛日报 · {day}</title>
<style>{CSS}</style>
</head>
<body data-day="{day}">
<div class="shell">
  <header class="top">
    <a class="brand" href="#top"><span class="logo">🐑</span><strong>羊毛日报</strong></a>
    <div class="top-actions">
      <span class="top-date">{day}</span>
      <button class="mode" id="theme" type="button">☾ 夜间</button>
    </div>
  </header>

  <div class="hero" id="top">
    <div class="eyebrow">{n_sources} 源聚合 · 更新于 {generated:%Y-%m-%d %H:%M}</div>
    <h1>今天的羊毛，挑最值得写的。</h1>
    <p>{new_round_note}只做推荐，不代领。黄色标签是<b>有门槛</b>（限地区/银行卡/号段/满减），红色是<b>有风险</b>（含推广/概率性）；卡片上的绿/红小徽章是<b>分数构成</b>。</p>
  </div>

  <div class="stats">
    <div class="stat hl"><strong>{len(items)}</strong><span>条可写</span></div>
    <div class="stat hot"><strong>{n_new_round}</strong><span>本次新增</span></div>
    <div class="stat"><strong>{len(excluded)}</strong><span>条已排除</span></div>
    <div class="stat"><strong>{n_uniq}</strong><span>条线索（去重后）</span></div>
    <div class="stat"><strong>{n_sources}/{len(fs.SOURCES)}</strong><span>来源在线</span></div>
  </div>

  {errbox}
  {empty_note}

  <section class="panel" aria-label="搜索与筛选">
    <label class="search"><span aria-hidden="true">⌕</span>
      <input id="q" type="search" placeholder="搜索标题 / 摘要 / 标签（如：限地区）…" autocomplete="off">
    </label>
    <div class="filters"><span class="fname">类别</span>
      <div class="chips">{''.join(cat_chips)}</div></div>
    <div class="filters"><span class="fname">排序</span>
      <div class="chips">
        <button class="chip active" type="button" data-sort="score">分数优先</button>
        <button class="chip" type="button" data-sort="time">最新发布</button>
        <button class="chip" type="button" id="only-new">只看本次新增 {n_new_round}</button>
      </div>
      <span class="count" id="count">显示 {len(items)} / {len(items)} 条</span>
    </div>
  </section>

  <section class="group" id="pick" data-key="今日优先">
    <div class="group-head"><h2>🔥 今日优先</h2><div class="line"></div>
      <span class="portion">{len(top)} 条 · {pick_label}</span></div>
    <div class="cards">{''.join(card_html(i, pick=True) for i in top)}</div>
  </section>

  {''.join(sections)}

  <details class="hint">
    <summary>说明 · 分数怎么来的 · 排除规则</summary>
    <p>· <b>加分</b>：免费/可领 +25、入口清晰 +15、今日新增 +20、昨日 +12、永久/长期 +10、说明详细 +10（较短 +5）、官方域名 +8、生活类 +5。</p>
    <p>· <b>减分</b>：含推广 −25、付费前置 −15、限地区 −12、偏教程 −10、满减门槛 −8、限银行卡/运营商 −6。分数只用于排序，不代表官方口径。</p>
    <p>· <b>确定性</b>：确定 / 需满足条件（限地区·银行卡·号段·付费）/ 概率性。</p>
    <p>· <b>排除</b>：交易帖、抽奖、新闻、讨论、无发放动作、已结束。点开下面按原因分类的清单（可点标题）核对是否误杀。</p>
    <p>· <b>有效期</b>不做自动核验，写稿前务必点「打开原帖」确认库存与截止时间。</p>
  </details>

  <details class="hint">
    <summary>已排除 {len(excluded)} 条 · 点开核对是否误杀</summary>
    {exc_html}
  </details>

  <footer class="footer">
    来源：{esc("、".join(fs.SOURCE_LABELS[n] for n in fs.SOURCES))}<br>
    只聚合公开信息，不保证福利仍有效；请自行甄别，勿泄露账号与人脸等敏感信息。<br>
    本期生成于 {generated:%Y-%m-%d %H:%M:%S}（每日 9/15/21 点各更新一次，以页面时间为准）。
  </footer>
</div>
<button class="fab" id="fab" type="button" title="返回顶部" aria-label="返回顶部" hidden>↑</button>
<script>{JS}</script>
</body></html>"""


# ------------------------------------------------------------------ PDF
def to_pdf(html_path: Path, pdf_path: Path) -> bool:
    edge = "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
    if not os.path.exists(edge):
        print("[warn] Edge not found, skip PDF", file=sys.stderr)
        return False
    win_html = "E:/AI/Zcode/projects/wool-daily/out/" + html_path.name
    win_pdf = "E:/AI/Zcode/projects/wool-daily/out/" + pdf_path.name
    profile = "C:/Users/renyingpeng/AppData/Local/Temp/wool-edge"
    cmd = [edge, "--headless=new", "--disable-gpu", "--no-first-run",
           f"--user-data-dir={profile}", f"--print-to-pdf={win_pdf}",
           "--no-pdf-header-footer", f"file:///{win_html}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        return pdf_path.exists() and pdf_path.stat().st_size > 8000
    except Exception as exc:
        print(f"[warn] pdf: {exc!r}", file=sys.stderr)
        return False


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="抓取窗口（天），默认 3")
    ap.add_argument("--date", default="", help="输出日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--force-overwrite", action="store_true",
                    help="配合 --date 覆盖历史归档（默认另存为 <day>-rerun<时间>）")
    ap.add_argument("--top", type=int, default=12, help="stdout 摘要条数")
    args = ap.parse_args()

    now = datetime.now().astimezone()
    day = args.date or now.strftime("%Y-%m-%d")
    day_real = now.strftime("%Y-%m-%d")  # 无 --date 的真实今天（台账写入判据用）
    cutoff = now - timedelta(days=args.days)
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    rows = fetch_all()  # 全部源（skill 引擎；分源状态由引擎打到 stderr）
    # 没产出的源当抓取失败，进页面告警条（引擎对单源失败静默返回空）
    global FETCH_ERRORS
    got_sources = {r["source"] for r in rows}
    FETCH_ERRORS = [n for n in fs.SOURCES if n not in got_sources]
    print(f"[fetch] {len(got_sources)}/{len(fs.SOURCES)} 源有产出，共 {len(rows)} 行",
          file=sys.stderr)

    # 窗口过滤（没有时间的保留，按“近期”处理）
    rows = [r for r in rows if not r.get("published") or r["published"] >= cutoff]

    # 去重：URL 优先，其次标题归一
    seen_url, seen_title, uniq = set(), set(), []
    for r in rows:
        u, t = r["url"], policy.norm_title(r["title"])
        if u in seen_url or t in seen_title:
            continue
        seen_url.add(u)
        seen_title.add(t)
        uniq.append(r)
    rows = uniq
    n_uniq = len(uniq)

    # 富化 + 打分（排除顺序与 make_card 一致：已发帖 → 往期未选 → 硬排除 → 低分，见 wool_policy）
    # 「往期未选」口径 v2（2026-10-24 用户定）：**今日之前展示过的收起来，今日内
    # 展示过的继续留着**——粒度是「日」不是「轮」。早上 9 点看过的候选，下午 3 点
    # 还能看到（数据是新鲜的，只是不用重复推）；昨天及更早看过的才沉底。
    # 判据：台账记每条 first_seen_day（首次展示日期），首轮 = day 的进表，更早的排除。
    # 标记：item.is_today_new=True 供页面渲染「今日新增」徽章；排除归因「往期未选」
    # 的条目在排除区按原因分组，一眼分清新旧。
    posted_urls, posted_titles = load_posted_marks()
    seen_prev = load_seen()
    # 上一轮推送的 URL 集合（用来判断「本次推送新出现」）。
    # 台帐里 last_seen_round 记的是「上次出现在第几轮」，但更可靠的做法是直接读
    # 上一份推送快照——这样重启、补跑、跨天都不会错。见 save_round_snapshot()。
    prev_round_urls = load_round_snapshot()
    items, excluded = [], []
    for r in rows:
        enrich(r["source"], r)
        if r["url"] in posted_urls or policy.norm_title(r["title"]) in posted_titles:
            excluded.append({"title": r["title"], "url": r["url"], "why": "已发帖"})
            continue
        rec = seen_prev.get(r["url"])
        # 旧台账记录只有 first（首次日期）没有 first_seen_day——必须兜底读 first，
        # 否则 137 条历史记录会以「新条目」身份再展示一天才被回填沉底（实测踩过）。
        fsd = (rec or {}).get("first_seen_day") or (rec or {}).get("first")
        if fsd and fsd < day:
            # 只保留最近 7 天的沉底记录：一周没发的基本永远不会再发，
            # 无限累积只会让 JSON/排除区膨胀（2026-10-24 实测一天就 +80 条）
            if (now.replace(tzinfo=None) - datetime.strptime(fsd, "%Y-%m-%d")).days <= 7:
                excluded.append({"title": r["title"], "url": r["url"],
                                 "why": "往期未选", "why_day": fsd})
            continue
        cat, flags = policy.classify(r["title"], r.get("summary", ""), r.get("url", ""))
        hard_why = policy.hard_exclude(r["title"], cat, flags, r["source"])
        if hard_why:
            excluded.append({"title": r["title"], "url": r["url"], "why": hard_why})
            continue
        age_days, age_label = policy.age_label(r.get("published"), now)
        sc, why = policy.score_of(cat, flags, age_days, r.get("summary", ""))
        if sc < policy.LOW_SCORE or not why:
            # 归因固定写「低分」：原来拿 why[0] 当原因，而 why[0] 常是加分项
            # （「今日新增」「人人可用」），排除区就出现「今日新增（3）」这种
            # 看着像分类、其实是没达线的条目。
            excluded.append({"title": r["title"], "url": r["url"], "why": "低分"})
            continue
        items.append({
            "title": r["title"], "url": r["url"],
            "source": r["source"], "source_label": r["source_label"],
            "summary": r.get("summary", ""),
            "published": r["published"].isoformat() if r.get("published") else "",
            "age_label": age_label,
            "category": cat, "score": sc, "reasons": why,
            "confidence": policy.confidence_of(flags),
            "flags": [k for k, v in flags.items() if v],
            # 两个口径分开（2026-10-24 用户要求「标注出哪些是本次推送的」）：
            #   is_new_round  = 上一轮推送里没有、本轮才有 → 页面标「本次新增」（醒目）
            #   is_today_new  = 今天第一次进榜 → 页面标「今日首见」（次要）
            # 同一条可能 is_new_round=True 且 is_today_new=True（今天第一次出现）。
            "is_new_round": r["url"] not in prev_round_urls,
            "is_today_new": not (rec and rec.get("first_seen_day") == day),
        })

    # 排序：分数优先 → 本轮新增排同分在榜卡之前（用户定：新货优先看）
    #       → 帖子发布时间新的在前 → 标题
    items.sort(key=lambda x: (-x["score"],
                              not x.get("is_new_round", True),
                              not x.get("is_today_new", True),
                              x["age_label"] != "今日",
                              x["title"]))

    # 台账：按「日」记账。first_seen_day=首次展示日期（「往期未选」的判据，永不变），
    # shown=累计展示次数，last_day=最近展示日期。同一天内多轮展示不重复计次。
    # 旧字段兼容：老记录没有 first_seen_day 时用 first（老台账的首次日期）回填。
    # 防 --date 污染：手动跑历史日期（复盘）时跳过台账写入——否则 first_seen_day
    # 会被写成过去/未来的日期，破坏「往期未选」的判据。只有跑今天才记账。
    if args.date and args.date != day_real:
        print(f"[skip] --date {args.date} 非今天，跳过台账/快照写入"
              f"（避免污染 first_seen_day 与本轮基线）", file=sys.stderr)
    else:
        seen = load_seen()
        for it in items:
            rec = seen.get(it["url"]) or {}
            if not rec.get("first_seen_day"):
                rec["first_seen_day"] = rec.get("first") or day
            if rec.get("last_day") != day:
                rec["shown"] = int(rec.get("shown", 0)) + 1
            rec["last_day"] = day
            rec["first"] = rec["first_seen_day"]  # 兼容旧读者
            rec["last"] = day
            seen[it["url"]] = rec
        save_seen(seen)
        # 本轮快照：下一轮据此判断「哪些是本次新增」。必须在算完标记之后写，
        # 否则本轮自身的条目会被下一轮误判成旧的在榜卡。
        save_round_snapshot(day, now, items)

    payload = {
        "date": day,
        "generated_at": now.isoformat(),
        "window_days": args.days,
        "items": items,
        "excluded": excluded,
        "fetch_errors": FETCH_ERRORS,
    }
    # 防 --date 覆盖历史归档（2026-10-24 踩过两次）：
    # 手动跑 --date <过去日期> 复盘时，写出去的是**今天的实时抓取结果**，
    # 覆盖掉那天真实的归档后无法恢复（out/ 不在 git 里）。所以：
    #   · 目标文件已存在 + 不是今天 → 自动改名 <day>-rerun<时间>，保留原归档
    #   · 确实要覆盖原件时显式加 --force-overwrite
    target_day = day
    if args.date and args.date != day_real and (DATA / f"{day}.json").exists():
        stamp = now.strftime("%H%M%S")
        target_day = f"{day}-rerun{stamp}"
        print(f"[warn] --date {day} 的归档已存在，本轮结果另存为 {target_day}"
              f"（避免覆盖历史归档；要覆盖原件加 --force-overwrite）",
              file=sys.stderr)
        if args.force_overwrite:
            target_day = day
            print(f"[warn] --force-overwrite：覆盖 {day} 的历史归档", file=sys.stderr)
    (DATA / f"{target_day}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    page = build_html(items, excluded, day, args.days, now,
                      totals={"uniq": n_uniq, "sources_ok": len(fs.SOURCES) - len(FETCH_ERRORS)})
    html_path = OUT / f"{target_day}.html"
    html_path.write_text(page, encoding="utf-8")
    pdf_path = OUT / f"{target_day}.pdf"
    pdf_ok = False if args.no_pdf else to_pdf(html_path, pdf_path)

    # stdout：给 cron agent 的简报（不打印全文，避免吃满上下文）
    print(f"DATE={day}")
    print(f"ITEMS={len(items)} EXCLUDED={len(excluded)}")
    print(f"HTML=E:/AI/Zcode/projects/wool-daily/out/{target_day}.html")
    if pdf_ok:
        print(f"PDF=E:/AI/Zcode/projects/wool-daily/out/{target_day}.pdf")
    print("")
    print("TOP:")
    for it in items[: args.top]:
        print(f"- [{it['score']}] {it['title']} | {it['category']} | {it['age_label']} | {it['source_label']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
