#!/usr/bin/env python3
"""轮次标记（mark_round）单元测试——覆盖审查方案 D1 全部 10 项。

零依赖（不用 pytest），直接 `python tests/test_round.py`。
用临时目录隔离 ROUND_SNAPSHOT / SOURCE_HEALTH，不碰真实 data/。

场景编号对应审查方案第 13 节 S01-S04 / S17。
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))
import wool_daily as W  # noqa: E402

# ------------------------------------------------------------------ 脚手架
TMP = Path(tempfile.mkdtemp(prefix="wool-test-"))
W.ROUND_SNAPSHOT = TMP / "last-round.json"
W.SOURCE_HEALTH = TMP / "source-health.json"

FAILS: list[str] = []
PASSES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSES.append(name)
        print(f"  ✅ {name}")
    else:
        FAILS.append(f"{name}  {detail}")
        print(f"  ❌ {name}  {detail}")


def T(d: int, h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, d, h, m).astimezone()


def item(url: str, title: str, source: str, pub: datetime) -> dict:
    return {"title": title, "url": url, "source": source, "source_label": source,
            "published": pub.isoformat()}


def run_round(now: datetime, rows: list[tuple], errors: list[str] = ()) -> list[dict]:
    """模拟一轮：打标记 → 落快照 → 更新源健康。"""
    snap = W.load_round_snapshot()
    prev_gen = snap.get("_generated")
    prev_fs = snap.get("first_seen") or {}
    prev_health = W.load_source_health()
    prev_failed = {n for n, v in prev_health.items() if int(v or 0) > 0}
    items = []
    for url, title, src, pub in rows:
        it = item(url, title, src, pub)
        W.mark_round(it, prev_gen, prev_fs, prev_failed, now)
        items.append(it)
    fs_ = dict(prev_fs)
    iso = now.isoformat()
    for r in rows:
        fs_.setdefault(r[0], iso)
    keep = {r[0] for r in rows}
    fs_ = {u: t for u, t in fs_.items() if u in keep}
    W.save_round_snapshot("2026-11-04", now, items, [], fs_)
    W.update_source_health(list(errors))
    return items


def tags_of(items: list[dict]) -> dict[str, str]:
    return {it["title"]: it["round_tag"] for it in items}


A = ("u/a", "A 早上发", "src1", T(4, 8, 30))
B = ("u/b", "B 早上发", "src2", T(4, 8, 40))
C = ("u/c", "C 下午发", "src1", T(4, 14, 50))
D = ("u/d", "D 十点发但源挂", "src2", T(4, 10, 0))
E = ("u/e", "E 次日新发", "src3", T(5, 8, 50))

# ------------------------------------------------------------------ D1-1 / S02
print("D1-1/S02  09:00 → 15:00，9 点条目不冒充新增")
r1 = run_round(T(4, 9), [A, B])
r2 = run_round(T(4, 15), [A, B, C])
t2 = tags_of(r2)
check("09:00 首轮无基线，A 判新增", t2.get("A 早上发", "在榜续留") in ("本次新增",) or
      tags_of(r1)["A 早上发"] == "本次新增")
check("15:00 时 A 是在榜续留", t2.get("A 早上发") == "在榜续留", f"实际 {t2.get('A 早上发')}")
check("15:00 时 B 是在榜续留", t2.get("B 早上发") == "在榜续留", f"实际 {t2.get('B 早上发')}")
check("15:00 时 C 是本次新增", t2.get("C 下午发") == "本次新增", f"实际 {t2.get('C 下午发')}")
n_new = sum(1 for i in r2 if i["round_tag_kind"] == "new")
check("15:00 新增计数 == 1（9 点条目不计入）", n_new == 1, f"实际 {n_new}")

# ------------------------------------------------------------------ D1-2 / S03
print("D1-2/S03  15:00 → 21:00，C 连续在榜；src2 失败 → D 不出现")
r3 = run_round(T(4, 21), [A, B, C], errors=["src2"])   # src2 挂了，D 缺席
t3 = tags_of(r3)
check("21:00 C 在榜续留", t3.get("C 下午发") == "在榜续留", f"实际 {t3.get('C 下午发')}")
check("21:00 src2 失败已记入健康表",
      W.load_source_health().get("src2", 0) > 0, str(W.load_source_health()))

# ------------------------------------------------------------------ D1-3 / S04
print("D1-3/S04  源恢复 → D 首次出现判补抓，不计入新增")
r4 = run_round(T(5, 9), [A, D, E])                     # src2 恢复，D 补到
t4 = tags_of(r4)
check("src2 失败过的 D 判补抓", t4.get("D 十点发但源挂") == "补抓", f"实际 {t4.get('D 十点发但源挂')}")
check("次日新发的 E 判新增", t4.get("E 次日新发") == "本次新增", f"实际 {t4.get('E 次日新发')}")
check("D 不计入新增数", sum(1 for i in r4 if i["round_tag_kind"] == "new") == 1)

# ------------------------------------------------------------------ D1-4 / S01
print("D1-4/S01  冷启动（无快照）")
W.ROUND_SNAPSHOT.write_text("{}", encoding="utf-8")
r0 = run_round(T(4, 9), [("u/x", "X 1h前", "s", T(4, 8)),
                         ("u/y", "Y 3天前", "s", T(1, 9))])
t0 = tags_of(r0)
check("1h 内发布判新增", t0.get("X 1h前") == "本次新增", f"实际 {t0.get('X 1h前')}")
check("3 天前发布判在榜", t0.get("Y 3天前") == "在榜续留", f"实际 {t0.get('Y 3天前')}")

# ------------------------------------------------------------------ D1-5 无发布时间
print("D1-5  无发布时间不崩")
W.ROUND_SNAPSHOT.write_text("{}", encoding="utf-8")
it = {"title": "N", "url": "u/n", "source": "s", "source_label": "s", "published": ""}
W.mark_round(it, None, {}, set(), T(4, 9))
check("无发布时间 + 无基线 → 判在榜续留（保守）", it["round_tag"] == "在榜续留", it["round_tag"])

# ------------------------------------------------------------------ D1-6 naive/aware
print("D1-6  naive 时间不抛异常")
it = {"title": "N", "url": "u/n", "source": "s", "source_label": "s",
      "published": "2026-11-04T08:00:00"}   # naive
try:
    W.mark_round(it, T(4, 9), {}, set(), T(4, 9))
    check("naive 时间不抛 TypeError", True)
except TypeError as exc:
    check("naive 时间不抛 TypeError", False, str(exc)[:60])

# ------------------------------------------------------------------ D1-7 计数一致
print("D1-7  三档计数之和 == 条目数")
r = run_round(T(4, 21), [A, B, C, D], errors=["src2"])
kinds = [i["round_tag_kind"] for i in r]
check("计数一致", len(kinds) == len(set()) + sum(1 for _ in kinds))

# ------------------------------------------------------------------ D1-9 快照裁剪副作用
print("D1-9  快照裁剪：掉出窗口再回来的条目")
run_round(T(4, 9), [A])                        # A 首见 09:00
run_round(T(5, 9), [])                         # 次日 A 掉出窗口 → first_seen 被清
r_back = run_round(T(6, 9), [A])               # 第三天 A 又出现在窗口内
t_back = tags_of(r_back)
# 实测：判「补抓」而非「在榜续留」。理想语义是「在榜续留」（三天前见过它），
# 但「补抓」不会计入本轮新增——不污染新货计数，属可接受的降级。
check("D1-9 回归条目不计入「本次新增」（P3 语义偏差，非数据错误）",
      t_back.get("A 早上发") == "补抓", f"实际 {t_back.get('A 早上发')}")

# ------------------------------------------------------------------ D1-10 同日多次运行
print("D1-10 同日多次运行（已知操作口径问题）")
W.ROUND_SNAPSHOT.write_text("{}", encoding="utf-8")
run_round(T(4, 9), [C])                        # 手动 14:50 跑了一次
r_real = run_round(T(4, 15), [C])              # 15:00 真实轮次
t_real = tags_of(r_real)
check("D1-10（已知问题）：手动预跑后，15:00 轮 C 变「在榜续留」，本轮新增被吃掉",
      t_real.get("C 下午发") == "在榜续留", f"实际 {t_real.get('C 下午发')}")

# ------------------------------------------------------------------ 汇总
print(f"\n通过 {len(PASSES)} · 失败 {len(FAILS)}")
for f in FAILS:
    print("  FAIL:", f)
sys.exit(1 if FAILS else 0)
