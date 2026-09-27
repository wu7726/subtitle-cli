# subtitle-cli —— B站合集 / 播客 / 抖音字幕提取器

[![CI](https://github.com/wu7726/subtitle-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/wu7726/subtitle-cli/actions/workflows/ci.yml)

输入一个B站合集链接（或合集里任意一个视频链接），一次性提取该合集**全部分集**的字幕（CC 字幕 + AI 字幕）；或输入一个**播客 RSS 地址 / Apple Podcasts 节目链接**，提取各单集的现成文稿；或输入一个**抖音分享口令/视频链接**，下载音频本地转写出正文。输出为可直接阅读的 Markdown 笔记，**直接写入 Obsidian vault**，形成可搜索、可双链跳转的知识库。适合看完合集后做笔记、喂给 AI 总结、离线阅读。

> 定位：个人学习用途的轻量命令行工具，仅供个人使用。PRD 见 `PRD.md`。

## 功能

- 输入灵活：合集页 URL（含 `sid=` / `season_id=`）、**合集内单个视频的链接或 BV 号**（自动识别所属合集）、**多P视频链接**（视频选集 N/M 形态，按分P批量提取）、纯数字 season_id
- **播客支持**：RSS 地址或 Apple Podcasts 节目链接 → 按发布时间正序编号提取各单集**现成文稿**（RSS 的 `podcast:transcript` 标签，支持 VTT/SRT/JSON/纯文本/HTML），笔记默认落 vault 的 `播客字幕/` 文件夹（可用 `--vault-subdir` 改）；没有文稿的单集归入「无字幕」。**注意**：实测中文播客极少数提供文稿标签（抽样 40 个热门节目为 0），英文播客覆盖较好；小宇宙网页端不公开 RSS，请从 App「复制 RSS 链接」后粘贴
- **本地语音转写兜底（ASR）**：`--asr` 开启后，无字幕/无文稿的分集自动下载音频（B站音轨 / 播客 enclosure），用 faster-whisper 本地转写出正文——中文播客、无字幕B站视频全覆盖。默认 `medium` 模型（约 1.5GB），模型从 ModelScope 下载（国内直连，支持断点续传）；**有 NVIDIA 显卡时自动走 CUDA（float16）**，只需额外装一次 cuBLAS/cuDNN（见「显卡加速」），未装或探测不到则自动回退 CPU（int8 量化）并打印提示。纯音乐/无人声的分集会明确报失败而非产出空笔记
- **抖音视频（单个）**：粘贴抖音 App「复制链接」得到的分享口令（含 v.douyin.com 短链，自动解析 302）、视频页或图文页链接 → 自动下载媒体并用本地转写出正文（抖音无可直接抓取的字幕，`--asr` 必开）；发布日期写入属性头 `published`，笔记默认落 vault 的 `抖音字幕/<作者>/`；合集/主页批量暂不支持
- 自动翻页遍历全部分集，长合集（几十上百集）完整提取
- 逐集获取字幕并选轨：人工 CC（zh-CN）优先，其次 AI 字幕（ai-zh），再退列表第一个
- 输出纯文本 Markdown：分段拼好的自然段落，无时间轴
- **直接写入 Obsidian vault**（`--vault` 或网页配置）：每集笔记带 YAML 属性头（合集名、BV 号、集数、视频链接、抓取日期、标签），另为每个合集生成**索引页**（双链到全部分集）；路径记住一次，之后免配置
- **旧字幕迁移**（`subtitle-cli-migrate` 或网页迁移卡）：把之前下载到普通文件夹的旧字幕原地转换成新格式写入 vault，全程不联网、不需要 Cookie；提取产物永不被迁移覆盖
- **「在 Obsidian 中打开合集索引」**：网页汇总卡一键跳进 Obsidian
- **提取前审查**：先预览第 1 集的成品排版与内容（网页「预览第 1 集」按钮 / CLI `--preview`），附审查报告；落盘前自动清理无效标记行（「（音乐）」「♪」等）并合并连续重复行
- **增量下载**：文件已存在的分集自动跳过，失败重跑不重复下载；索引页每次运行按磁盘实况重生成
- 单集失败不中断整体，结尾汇总 成功 / 无字幕 / 失败 三类清单
- 全程串行 + 随机间隔 + 指数退避，降低触发风控的概率

## 安装

要求 Python ≥ 3.10。

```bash
git clone <本仓库>
cd 字幕
pip install -e .            # 基础功能（B站 + 播客现成文稿）
pip install -e ".[asr]"     # 加装本地语音转写（faster-whisper，约 200MB 依赖）

# 可选：有 NVIDIA 显卡时装上，ASR 会自动改用显卡（约 1.3GB）
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

注意：直接 `pip install faster-whisper` 时，国内网络建议加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。

### 显卡加速（可选）

两个 nvidia 包把 DLL 装在 `site-packages/nvidia/<lib>/bin`，并不在系统 PATH 上；工具会在加载模型**之前**把这些目录注入 DLL 搜索路径，所以不需要改系统环境变量，也不需要单独装 CUDA Toolkit。判定要求 cuBLAS 与 cuDNN 同时齐备，缺任一个都会回退 CPU 并打印一行说明。

回退到 CPU 时建议改用小模型（`--asr-model small`）：`medium` 在 CPU 上可能慢于音频本身时长。

## 快速体验（离线 Demo，无需 Cookie）

不想先折腾 Cookie？两种方式任选：

**命令行 Demo**：本机起临时 Mock 服务回放B站接口，CLI 全流程真实执行，10 秒内生成 6 集示例字幕：

```bash
python demo/run_demo.py
```

**网页界面**：在浏览器里点按钮提取，实时查看逐集进度、三类汇总，并直接阅读渲染好的笔记。两种启动方式任选：

```bash
# 方式一：双击项目根目录的 启动网页版.bat（自动使用 .venv，浏览器自动打开）

# 方式二：命令行启动
.venv\Scripts\python web/server.py     # 自动打开 http://127.0.0.1:8765
```

网页支持「真实接口」模式（填入 Cookie 即可提取任意真实合集，Cookie 仅本次运行透传、不落盘），并默认把成果**直接写入 Obsidian vault**：vault 路径支持「浏览…」弹窗逐级点选（首次配置后记住），路径检查自动进行，无需手动输入。命令行 Demo 输出在 `demo_output/`，覆盖成功、AI 字幕回退（EP03）、无字幕（EP05）三类场景，并演示二次运行的增量跳过。

## 使用

```bash
# 默认下载到当前目录（支持合集页 URL 或 season_id）——不带 --vault 时行为与旧版一致
subtitle-cli <合集URL或season_id>

# 提取并直接写入 Obsidian vault（传入即记住，下次可省略）
subtitle-cli <合集URL或season_id> --vault "D:/Obsidian/MyVault"

# 自定义 vault 内字幕文件夹（默认 B站字幕，可嵌套）
subtitle-cli <合集URL或season_id> --vault "D:/Obsidian/MyVault" --vault-subdir "学习/B站字幕"

# 输入合集里任意一个视频的链接或 BV 号，自动识别并提取整个合集
subtitle-cli <视频URL或BV号>

# 指定普通输出目录（显式给出时优先于 vault）
subtitle-cli <合集URL或season_id> --output D:/字幕/

# 传入 B站 Cookie（AI/CC 字幕列表需要登录态，见下文）
subtitle-cli <合集URL或season_id> --cookie "SESSDATA=..."

# 播客：RSS 地址或 Apple Podcasts 节目链接（不需要 Cookie）
subtitle-cli "https://example.com/feed.xml" --vault "D:/Obsidian/MyVault"
subtitle-cli "https://podcasts.apple.com/cn/podcast/xxx/id123456" --vault "D:/Obsidian/MyVault"
subtitle-cli "https://example.com/feed.xml" --vault "D:/Obsidian/MyVault" --vault-subdir "学习/播客"

# 无字幕时本地语音转写兜底（需先 pip install -e ".[asr]"）
subtitle-cli <合集URL或season_id> --asr                          # 默认 medium 模型，不限转写集数
subtitle-cli <合集URL或season_id> --asr --asr-limit 5            # 本次最多转写 5 集
subtitle-cli <合集URL或season_id> --asr --asr-model small        # 更快更省：tiny/base/small/medium

# 抖音单个视频：分享口令 / 短链 / 视频页链接均可（配合 --asr 本地转写）
subtitle-cli "7.42 复制打开抖音 https://v.douyin.com/iAbCdEf/ 看视频" --asr
subtitle-cli "https://www.douyin.com/video/7262554372352085267" --asr --vault "D:/Obsidian/MyVault"

# 旧字幕迁移：把 output/ 里的已有合集转换成新格式写入 vault（不联网）
subtitle-cli-migrate "output" --vault "D:/Obsidian/MyVault"
subtitle-cli-migrate "output" --vault "D:/Obsidian/MyVault" --dry-run   # 只看计划不写盘
subtitle-cli-migrate "output" --vault "D:/Obsidian/MyVault" --overwrite # 重写已迁移笔记
```

视频链接支持带任何参数（`?p=2`、分享参数等），b23.tv 短链暂不支持（请先在浏览器打开后复制完整链接）。输入的视频有三种结局：属于合集 → 提取整个合集；是含多个分P的单视频 → 提取全部分P；单P且无合集 → 明确报错。

**重要**：B站自 2023 年起，未登录状态下 player 接口返回的字幕列表恒为空（CC 和 AI 字幕都一样，2026-08-30 实测确认）。因此**必须携带 Cookie 才能拿到字幕**，否则所有分集都会被归入"无字幕"（开启 `--asr` 语音转写兜底后，无字幕分集会转写音频出正文，不再受此限制）。

### 如何获取 Cookie

1. 浏览器登录 bilibili.com；
2. 按 F12 打开开发者工具 → 网络（Network）→ 刷新页面 → 任选一个发往 `api.bilibili.com` 的请求 → 请求头里的 `Cookie:` 整串复制；
3. 通过 `--cookie` 参数传入，或设置环境变量 `BILI_COOKIE`（至少包含 `SESSDATA=...`）。

也可以只复制 SESSDATA 这一项的**值**（Application → Cookies → 双击 SESSDATA 的值），工具会自动补全字段名。粘贴后可先用网页界面的「检测登录态」按钮验证。

Cookie 属于敏感凭据：本工具只把它透传给 `api.bilibili.com` 域的请求，不落盘、不打印、不进日志，也不会存进 Git。

### 输出结构

vault 模式（`--vault` 或网页配置）：

```
<vault>/
└── B站字幕/                    # B站笔记，可改可嵌套（--vault-subdir）
    └── <合集名>/
        ├── <合集名>.md          # 合集索引页：双链到全部分集
        ├── EP01 第一集标题.md    # 每集笔记
        ├── EP02 第二集标题.md
        └── ...
└── 播客字幕/                    # 播客笔记（CLI 默认目录，网页沿用界面里的字幕文件夹设置）
    └── <播客名>/
        ├── <播客名>.md          # 索引页（不写 season_id）
        └── EPNN 单集标题.md     # 有现成文稿的单集
└── 抖音字幕/                    # 抖音笔记（按作者归档）
    └── <作者昵称>/
        ├── <作者昵称>.md        # 索引页
        └── EP01 视频标题.md     # 本地转写出的正文
```

普通文件夹模式（`--output`）结构相同，只是没有属性头与索引页。

### 笔记形态（vault 模式）

每集笔记 = YAML 属性头（键名对齐 Obsidian Web Clipper 模板）+ 原有纯文本正文：

```markdown
---
author: UP主昵称
created: 2026-08-30
description: ""
published: ""
source: "https://www.bilibili.com/video/BV1xxxxxxxx"
tags:
  - B站字幕
  - 合集名
title: 第1集 视频标题
fetched_by: subtitle-cli
---

# 第1集 视频标题

大家好，今天我们来聊一聊……
首先介绍一下背景……
```

`author` 自动取合集 UP 主；`created` 是抓取日期；`source` 是视频链接（多P 带 `?p=序号`）；`description`/`published` 暂为空位；合集归属体现在 `tags` 与文件夹结构上。

索引页 `<合集名>.md` 带 `type: index` 属性头，正文为全部分集的双链列表（`[[EP01 xxx|第1集 xxx]]`）；每次运行按磁盘实况重生成，保证双链与文件一致。多P 视频省略 `season_id`，`url` 带 `?p=序号`。

## 结果汇总与退出码

运行结束输出三类清单：

```
—— 汇总 ——
成功 45（其中增量跳过 40）
无字幕  3：EP12、EP17、EP33
失败    2：EP08（HTTP 412，疑似风控）、EP25（网络错误（ReadTimeout））
失败可重跑：subtitle-cli <同一输入> 会自动跳过已成功分集
```

- **成功**：新下载 + 增量跳过；**无字幕**：该集在登录态下也没有任何可用字幕；**失败**：网络错误/风控/解析失败，带原因。
- 退出码：`0` 无失败；`1` 存在失败；`2` 参数或输入错误。
- 连续多集疑似风控会提前终止，稍等后重跑即可（已成功的分集自动跳过）。

## 验收自测清单（需真实 vault + Cookie 时）

1. 真实模式提取一个合集到 vault：检查每集属性头字段齐全、正文与网页预览一致；
2. 同一合集重复提取：日志出现「跳过」，已存在文件修改时间不变，索引页仍完整；
3. 在 Obsidian 中打开索引页：双链可跳转、属性面板可见标签；
4. 网页汇总卡「在 Obsidian 中打开合集索引」能直接定位；
5. `subtitle-cli-migrate --dry-run` 只打印计划；正式迁移后断网重跑一次，全部「跳过」；
6. 全程检查 `~/.subtitle-cli/config.json` 与日志：不出现 Cookie。

## 开发与测试

```bash
pip install -e ".[dev]"

# 单元测试（不依赖网络，默认排除集成测试）
pytest

# Lint 检查（CI 同款规则；规则集中在 pyproject.toml 的 [tool.ruff.lint]）
ruff check .

# 集成测试（真实调用B站接口，手动开启）
SUBTITLE_CLI_INTEGRATION=1 pytest -m integration
# 端到端用例需要登录态：另设 BILI_COOKIE 环境变量
```

架构：四层单向依赖，接口层（`bilibili/`，唯一网络边界）与转换层（`converter.py` 纯函数）分离，详见 `docs/技术方案.md`。接口实测结论与 fixture 说明见 `docs/技术方案.md` §5.5 与 `tests/fixtures/README.md`。

## 免责声明

本项目仅为个人学习用途的轻量工具，字幕内容归创作者/平台所有，请勿用于商业用途或大规模抓取。
