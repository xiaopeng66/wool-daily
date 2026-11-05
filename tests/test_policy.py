#!/usr/bin/env python3
"""口径（wool_policy）单元测试——覆盖审查方案 D3 反向用例与双口径回归。

零依赖：`python tests/test_policy.py`。

**必须保留**（防误杀）的用例来自本轮实测踩过的坑；
**必须排除**的用例来自同类误放行。两条红线：
  · wool 口径：VPS/服务器要；软件羊毛、购物、影视不要
  · forum 口径：VPS/梯子/中转站不要（论坛红线），软件限免是正经题材
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, "E:/AI/Zcode/.agents/skills/baipiao-daily/scripts")
sys.path.insert(0, str(PROJ))
import wool_policy as P  # noqa: E402

FAILS: list[str] = []
PASSES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSES.append(name)
        print(f"  ✅ {name}")
    else:
        FAILS.append(f"{name}  {detail}")
        print(f"  ❌ {name}  {detail}")


def verdict(title: str, summary: str = "", source: str = "jike", profile: str = "wool",
            url: str = "") -> str:
    P.set_profile(profile)
    cat, flags = P.classify(title, summary, url)
    return P.hard_exclude(title, cat, flags, source)


def must_keep(title: str, summary: str = "", source: str = "jike",
              profile: str = "wool", url: str = "") -> None:
    w = verdict(title, summary, source, profile, url)
    check(f"保留  {title[:46]}", w == "", f"被排除：{w}")


def must_drop(title: str, why_contains: str = "", summary: str = "",
              source: str = "jike", profile: str = "wool", url: str = "") -> None:
    w = verdict(title, summary, source, profile, url)
    ok = w != "" and (why_contains in w if why_contains else True)
    check(f"排除  {title[:46]}", ok, f"实际：{w or '未排除'}")


# ------------------------------------------------------------------ A. 必须保留（防误杀）
print("A. 反向用例——必须保留（wool 口径）")
KEEP = [
    ("Cloady.io 永久免费容器云", "每个工作区 0.5 vCPU 1GB 内存"),
    ("首页 · betaxx.us 免费子域名 更新域名 123456.loc.cc", ""),
    ("已更新支持 Qoder+Trae+WorkBuddy自动签到+机器人通知，Cloudflare Worker部署", ""),
    ("智谱发真鸡蛋了", ""),
    ("Zcode 每天的鸡蛋又来了，记得去领哦", ""),
    ("0刀卡拿去用（无效）", ""),
    ("48team成功白嫖", ""),
    ("谷歌pro18个月链接", ""),
    ("EdgeNAT 2026双11 全场VPS月付7折 28元/月起 香港/美国/韩国可选", "", "vpser"),
    ("CloudCone 9周年庆 美国洛杉矶 1核/1GB/55GB 14.24美元/年起，支持支付宝", "", "vpser"),
    ("商汤免费积分提额了", ""),
    ("白嫖 opencode 免费模型", "", "linuxdo-welfare"),
    ("补两个exa的长期兑换码", "", "linuxdo-welfare"),
    ("RackNerd’s Fall Giveaway Has Started! Cheap VPS Deals from $21.99/YEAR!", "", "lowendbox"),
    ("腾讯云 6周年 5年轻友 1折续费，新用户 轻量云99元1年", "", "vpser"),
    # 求助帖规则的反向用例：教程/陈述句不能被「问句化」规则误杀
    ("如何白嫖官方 GPT 额度（教程）", ""),
    ("Zcode 每天的鸡蛋又来了，记得去领哦", ""),
]
for row in KEEP:
    title, summary = row[0], row[1]
    source = row[2] if len(row) > 2 else "jike"
    must_keep(title, summary, source)

# ------------------------------------------------------------------ B. 必须排除（wool 口径）
print("\nB. 必须排除（wool 口径）——购物/软件/影视/闲聊/交易/抽奖")
DROP = [
    ("Rust 开源版 Photoshop 来了！PhotoCraft v0.20.1 中文版", "软件羊毛"),
    ("IDM 下载器替代工具 AB Download Manager 1.11.4", "软件羊毛"),
    ("开源轻量工具 SoundSwitch v7.4.0 解决多声卡切换", "软件羊毛"),
    ("PasteKit  // 剪贴板键盘", "软件羊毛"),
    ("限时免费获取字体 Foxer[Windows、macOS]", "软件羊毛"),
    ("(喜+3) Epic 本周限免：《Gladiator》+《Quiztop》", "软件羊毛"),
    ("嫁金钗 (2026) [WEB-4K] [国语中字] [全30集]", "影视资源"),
    ("暗黑(三季合集)【4K.高码率】【内封简繁英字幕】", "影视资源"),
    ("🚦 砸或领卷中心21-20券", "购物"),
    ("京东红包1.18，速度", ""),
    ("招行信用卡送50元立减金", ""),
    ("【中国移动】100GB流量畅快用！广东移动", ""),
    ("[20出]懒猫 新款us 4837 6折 6r", "交易"),
    ("290 出 Zouter JP BGP Global Plus", "交易"),
    ("120收taxi sg 10刀", "交易"),
    ("【继续抽抽】抽Gemini pro 3个月优惠链接 × 6", "抽奖"),
    ("抽GPT PLUS 试用号 * 2，未接码", "抽奖"),
    ("RackNerd’s Fall Giveaway! Win a Meta Quest 3", "抽奖"),
    ("Codex 的过度设计,防御性编程,测试狂魔有解法吗", ""),
    ("有稳定跑 claude、codex 的方案吗？", ""),
    ("Copilot卡在绑卡阶段", ""),
    ("啊 语音模型免费？", ""),
    # 求助帖收口（2026-11-04 用户定：不要求助贴）——每条都是实测漏网或同类变体
    ("谁给个免费查激活安卓时间的小程序我", "讨论"),
    ("各位佬有成功调用过这个大模型API的吗", "讨论"),
    ("哪里能领免费VPS", "讨论"),
    ("在哪申请免费容器云", "讨论"),
    ("蹲一个免费域名", "讨论"),
    ("来个白嫖额度的方法", "讨论"),
    ("有没有大佬教我怎么开team", "讨论"),
    ("帮忙免费验证几个Google One 学生认证", "讨论"),
    ("免费领取是真的吗？", "讨论"),
    ("这额度还能用吗？", "讨论"),
    # 中转站判定：标题不带证据时无法判「中转」，但摘要里露出 aff 链接就杀
    ("【LV中转】11 月了，给新朋友的 10 元体验额度还在", "中转"),
    ("每天免费的token又来了", "中转",
     "目前agent又开启了全天不限量供应GPT，注册每天登录即可获得25刀余额，"
     "邀请一位奖励100刀余额，欢迎来使用免费的token 有AFF https://x/register?aff=8zSQ"),
    ("梯子|翻墙专用节点推荐", "梯子"),
    ("免费机场订阅 香港节点", "梯子"),
    ("发现频道：10款大家发现的好评软件[2026年第44期]", ""),
    ("派评 | 近期值得关注的 App", ""),
    ("小米米家洗鞋机 3kg 开启众筹：3D 水流，799 元", ""),
    ("比亚迪仰望 U8 推送 OTA 升级：哨兵模式可预约", ""),
]
for row in DROP:
    title, why = row[0], row[1]
    summary = row[2] if len(row) > 2 else ""
    must_drop(title, why, summary)

# ------------------------------------------------------------------ C. forum 口径回归
print("\nC. forum（发帖）口径回归——红线不得被 wool 侧改动污染")
P.set_profile("forum")
must_drop("EdgeNAT 2026双11 全场VPS月付7折 28元/月起", "VPS", source="vpser", profile="forum")
must_drop("CloudCone 9周年庆 美国洛杉矶 1核/1GB", "VPS", source="vpser", profile="forum")
must_keep("Rust 开源版 Photoshop 来了！PhotoCraft v0.20.1 中文版", profile="forum")
must_keep("PasteKit  // 剪贴板键盘", source="tg-tosstf", profile="forum")
must_keep("腾讯云 6周年 5年轻友 1折续费，新用户 轻量云99元1年", source="vpser", profile="forum")
must_drop("免费机场订阅 香港节点", "梯子", profile="forum")

# ------------------------------------------------------------------ D. 打分与归因
print("\nD. 打分与归因")
P.set_profile("wool")
cat, flags = P.classify("EdgeNAT 2026双11 全场VPS月付7折", "", "x")
check("VPS 报价归「服务器 VPS」类目", cat == "服务器 VPS", cat)
cat, _ = P.classify("已更新支持 自动签到+机器人通知，Cloudflare Worker部署", "", "x")
check("签到/Worker 归「自建服务」类目", cat == "自建服务", cat)
sc, why = P.score_of("自建服务", {"free": True, "claim": True, "entry": True}, 0, "x" * 300)
check("免费+入口+今日 ≥ 60 分", sc >= 60, f"{sc} {why}")
P.set_profile("forum")
cat, _ = P.classify("EdgeNAT 2026双11 全场VPS月付7折", "", "x")
check("forum 口径下 VPS 类目仍标「服务器 VPS」（分类不因口径漂移）", cat == "服务器 VPS", cat)

# ------------------------------------------------------------------ 汇总
print(f"\n通过 {len(PASSES)} · 失败 {len(FAILS)}")
for f in FAILS:
    print("  FAIL:", f)
sys.exit(1 if FAILS else 0)
