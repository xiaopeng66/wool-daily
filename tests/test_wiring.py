#!/usr/bin/env python3
"""数据接线测试——覆盖审查方案 D2 / D4-1 / R5，全部离线可跑。

零依赖：`python tests/test_wiring.py`
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
SKILL = Path("E:/AI/Zcode/.agents/skills/baipiao-daily/scripts")

FAILS: list[str] = []
PASSES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSES.append(name)
        print(f"  ✅ {name}")
    else:
        FAILS.append(f"{name}  {detail}")
        print(f"  ❌ {name}  {detail}")


# ------------------------------------------------------------------ 载入引擎
sys.path.insert(0, str(SKILL))
sys.path.insert(0, str(PROJ))
import fetch_sources as fs  # noqa: E402
import wool_daily as W      # noqa: E402

# ------------------------------------------------------------------ D2-1 接线
print("D2-1  ACTIVE_SOURCES 全覆盖，无 fs.SOURCES 残留")
src = (PROJ / "wool_daily.py").read_text(encoding="utf-8")
uses = [i + 1 for i, line in enumerate(src.splitlines(), 1)
        if "fs.SOURCES" in line and not line.strip().startswith("#")]
check("fs.SOURCES 只出现在 ACTIVE_SOURCES 定义行", uses == [77], f"行号 {uses}")
check("页脚用 ACTIVE_SOURCES 而非 fs.SOURCES",
      "fs.SOURCE_LABELS[n] for n in fs.SOURCES" not in src,
      "页脚仍遍历全部源（含被跳过的软件源）——D2-1 实锤")
check("FETCH_ERRORS 基于 ACTIVE_SOURCES", "for n in fs.SOURCES if n not in got_sources" not in src)

print("D2-2  每个源都有 label（防页脚 KeyError）")
missing = [n for n in fs.SOURCES if n not in fs.SOURCE_LABELS]
check("SOURCE_LABELS 覆盖全部源", not missing, f"缺 {missing}")

print("D2-3  软件源名单与引擎清单一致")
check("SOFTWARE_SOURCES ⊆ 引擎源", W.SOFTWARE_SOURCES <= set(fs.SOURCES),
      f"多余 {W.SOFTWARE_SOURCES - set(fs.SOURCES)}")
check("ACTIVE_SOURCES = 23（29 - 6 软件源）", len(W.ACTIVE_SOURCES) == 23,
      f"实际 {len(W.ACTIVE_SOURCES)}")
for s in ("apprcn", "appinn", "iplaysoft", "sspai", "dayanzai", "tg-tosstf"):
    check(f"软件源 {s} 已跳过", s not in W.ACTIVE_SOURCES)

print("D2-4  抓取失败回填不误报被跳过的源")
check("FETCH_ERRORS 逻辑行存在且用 ACTIVE_SOURCES",
      "FETCH_ERRORS = [n for n in ACTIVE_SOURCES if n not in got_sources]" in src)

# ------------------------------------------------------------------ D2-7 JSON 字段
print("D2-7  data/DATE.json 字段完整")
data_file = PROJ / "data" / "2026-11-04.json"
if data_file.exists():
    d = json.loads(data_file.read_text(encoding="utf-8"))
    need = {"round_tag", "round_tag_kind", "round_reason", "is_new_round",
            "is_catchup", "is_today_new", "published", "score", "category"}
    items = d.get("items") or []
    have = set(items[0]) if items else set()
    check("items 含轮次/评分/类目全部字段", need <= have, f"缺 {need - have}")
    kinds = {i.get("round_tag_kind") for i in items}
    check("round_tag_kind ∈ {new,catchup,keep}", kinds <= {"new", "catchup", "keep"}, str(kinds))
    n_new = sum(1 for i in items if i.get("round_tag_kind") == "new")
    n_catch = sum(1 for i in items if i.get("round_tag_kind") == "catchup")
    n_keep = len(items) - n_new - n_catch
    check("三档计数一致", n_new + n_catch + n_keep == len(items))
else:
    check("data/2026-11-04.json 存在", False, "先跑一次 wool_daily.py")

# ------------------------------------------------------------------ D2-8 HTML↔JSON
print("D2-8  HTML 与 JSON 数量一致")
html_file = PROJ / "out" / "2026-11-04.html"
if html_file.exists() and data_file.exists():
    h = html_file.read_text(encoding="utf-8")
    pids = re.findall(r'<article[^>]*data-pid="([^"]+)"', h)
    uniq = set(pids)
    check("HTML 卡片 URL 去重后 == JSON items 数", len(uniq) == len(items),
          f"去重 {len(uniq)} vs JSON {len(items)}")
    dup = len(pids) - len(uniq)
    if dup:
        print(f"  ⚠️  发现重复渲染 {dup} 张（今日优先与下方分组）——记 D2-8 缺陷")
    check("无重复渲染卡片（D2-8）", dup == 0, f"重复 {dup} 张")
    check("范围 chip 数 = 4（new/catchup/keep/all）",
          len(re.findall(r'data-scope="(?:new|catchup|keep|all)"', h)) >= 4)

# ------------------------------------------------------------------ D4-1 统计带
print("D4-1  统计带栅格（已知孤儿格问题）")
if html_file.exists():
    stats = re.findall(r'<div class="stat[^"]*"><strong>', h)
    cols = re.search(r"\.stats\{[^}]*repeat\((\d+)", src)
    check("stat 数量 == CSS 列数", len(stats) == int(cols.group(1)),
          f"{len(stats)} 个 stat vs {cols.group(1)} 列 —— 会有孤儿格")

# ------------------------------------------------------------------ D2-9 cron 消息
print("D2-9  run-daily.sh 读取的字段仍然存在")
sh = (PROJ / "run-daily.sh").read_text(encoding="utf-8")
for field in ("is_new_round", "is_catchup"):
    check(f"脚本读 {field}，items 里也有", field in sh and (field in have if items else False))

# ------------------------------------------------------------------ R5 编号稳定
print("R5  帖子编号跨调用稳定")
u = "https://example.com/a"
check("同一 URL 编号稳定", W.short_id(u) == W.short_id(u))
check("不同 URL 编号不同", W.short_id(u) != W.short_id(u + "b"))
check("编号字符表无易混字符", not set(W.short_id(u)) & set("0O1IL"))

# ------------------------------------------------------------------ D11-2 JS 语法
print("D11  页面 JS 可被 node 校验（有 node 时）")
import shutil  # noqa: E402
import subprocess  # noqa: E402
if html_file.exists() and shutil.which("node"):
    js = re.search(r"<script>(.*?)</script>", h, re.S)
    tjs = Path("E:/AI/Zcode/tmp/wool-review/page.js")
    tjs.parent.mkdir(parents=True, exist_ok=True)
    tjs.write_text(js.group(1), encoding="utf-8")
    rc = subprocess.run(["node", "--check", str(tjs)], capture_output=True)
    check("页面 JS 语法正确", rc.returncode == 0, rc.stderr.decode()[:120])

# ------------------------------------------------------------------ D11-4 死代码
print("D11-4  wool_daily 无未引用定义")
tree = ast.parse(src)
defs = {n.name: n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
used_names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
used_names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
dead = [k for k in defs if k not in used_names and k != "main"]
check("无死代码", not dead, str(dead))

# ------------------------------------------------------------------ 缓存清理
print("D9-7  data/cache 超期清理（只清 desc-*，不碰运行依赖）")
import os  # noqa: E402
import tempfile  # noqa: E402
import time as _time  # noqa: E402

_tmpcache = Path(tempfile.mkdtemp(prefix="wool-cache-"))
W.CACHE = _tmpcache
_old = _tmpcache / "desc-111.json"
_new = _tmpcache / "desc-222.json"
_keep = _tmpcache / "linuxdo.json"
for _f in (_old, _new, _keep):
    _f.write_text('{"summary":"x"}', encoding="utf-8")
_aged = _time.time() - 10 * 86400          # 10 天前
os.utime(_old, (_aged, _aged))
os.utime(_keep, (_aged, _aged))
_removed = W.prune_cache(7)
check("超期 desc-* 被删", _removed == 1 and not _old.exists(), f"removed={_removed}")
check("未超期 desc-* 保留", _new.exists())
check("运行依赖 linuxdo.json 不被删（即使超期）", _keep.exists())
_removed2 = W.prune_cache(7)
check("重复清理幂等（第二次删 0 个）", _removed2 == 0, f"removed={_removed2}")

# ------------------------------------------------------------------ 汇总
print(f"\n通过 {len(PASSES)} · 失败 {len(FAILS)}")
for f in FAILS:
    print("  FAIL:", f)
sys.exit(1 if FAILS else 0)
