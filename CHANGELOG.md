# 更新日志

本项目的显著变更记录在此文件。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循语义化版本（1.0 前的 0.x 视为面向个人的快速迭代期）。

## [0.2.1] - 2026-09-27

启动体验与网页界面改版轮：不加新功能，把「打开工具」和「看着它」这两件事做好。

### 新增

- **智能启动器**：`启动网页版.bat` 先通过新增的 `/api/health` 接口探活——服务已在跑就直接开浏览器（不再起第二个实例、不再报端口冲突），没在跑则最小化启动服务窗口、轮询就绪后再开浏览器（浏览器不会打到半启动的服务）；新增 `停止网页版.bat` 一键结束服务进程；生成带图标的 `字幕工具.lnk` 快捷方式（`.lnk` 被 .gitignore 排除，仅存于本地）
- **网页界面改版**（动效风格参考 reactbits.dev，纯 CSS/JS 零依赖、离线可用）：暗色玻璃质感 + Aurora 环境光背景、标题逐字模糊入场、卡片聚光跟随指针、主按钮渐变描边（Star Border）+ 点击迸发（Click Spark）、结果汇总 CountUp 数字动画统计磁贴；顶栏状态灯实时显示本地服务连接情况
- 网页动效尊重 `prefers-reduced-motion`，全部内容默认可见、动效只做增强；增强脚本失败不阻塞原有功能

### 修复

- 结果汇总统计磁贴误把「未登录……无字幕」提示行计成无字幕分集：改为优先从汇总文本精确解析

### 变更

- 清理后端日志与网页提示里的 `⚠️/✅/❌/⏳` 等 emoji 前缀，改用状态圆点与旋转环等样式表达

[0.2.1]: https://github.com/wu7726/subtitle-cli/releases/tag/v0.2.1

## [0.2.0] - 2026-09-27

工程健康硬化轮：不加任何新功能，补齐 CI、锁定、日志、去重与安全防护。

### 新增

- **轮转文件日志**（CLI 与网页共用）：任务起止与异常完整 traceback 写入
  `~/.subtitle-cli/logs/`（5MB × 3 份轮转，`SUBTITLE_CLI_LOG_DIR` 可覆盖）；
  Cookie 永不进日志。修复网页兜底异常只留 `TypeName: msg`、无法事后取证的问题
- **网页服务回环门禁**：Host 必须是 `127.0.0.1`/`localhost` + 本服务端口
  （封死恶意网页把域名解析到 127.0.0.1 的 DNS rebinding），POST 额外校验
  Origin/Referer 同源（封死跨站表单提交）；curl 等非浏览器工具不受影响
- GitHub Actions CI：Python 3.10 / 3.13 矩阵，uv 按 `uv.lock` 锁定同步，
  ruff + pytest + coverage 门禁，覆盖率写进 job summary
- `uv.lock` 依赖锁定；`LICENSE`（MIT）；本更新日志

### 修复

- 网页提取/迁移遇到**已失效的 vault 根目录**时显式拒绝（400），不再静默
  `mkdir` 重建死路径并把笔记写进去——对齐 CLI 既有纪律
- 网页配置改为**校验通过才写回**：写错的 vault 路径不再被记住
- Cookie 解析与 SESSDATA 门禁收敛为单一实现（CLI 与网页共用
  `resolve_bilibili_cookie`），消除四处复制导致的语义漂移；
  vault 子目录的平台三元选择同理收敛到 `dispatch.platform_subdir`
- 移除 httpx 按请求传 Cookie 的弃用警告（抖音 detail 接口改单请求 Cookie 头）

### 变更

- 新增 ruff 中等档 lint 门禁（默认规则 + bugbear + BLE + RUF + UP）并全仓归零；
  规则集中写在 `pyproject.toml`，不开 import 排序与格式化、不动现有代码风格
- 版本号单源化至 `src/subtitle_cli/__init__.py`，pyproject 动态读取

[0.2.0]: https://github.com/wu7726/subtitle-cli/releases/tag/v0.2.0
