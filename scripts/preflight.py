#!/usr/bin/env python3
"""改完必跑：全流程预检（2026-11-04 用户要求「全流程修改，不然出 bug」）。

一次检查把这条链上的每一环都验一遍，任何一环断了就退出码 1：

  ① 语法      py 文件能 ast.parse；sh 文件 bash -n 通过（在 WSL 里验，Git Bash 会放过 CRLF）
  ② 换行      .sh 必须是 LF（CRLF 的 .sh 在 WSL 直接 syntax error，cron 跑不起来）
  ③ 部署      ~/.hermes/scripts/wool-daily.sh 与仓库版逐字节一致（cron 跑的是这份）
  ④ 测试      tests/ 三套（round / policy / wiring）
  ⑤ 产物      index.html 与 out/<今天>.html 同源（发布后才发现不一致就晚了）

用法：
  python scripts/preflight.py          # 全检
  python scripts/preflight.py --quick  # 跳过测试套件（只查语法/换行/部署）
"""
from __future__ import annotations

import argparse
import ast
import filecmp
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
WSL_DEPLOY = "~/.hermes/scripts/wool-daily.sh"
WSL_REPO = "/mnt/e/AI/Zcode/projects/wool-daily"

PY_FILES = [
    PROJ / "wool_daily.py",
    Path("E:/AI/Zcode/.agents/skills/baipiao-daily/scripts/wool_policy.py"),
    Path("E:/AI/Zcode/.agents/skills/baipiao-daily/scripts/fetch_sources.py"),
    Path("E:/AI/Zcode/.agents/skills/baipiao-daily/scripts/linuxdo_fetch.py"),
]
SH_FILES = [PROJ / "run-daily.sh"]

OK, BAD, SKIP = [], [], []


def ok(msg: str) -> None:
    OK.append(msg)
    print(f"  ✅ {msg}")


def bad(msg: str) -> None:
    BAD.append(msg)
    print(f"  ❌ {msg}")


def skip(msg: str) -> None:
    SKIP.append(msg)
    print(f"  ⏭  {msg}")


def run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def wsl(cmd: str, timeout: int = 120) -> tuple[int, str]:
    return run(["wsl", "-e", "bash", "-lc", cmd], timeout)


def check_syntax() -> None:
    print("① 语法")
    for f in PY_FILES:
        if not f.exists():
            bad(f"{f.name} 不存在")
            continue
        try:
            ast.parse(f.read_text(encoding="utf-8"))
            ok(f"{f.name} py 语法")
        except SyntaxError as exc:
            bad(f"{f.name} py 语法：第 {exc.lineno} 行 {exc.msg}")

    has_wsl = shutil.which("wsl") is not None
    for f in SH_FILES:
        if not f.exists():
            bad(f"{f.name} 不存在")
            continue
        if not has_wsl:
            rc, out = run(["bash", "-n", str(f)])
            (ok if rc == 0 else bad)(f"{f.name} bash -n（Git Bash，不验 CRLF）")
            continue
        rc, out = wsl(f"bash -n '{WSL_REPO}/{f.name}'")
        if rc == 0:
            ok(f"{f.name} bash -n（WSL）")
        else:
            bad(f"{f.name} bash -n（WSL）：{out.strip()[:120]}")


def check_lineendings() -> None:
    print("② 换行（.sh 必须 LF）")
    for f in SH_FILES:
        if not f.exists():
            continue
        raw = f.read_bytes()
        crlf = raw.count(b"\r\n")
        if crlf:
            bad(f"{f.name} 含 {crlf} 个 CRLF —— WSL bash 会报 syntax error")
        else:
            ok(f"{f.name} 纯 LF")


def check_deploy() -> None:
    print("③ 部署副本（cron 真正执行的那份）")
    if shutil.which("wsl") is None:
        skip("没有 wsl，跳过部署一致性检查")
        return
    for f in SH_FILES:
        rc, out = wsl(f"diff -q '{WSL_REPO}/{f.name}' {WSL_DEPLOY}")
        if rc == 0:
            ok(f"{f.name} 仓库版 == 部署版")
        elif rc == 1:
            bad(f"{f.name} 仓库版 != 部署版 —— 改了没同步，线上行为不变！\n"
                f"       同步：cp '{WSL_REPO}/{f.name}' {WSL_DEPLOY} && chmod +x {WSL_DEPLOY}")
        else:
            skip(f"{f.name} 部署检查失败（{out.strip()[:80]}）")


def check_tests() -> None:
    print("④ 测试套件")
    tests = sorted((PROJ / "tests").glob("test_*.py"))
    if not tests:
        bad("tests/ 下没有测试")
        return
    for t in tests:
        rc, out = run([sys.executable, str(t)], timeout=180)
        tail = [ln for ln in out.strip().splitlines() if ln.startswith("通过")]
        summary = tail[-1] if tail else out.strip().splitlines()[-1][:60] if out.strip() else "无输出"
        (ok if rc == 0 else bad)(f"{t.name}：{summary}")


def check_artifacts() -> None:
    print("⑤ 产物一致性")
    today = date.today().isoformat()
    out_html = PROJ / "out" / f"{today}.html"
    index = PROJ / "index.html"
    if not out_html.exists():
        skip(f"out/{today}.html 还没生成（本轮没跑）")
        return
    if not index.exists():
        bad("index.html 不存在")
        return
    if filecmp.cmp(out_html, index, shallow=False):
        ok("index.html 与 out/今天.html 同源")
    else:
        bad("index.html 与 out/今天.html 不一致 —— 发布步骤没跑或只更新了一半")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过测试套件")
    args = ap.parse_args()

    print(f"全流程预检 · {PROJ}\n")
    check_syntax()
    check_lineendings()
    check_deploy()
    if not args.quick:
        check_tests()
    check_artifacts()

    print(f"\n通过 {len(OK)} · 失败 {len(BAD)} · 跳过 {len(SKIP)}")
    if BAD:
        print("\n必须修完再收工：")
        for b in BAD:
            print("  -", b.splitlines()[0])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
