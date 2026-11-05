#!/usr/bin/env python3
"""每日羊毛推荐表：抓 24-72h 内的福利线索，打分、分类、生成 HTML + PDF。

设计口径（2026-11-04 用户重定）：
  · **只做推荐表，不写单篇稿**——先让人一眼看出"本轮有哪些值得写"。
  · 题材：**只要科技**——AI 官方额度、云厂商活动、**VPS/服务器/主机**、域名建站、
    开源/自托管工具、软件与游戏限免。**购物类一律不要**（吃的/生活用品/服饰美妆/
    家电/购物券/影音会员/影视资源）。
  · **每轮只呈现本轮新增**：页面默认范围 = 本轮新增；补抓与在榜续留靠范围切换查看，
    15:00 那轮不会混进 9:00 的条目（判据见 mark_round）。
  · 硬排除：交易（收/出）、已结束、抽奖/概率性、aff/邀请码、梯子/机场、AI 中转站、
    非科技题材、影视资源。**VPS 不再排除**（forum 口径仍排除，见 wool_policy.PROFILE）。
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
# 全部源（内置 + sources.local.json 追加）、RSS/Atom/V2EX JSON/TG 预览页四套解析器、
# 代理回退（默认→直连→Clash）、
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

# 软件限免类源：本表**不要**（2026-11-04 用户定「不要软件类的羊毛」，
# 只要基础设施 + 自建服务）。这些源在发帖 skill 里仍是正经的「软件限免」类目，
# 所以只在推荐表侧跳过，不改 fetch_sources 的源清单。
SOFTWARE_SOURCES = {"apprcn", "appinn", "iplaysoft", "sspai", "dayanzai", "tg-tosstf"}
# 本表实际抓的源（内置 + sources.local.json 追加 − 软件类）。
# 所有「N/M 源在线」的计数都用它，别再直接 len(fs.SOURCES)。
ACTIVE_SOURCES = [n for n in fs.SOURCES if n not in SOFTWARE_SOURCES]


def fetch_all() -> list[dict]:
    """抓全部源，转成本文件的行格式（published 统一成 tz-aware datetime）。

    NodeSeek 的「WSL 直连被 reset」老问题由引擎的 Clash 回退解决
    （2026-10-24 实测 WSL 可直连 127.0.0.1:7897），win_fetch.py 那套不再需要。
    """
    # 软件限免源整体跳过（见文件上方 SOFTWARE_SOURCES 的说明）。注意 FETCH_ERRORS
    # 的回填也要用同一份名单，否则被跳过的源会被误报成「抓取失败」。
    raw = fs.fetch_rows(ACTIVE_SOURCES)
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

# 消费方口径（2026-11-04 用户定）：推荐表**要** VPS/服务器类科技羊毛；
# 而 baipiao-daily 发帖仍走论坛红线（VPS 不发），那边保持默认的 forum 口径。
policy.set_profile("wool")

# 软件限免类源：本表**不要**（2026-11-04 用户定「不要软件类的羊毛」，
# 只要基础设施 + 自建服务）。这些源在发帖 skill 里仍是正经的「软件限免」类目，
# 所以只在推荐表侧跳过，不改 fetch_sources 的源清单。
SOFTWARE_SOURCES = {"apprcn", "appinn", "iplaysoft", "sspai", "dayanzai", "tg-tosstf"}


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


# 富化缓存保留天数：desc-*.json 每条一个文件，用完就没人读了，留着只会越积越多
# （每个源每天几十条，一年就是几万个文件）。12 小时内的缓存才有命中价值，
# 留 7 天纯粹是给「手动复盘跑历史日期」留点余地。
CACHE_MAX_AGE_DAYS = 7


def prune_cache(max_age_days: int = CACHE_MAX_AGE_DAYS) -> int:
    """删掉 data/cache/ 里超期的富化缓存，返回删除个数。

    **只删 `desc-*.json`**：那是 enrich() 的正文摘要缓存，纯派生数据，删了下次重抓。
    `linuxdo.json` 不在这里（在 skill 的 data/cache/），而且它是 linux.do 源的
    运行依赖而非缓存——删了下一轮就得多跑一次隐身浏览器，不能当垃圾清。
    """
    if not CACHE.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for p in CACHE.glob("desc-*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue          # 被别的进程占着/权限不足：跳过，不影响本轮
    return removed


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


# 每轮推送的条目快照：{date, generated_at, urls:[...], entries:{url:{first_seen_round,
# miss_rounds}}}。用来回答「哪些是**本次推送**新出现的」。
#
# ⚠️ 2026-11-04 重构：原来只记 urls（「上一轮展示过的 URLs」），于是有两个漏洞：
#   ① 上一轮某源抓取失败 → 它本来会展示的条目不在上一轮 urls 里 → 这一轮被当成
#      「本次新增」。可它上一轮就该在榜（只是漏了），严格说不属于「本次」。
#   ② 相反，一个条目被挤出榜单（低分/被别的源抢了 Top）又回来，也会被标成新增。
# 新口径 = 「按发布时间判新旧」，且把「上轮漏抓」单独叫「补抓」：
#
#   first_seen_round = 首次出现在任何一轮**抓取结果**里的轮次时间（含被排除的）：
#       用 rows_all（窗口内全部去重行）记，而不是只记展示条目——源抓到了但没上榜的，
#       说明「上轮既没漏也没新」，这轮再上榜是新排版不是新货。
#   miss_rounds = 该源连续抓取失败的轮数（source_health）。首见轮次之前有失败轮，
#       说明这个首见可能是「补」出来的。
#
# 判级（见 mark_round）：
#   本次新增 · 确定   P > 上一轮结束        —— 本轮之后才发布，铁定新货
#   本次新增 · 疑似   first_seen_round > prev_round 且 P ≤ 上一轮结束
#                     （上轮抓取时这条还不存在或没来得及抓，上轮也没有失败记录）
#   补抓             first_seen_round > prev_round 且前面有失败轮（该抓没抓到）
#   在榜续留         其余（上轮就已在榜，或上轮抓到但没上榜）
#
# 用户口径（2026-11-04）：「15 点更新要完全属于本次，不要遗漏，也不要混进 9 点的」。
ROUND_SNAPSHOT = DATA / "last-round.json"
SOURCE_HEALTH = DATA / "source-health.json"


def load_round_snapshot() -> dict:
    """上一轮快照。没有（首次跑/搬机器）返回空 dict，调用方按「首轮」处理。"""
    try:
        d = json.loads(ROUND_SNAPSHOT.read_text(encoding="utf-8"))
        gen = d.get("generated_at")
        d["_generated"] = datetime.fromisoformat(gen) if gen else None
        d.setdefault("first_seen", {})
        return d
    except Exception:
        return {"_generated": None, "first_seen": {}}


def save_round_snapshot(day: str, generated: datetime, items: list[dict],
                        rows_all: list[dict], first_seen: dict[str, str]) -> None:
    """落盘本轮快照。first_seen = 窗口内全部行的 {url: 首次进抓取结果的轮次时间}，
    已并入历史 first_seen（只保留本轮出现的 URL，避免无限膨胀）。"""
    DATA.mkdir(parents=True, exist_ok=True)
    payload = {"date": day, "generated_at": generated.isoformat(),
               "urls": [it["url"] for it in items],
               "first_seen": first_seen}
    tmp = ROUND_SNAPSHOT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, ROUND_SNAPSHOT)


def load_source_health() -> dict:
    try:
        return json.loads(SOURCE_HEALTH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_source_health(health: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = SOURCE_HEALTH.with_suffix(".tmp")
    tmp.write_text(json.dumps(health, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, SOURCE_HEALTH)


def update_source_health(errors: list[str]) -> dict[str, int]:
    """每个源连续失败的轮数：这轮失败 +1，成功清零。返回本轮失败源的连续失败轮数。"""
    health = load_source_health()
    failed = set(errors)
    for name in set(health) | failed:
        if name in failed:
            health[name] = int(health.get(name, 0)) + 1
        else:
            health[name] = 0
    save_source_health(health)
    return {n: health.get(n, 1) for n in failed}


def _pub_dt(it: dict) -> datetime | None:
    try:
        dt = datetime.fromisoformat(it["published"]) if it.get("published") else None
    except (ValueError, TypeError):
        return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def mark_round(it: dict, prev_round: datetime | None, prev_first_seen: dict,
               prev_failed: set[str], round_start: datetime) -> None:
    """给一条打「本轮标记」，写回 round_tag / round_tag_kind / round_reason。

    判据只有两条，按优先级：
      ① 上轮抓取结果里有没有它？（prev_first_seen 记的是「首次进抓取结果的轮次」）
         first 早于 prev_round → 上轮就抓到过 → 在榜续留。
      ② 上轮没抓到，为什么？发布时间晚于上轮 → 真·新发布；否则 → 上轮漏抓（补抓）。
         若该源上轮是抓取失败（prev_failed），一律算补抓——不是新货，是上轮的债。

    这样 15:00 那轮里，9:00 已经在榜的条目**绝不会**被标成「本次新增」；
    9:00 因源失败漏掉的条目也不会冒充新货（标「补抓」，且不计入新增数）。
    """
    pub = _pub_dt(it)
    first = prev_first_seen.get(it["url"])
    prev_iso = prev_round.isoformat() if prev_round else None

    if prev_round is None:
        # 没有上一轮基线（首轮/搬机器）：按「本轮开始前 8 小时内发布」判，宁可标多别标漏
        is_new = pub is not None and pub >= round_start - timedelta(hours=8)
        it.update(round_tag="本次新增" if is_new else "在榜续留",
                  round_tag_kind="new" if is_new else "keep",
                  round_reason="无上轮基线，按发布时间判（8 小时内算新增）")
        return

    if first is not None and first <= prev_iso:
        # <= 而不是 <：first_seen 记的是「首次进抓取结果的轮次」，若它正好等于上一轮
        # 的时间戳，说明**上一轮就抓到了**（只是可能没达线）。用 < 会把这种情况
        # 漏到下面判成「补抓」——实测 15:00 那轮把 9:00 的条目全标成了补抓。
        it.update(round_tag="在榜续留", round_tag_kind="keep",
                  round_reason="上轮抓取结果里已有（在榜或未达线）")
        return

    # 上轮没抓到：区分「真新」与「补抓」
    if it.get("source") in prev_failed:
        it.update(round_tag="补抓", round_tag_kind="catchup",
                  round_reason="上轮该源抓取失败，本轮才抓到")
    elif pub is not None and pub > prev_round:
        it.update(round_tag="本次新增", round_tag_kind="new",
                  round_reason=f"发布于上轮之后（{pub:%m-%d %H:%M}）")
    elif pub is None:
        it.update(round_tag="本次新增", round_tag_kind="new",
                  round_reason="无发布时间，上轮未抓到")
    else:
        # 上轮源正常、发布时间却早于上轮 → 上轮窗口外/feed 迟到。按补抓算，
        # 不冒充新货（用户口径：本次更新只含本次）。
        it.update(round_tag="补抓", round_tag_kind="catchup",
                  round_reason=f"发布于上轮之前（{pub:%m-%d %H:%M}），上轮未抓到")


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
               "服务器 VPS": "🖥️", "AI 额度": "✨", "自建服务": "🛠️", "其他": "📦"}

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
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:16px 0 20px}
.stat{border:1px solid var(--line);border-radius:14px;background:var(--surface);
  padding:12px 14px;box-shadow:0 3px 14px rgba(25,45,80,.035)}
.stat strong{display:block;font-size:26px;line-height:1.15;letter-spacing:-.02em}
.stat span{display:block;color:var(--hint);font-size:12px;margin-top:2px}
.stat.hl strong{color:var(--primary)}
/* 「本次新增」格：绿色，和「条可写」的靛蓝区分开，一眼找到这次的新货 */
.stat.hot strong{color:var(--good-fg)}
.hl-num{color:var(--good-fg)}
@media (max-width:640px){.stats{grid-template-columns:repeat(2,1fr)}
  .stats .stat:last-child{grid-column:span 2}}

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
@media (max-width:640px){.group-head{flex-wrap:wrap}
  .group-head .portion{white-space:normal}}
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
.go{margin-left:auto;padding:8px 12px;border-radius:8px;background:var(--primary);color:#fff;
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
.exc-g ul{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:2px 16px}
.exc-g li{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
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
  const scopes = [...document.querySelectorAll('.chip[data-scope]')];
  // 默认范围 = 本轮新增（2026-11-04 用户口径：本次更新只含本次）。
  // 本轮真·无新增时退回「全部」，否则页面会空得像坏了。
  let cat = 'all', sort = 'score', scope = 'new';
  const hasNew = cards.some(c => c.dataset.scope === 'new');
  if (!hasNew) {
    scope = 'all';
    scopes.forEach(x => x.classList.toggle('active', x.dataset.scope === 'all'));
  }

  const refresh = () => {
    const term = q.value.trim().toLowerCase();
    cards.forEach(c => {
      const okScope = scope === 'all' || c.dataset.scope === scope;
      const okCat = cat === 'all' || c.dataset.cat === cat;
      const okText = !term || c.dataset.text.includes(term);
      c.hidden = !(okScope && okCat && okText);
    });
    groups.forEach(g => {
      g.hidden = ![...g.querySelectorAll('.card')].some(c => !c.hidden);
    });
    countEl.textContent = `显示 ${cards.filter(c => !c.hidden).length} / ${cards.length} 条`;
  };
  scopes.forEach(ch => ch.addEventListener('click', () => {
    scopes.forEach(x => x.classList.toggle('active', x === ch));
    scope = ch.dataset.scope; refresh();
  }));
  q.addEventListener('input', refresh);
  cats.forEach(ch => ch.addEventListener('click', () => {
    cats.forEach(x => x.classList.toggle('active', x === ch));
    cat = ch.dataset.cat; refresh();
  }));
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

  refresh();   // 首屏按默认范围（本轮新增）过滤一次，计数才对
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
    # 本轮标记（2026-11-04 口径）：三选一，由 mark_round() 判定，理由放 title 里。
    #   本次新增 = 上轮抓取结果里没有、且发布于上轮之后（亮蓝，最醒目）
    #   补抓     = 上轮该源失败 / 发布时间早于上轮，本轮才抓到（灰，不计入新增）
    #   在榜续留 = 上轮就有（灰）
    kind = it.get("round_tag_kind") or ("new" if it.get("is_new_round") else "keep")
    tag = it.get("round_tag") or ("本次新增" if kind == "new" else "在榜续留")
    cls = {"new": "new", "catchup": "keep", "keep": "keep"}.get(kind, "keep")
    reason = it.get("round_reason") or ""
    out.append(f'<span class="tag {cls}" title="{esc(reason)}">{esc(tag)}</span>')
    # 「今日首见」只在不是本次新增时补（同一天早几轮首次进榜，上轮已在榜）
    if it.get("is_today_new") and kind != "new":
        out.append('<span class="tag new-soft">今日首见</span>')
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
    kind = it.get("round_tag_kind") or "keep"
    return f"""
      <article class="card{' pick-card' if pick else ''}{' is-new' if kind == 'new' else ''}"
               data-pid="{esc(pid)}" data-cat="{esc(it['category'])}" data-score="{it['score']}"
               data-new="{1 if kind == 'new' else 0}" data-scope="{esc(kind)}"
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
        # 错误条用中文源名（2026-11-04）：内部名 linuxdo-welfare 直接怼给读者看不懂。
        f'<div class="errbox">⚠️ 本期有来源抓取失败：'
        f'<b>{esc("、".join(fs.SOURCE_LABELS.get(n, n) for n in errs))}</b>，'
        f'表中可能缺该来源的内容，下一轮会自动重试。</div>' if errs else ""
    )
    totals = totals or {}
    n_uniq = totals.get("uniq", len(items) + len(excluded))
    # 「本次新增」= 上一轮推送没有、这一轮才出现、且发布晚于上一轮快照的。
    # 发布早于上一轮快照却这时才进表的算「补抓」（上一轮漏抓：源失败/feed 迟到，
    # 2026-10-28 用户定），不混进新货里。每天推 3 次，得能一眼看出这次多了什么。
    n_new_round = sum(1 for i in items if i.get("is_new_round") and not i.get("is_catchup"))
    n_catchup = sum(1 for i in items if i.get("is_catchup"))
    n_keep = len(items) - n_new_round - n_catchup
    # 三池（本轮新增 / 补抓 / 在榜续留）在这里一次算好，下面空态、优先区、
    # 分组、模板统计都用它——重复推导容易口径漂移。
    new_pool = [i for i in items if i.get("round_tag_kind") == "new"]
    catch_pool = [i for i in items if i.get("round_tag_kind") == "catchup"]
    keep_pool = [i for i in items if i.get("round_tag_kind") not in ("new", "catchup")]
    seg = " · ".join(filter(None, [
        f'<b class="hl-num">本次新增 {n_new_round} 条</b>' if n_new_round else "",
        f"补抓 {n_catchup} 条" if n_catchup else "",
    ]))
    if seg:
        new_round_note = f'{seg}（另有 {n_keep} 条在榜续留，标「在榜续留」）。'
    else:
        new_round_note = '<b class="hl-num">本轮无新增</b>（各源暂无新货，下面展示在榜候选，「今日优先」按分数兜底）。'
    n_sources = totals.get("sources_ok", len(ACTIVE_SOURCES) - len(errs))

    # 今日优先：**只从本轮新增里挑**（2026-11-04 用户口径：本次更新只含本次）。
    # 原来是从全表挑，于是 15:00 的「今日优先」里躺着 9:00 的旧货。
    # 本轮新增不足时依次退：本轮新增 → +补抓 → 全表，标签如实写用了哪一档。
    fresh = new_pool
    fresh_or_catch = fresh + catch_pool
    top, pick_label = [], ""
    for pool, label in ((fresh, "本轮新增"), (fresh_or_catch, "本轮新增 + 补抓"),
                        (items, "全表（本轮无新增）")):
        hi = [i for i in pool if i["score"] >= 55]
        cand = pick_diverse(hi) if hi else []
        if len(cand) < 3:                      # 少于 3 条就用该池的分数 Top 兜底
            cand = pick_diverse(pool)
            if not cand:
                continue
            pick_label = f"{label} · 分数 Top {len(cand)}（≥55 分的只有 {len(hi)} 条）"
        else:
            pick_label = f"{label} · 分数 ≥55"
        top = cand
        break
    pick_label += " · 同源最多 3 条"
    # 空态说明：本轮新增为 0 但表里有旧货时，说清「为什么这一轮是空的」，
    # 别让人以为坏了。范围切换里仍能翻到补抓与在榜续留。
    empty_note = ""
    if not new_pool and items:
        empty_note = ('<div class="errbox" style="background:var(--soft);border-color:'
                      'color-mix(in srgb,var(--primary) 30%,var(--line));color:var(--muted)">'
                      f'📭 本轮（{generated:%H:%M}）没有新线索——各源自上一轮以来没有新发布。'
                      f'下面按「范围 → 在榜续留」可以看之前 {len(keep_pool)} 条仍在榜的候选。'
                      '</div>')
    elif not items:
        empty_note = ('<div class="errbox" style="background:var(--soft);border-color:'
                      'color-mix(in srgb,var(--primary) 30%,var(--line));color:var(--muted)">'
                      '📭 本轮没有候选——今日之前展示过的都发过或沉底（见排除区「往期未选」）。'
                      f'新线索要等各源更新后下一轮生成（每日 9/15/21 点，'
                      f'本轮更新于 {generated:%H:%M}）。</div>')
    top_urls = {i["url"] for i in top}

    # 「服务器 VPS」2026-11-04 起**恢复进表**（用户要服务器/主机类科技羊毛）；
    # 发帖侧仍按论坛红线排除（policy 的 profile 开关，见 wool_policy 文件头）。
    # 「自建服务」是 2026-11-04 新增的类目（自托管/容器/面板/签到脚本）；
    # 同日按用户口径从「开源工具」改名并去掉桌面软件词（限免 App 不再进表）。
    cat_order = ["AI 额度", "云资源", "服务器 VPS", "自建服务", "域名建站",
                 "生活羊毛", "其他"]
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

    # 分组按「本轮新增 / 补抓 / 在榜续留」三段拆开（2026-11-04 用户口径）：
    # 本轮新增是主角（默认展开、参与类别筛选）；补抓与在榜续留收进折叠块，
    # 避免 15:00 的页面里塞满 9:00 已经看过的条目。
    def _cat_sections(pool: list[dict], skip_urls: set[str], data_scope: str) -> str:
        by_cat: dict[str, list[dict]] = {}
        for it in pool:
            by_cat.setdefault(it["category"], []).append(it)
        out = []
        for cat in cat_order:
            rows = [r for r in (by_cat.get(cat) or []) if r["url"] not in skip_urls]
            if not rows:
                continue
            hidden = len(by_cat[cat]) - len(rows)
            note = f" · 另有 {hidden} 条在「今日优先」" if hidden else ""
            icon = GROUP_ICONS.get(cat, "📦")
            out.append(f"""
    <section class="group" data-key="{esc(cat)}" data-scope="{data_scope}">
      <div class="group-head"><h2>{icon} {esc(cat)}</h2><div class="line"></div>
        <span class="portion">{len(rows)} 条{note}</span></div>
      <div class="cards">{''.join(card_html(r) for r in rows)}</div>
    </section>""")
        return "".join(out)

    new_pool = [i for i in items if i.get("round_tag_kind") == "new"]
    catch_pool = [i for i in items if i.get("round_tag_kind") == "catchup"]
    keep_pool = [i for i in items if i.get("round_tag_kind") not in ("new", "catchup")]

    # 三段都在同一页，靠 data-scope 由 JS 切换显隐（默认只显示「本轮新增」）。
    # 不用 <details> 折叠：折叠块里的卡片仍会被筛选脚本统计，数量和「显示 x/y」对不上。
    # top_urls 对**三段都生效**（2026-11-04 审查 F2）：只把 new 段去重的话，
    # 今日优先从「补抓/在榜」兜底选出的卡片会在下方分组里再渲染一遍，
    # 「在榜续留 15」会数出 23 张卡——重复渲染直接污染所有范围的计数。
    sections = (_cat_sections(new_pool, top_urls, "new")
                + _cat_sections(catch_pool, top_urls, "catchup")
                + _cat_sections(keep_pool, top_urls, "keep"))

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
    <div class="eyebrow">{n_sources} 源聚合 · 本轮更新于 {generated:%Y-%m-%d %H:%M}</div>
    <h1>本轮新增的羊毛，挑最值得写的。</h1>
    <p>{new_round_note}页面默认<b>只显示本轮新增</b>；补抓与在榜续留用上方「范围」切换查看。黄色标签是<b>有门槛</b>（限地区/银行卡/号段/满减），红色是<b>有风险</b>（含推广/概率性）。</p>
  </div>

  <div class="stats">
    <div class="stat hot"><strong>{n_new_round}</strong><span>本轮新增</span></div>
    <div class="stat hl"><strong>{len(new_pool)}</strong><span>本轮可写</span></div>
    <div class="stat"><strong>{n_catchup}</strong><span>补抓</span></div>
    <div class="stat"><strong>{n_keep}</strong><span>在榜续留</span></div>
    <div class="stat"><strong>{n_sources}/{len(ACTIVE_SOURCES)}</strong><span>来源在线</span></div>
  </div>

  {errbox}
  {empty_note}

  <section class="panel" aria-label="搜索与筛选">
    <label class="search"><span aria-hidden="true">⌕</span>
      <input id="q" type="search" placeholder="搜索标题 / 摘要 / 标签（如：限地区）…" autocomplete="off">
    </label>
    <div class="filters"><span class="fname">范围</span>
      <div class="chips">
        <button class="chip active" type="button" data-scope="new">本轮新增 {n_new_round}</button>
        <button class="chip" type="button" data-scope="catchup">补抓 {n_catchup}</button>
        <button class="chip" type="button" data-scope="keep">在榜续留 {n_keep}</button>
        <button class="chip" type="button" data-scope="all">全部 {len(items)}</button>
      </div>
    </div>
    <div class="filters"><span class="fname">类别</span>
      <div class="chips">{''.join(cat_chips)}</div></div>
    <div class="filters"><span class="fname">排序</span>
      <div class="chips">
        <button class="chip active" type="button" data-sort="score">分数优先</button>
        <button class="chip" type="button" data-sort="time">最新发布</button>
      </div>
      <span class="count" id="count">显示 0 / {len(items)} 条</span>
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
    来源：{esc("、".join(fs.SOURCE_LABELS[n] for n in ACTIVE_SOURCES))}<br>
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
    profile = os.path.expandvars(r"%LOCALAPPDATA%\Temp\wool-edge").replace("\\", "/")
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
    # 每轮顺手清一次超期富化缓存（纯派生数据，见 prune_cache 注释）。
    # 放在抓取前：万一本轮抓取失败，也不影响这次清理。
    _pruned = prune_cache()
    if _pruned:
        print(f"[cache] 清理 {_pruned} 个超过 {CACHE_MAX_AGE_DAYS} 天的富化缓存",
              file=sys.stderr)

    rows = fetch_all()  # 全部源（skill 引擎；分源状态由引擎打到 stderr）
    # 没产出的源当抓取失败，进页面告警条（引擎对单源失败静默返回空）
    global FETCH_ERRORS
    got_sources = {r["source"] for r in rows}
    FETCH_ERRORS = [n for n in ACTIVE_SOURCES if n not in got_sources]
    print(f"[fetch] {len(got_sources)}/{len(ACTIVE_SOURCES)} 源有产出，共 {len(rows)} 行",
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
    # 上一轮快照（generated_at + first_seen：首次进抓取结果的轮次）。判断「本次新增」
    # 与「补抓」全靠它；重启、补跑、跨天都不会错。见 mark_round() 的注释。
    snap = load_round_snapshot()
    prev_generated = snap.get("_generated")
    prev_first_seen: dict[str, str] = snap.get("first_seen") or {}
    prev_health = load_source_health()
    prev_failed = {n for n, v in prev_health.items() if int(v or 0) > 0}
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
        item = {
            "title": r["title"], "url": r["url"],
            "source": r["source"], "source_label": r["source_label"],
            "summary": r.get("summary", ""),
            "published": r["published"].isoformat() if r.get("published") else "",
            "age_label": age_label,
            "category": cat, "score": sc, "reasons": why,
            "confidence": policy.confidence_of(flags),
            "flags": [k for k, v in flags.items() if v],
            "is_today_new": not (rec and rec.get("first_seen_day") == day),
        }
        # 本轮标记（2026-11-04 重写）：只回答「这条是不是本次这轮新出现的」。
        # 在榜续留 / 本次新增 / 补抓 三选一，理由写进 round_reason 供页面与简报用。
        mark_round(item, prev_generated, prev_first_seen, prev_failed, now)
        item["is_new_round"] = item["round_tag_kind"] == "new"
        item["is_catchup"] = item["round_tag_kind"] == "catchup"
        items.append(item)

    # 排序：分数优先 → 真新增在前、补抓居中、在榜最后（用户定：新货优先看）
    #       → 帖子发布时间新的在前 → 标题
    def _rank(x: dict) -> int:
        if x.get("is_catchup"):
            return 1
        return 0 if x.get("is_new_round") else 2
    items.sort(key=lambda x: (-x["score"],
                              _rank(x),
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
        # 本轮快照：下一轮据此判断「哪些是本次新增」。
        # first_seen 覆盖**窗口内全部去重行**（含被排除的），不只是上榜条目：
        # 上轮抓到但没达线的，这轮上榜属于「重新进榜」，不能算新货。
        first_seen = dict(prev_first_seen)
        now_iso = now.isoformat()
        for r in rows:
            first_seen.setdefault(r["url"], now_iso)
        # 只保留本轮仍在窗口内的 URL，避免快照无限膨胀
        keep = {r["url"] for r in rows}
        first_seen = {u: t for u, t in first_seen.items() if u in keep}
        save_round_snapshot(day, now, items, rows, first_seen)
        update_source_health(FETCH_ERRORS)

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
                      totals={"uniq": n_uniq, "sources_ok": len(ACTIVE_SOURCES) - len(FETCH_ERRORS)})
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
