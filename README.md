# wool-daily — 每日羊毛推荐表

每天 **9/15/21 点**抓取福利源，规则打分后生成一张推荐表，发布到
[GitHub Pages](https://xiaopeng66.github.io/wool-daily/)，并在 QQ 推一条
「本轮新增 N 条」的提醒。只做推荐，不写单篇稿。

**口径**：只收科技类——AI 官方额度、云厂商活动、VPS/服务器、域名/SSL/对象存储、
自托管（Docker/面板/容器云/Workers）。吃的、日用品、服饰美妆、家电、购物券、
影音会员、影视资源、梯子/机场、AI 中转站一律排除。论坛发帖另有一套口径（不发 VPS）。

## 页面

- 顶部统计带：本轮新增 / 本轮可写 / 补抓 / 在榜续留 / 来源在线。
- 三档标记，默认只显示本轮：`本次新增`（上轮之后发布）、`补抓`（上轮源失败或迟到，
  不计入新增）、`在榜续留`（上轮已抓到）。
- 范围、类别、关键词搜索、排序（分数 / 发布时间）都在前端完成，单文件零依赖。
- 「今日优先」从本轮新增里按分数取 TOP 8，下面按类别分组。
- 折叠区列出被排除的条目和原因，可直接点回原帖核对误杀。
- 英文帖（LowEndBox / LowEndTalk / LowEndSpirit 的 VPS 报价等）自动翻成中文：
  标题 tooltip 保留原文，翻译缓存写在 `data/cache/zh.json`，接口失败时退回原文。

## 数据源

29 个源、日常实际抓 23 个：即刻、V2EX（free / deals / 技术）、爱吾线报、大眼仔、
如有乐享、挖站否、NodeSeek、NodeLoc、VPS侦探、LowEndBox、LowEndTalk、LowEndSpirit、
linux.do 福利、TG 频道（Sliverkiss / LINUX DO / 二庆 / 真正值得买 / SaKurako）、
IT之家、蓝点网等。纯软件限免源（反斗、小众软件、异次元、少数派、TG·折腾App）已跳过。

源清单在 baipiao-daily skill 的 `fetch_sources.py`，追加源写 `scripts/sources.local.json`；
分类、打分、硬排除的唯一出处是同一个 skill 的 `wool_policy.py`，**改口径只改那一个文件**。

## 本地运行

```bash
python wool_daily.py --days 3 --date 2026-11-05 --no-pdf   # 生成 out/<日期>.html + data/<日期>.json
python scripts/preflight.py                                # 改完必跑，退出码 0 才算完
```

`data/` 存结构化结果与台账（`seen.json` 往期、`last-round.json` 上轮快照、
`source-health.json` 源健康），`out/` 是本地归档；两者都不进 git。

## 部署

Hermes cron `3c67d418c73f` 每天 9/15/21 点执行 `~/.hermes/scripts/wool-daily.sh`：
同步远端 → 生成 → 更新 `index.html` → commit/push → 消息经 `deliver=qqbot` 投递。
日志在 `~/.hermes/logs/wool-daily.log`。

仓库里的 `run-daily.sh` 是部署副本的源，**改完必须同步**：

```bash
cp run-daily.sh ~/.hermes/scripts/wool-daily.sh && chmod +x ~/.hermes/scripts/wool-daily.sh
```

`qqbot-send-file.py` 只用于手动补发 QQ 消息，日常不走它。

## 已知限制

- 抓取失败自动重试，仍失败时页面顶部告警条会写明缺哪个源，不会静默变空表。
- 截止日/有效期不做自动核验，只按标题关键词判断，用前点开原帖确认。
- 非科技判定靠关键词，边界会有误判；发现后在 `wool_policy.py` 补词即可。
- 分数只用于排序，不代表官方口径。
