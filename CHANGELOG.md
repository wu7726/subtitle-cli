# 更新日志

本项目的显著变更记录在此文件。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循语义化版本（1.0 前的 0.x 视为面向个人的快速迭代期）。

## [0.5.0] - 2026-10-01

效率与可运维轮：一次整理多个合集，工程门禁全线转正。

### 新增

- **批量队列提取**：网页来源可连续「加入队列」（chip 可移除），开始后前端
  逐个串行执行——后端单任务模型不变，天然防风控；启动失败即停，任务级
  失败继续下一条
- **历史记录删除**：历史卡每行可删除对应的 runs 状态记录（按
  `(season_id, output_dir)` 定位，带确认），不碰任何用户笔记

### 修复

- **端口绑定失败分诊**：`WinError 10013`（Windows 保留端口区间，Hyper-V/
  WSL 会划走一段且范围会漂移）不再误报为「旧的 server.py 还在运行」，
  改为提示换端口；10048（端口占用）保留原指引

### 变更

- **工程门禁全线转正**：mypy 从报告制转为硬门禁（本地 28 文件 0 错误）；
  coverage 加 `--cov-fail-under=85` 防滑坡
- 移动端 375px 视口实测：无横向溢出、无越界元素、触控目标均达标

[0.5.0]: https://github.com/wu7726/subtitle-cli/releases/tag/v0.5.0

## [0.4.0] - 2026-10-01

体验演进轮：长任务可感知、网络环境可配置、网页有了操作记忆。

### 新增

- **真实进度条**：`run_collection` 新增 `on_progress(done, total)` 回调（跳过/
  失败/无字幕都计入），网页进度条从扫动动画升级为实心 N/M + 百分比，提前
  终止停在已完成处；CLI 不传回调，行为不变
- **代理支持**：`--proxy` 参数 / `SUBTITLE_CLI_PROXY` 环境变量 / 网页设置卡
  三处入口，三平台客户端请求与语音模型下载（ModelScope 断点续传）共用；
  SOCKS 需额外安装 `httpx[socks]`
- **网页历史卡可操作**：点击历史行自动切真实模式并复填来源，配合「开始提取」
  即增量重跑（/api/history 增加 season_id）
- **单集 Obsidian 直达**：vault 模式下每条产物附 `obsidian://` 链接，文件
  列表一键跳进对应单集

### 变更

- **web/server.py 拆包为 web/app/ 包**：路由门禁（handler.py）、任务线程
  （jobs.py）、共享状态（state.py）、服务基类（httpd.py），server.py 留
  启动入口；行为不变，黑盒测试全绿
- 历史卡改全宽双行布局，修复长合集名被挤成不可见

[0.4.0]: https://github.com/wu7726/subtitle-cli/releases/tag/v0.4.0

## [0.3.0] - 2026-09-29

功能演进轮：补齐最高频的输入形态，让 Obsidian 属性面板真正可用，给网页加上记忆。

### 新增

- **b23.tv 短链支持**：手机 App「复制链接」直接粘贴，自动跟随 302 跳转解析到合集或
  视频（分享文案里混着文字也可以）；CLI/网页输入提示与 README 同步更新
- **B站笔记属性头补全**：`published`（合集分集取自接口 arc.pubdate，多P取 view
  pubdate）与 `description`（多P取 view desc；合集接口常为空则留空）——日期为
  YYYY-MM-DD（北京时间），与抖音格式一致，Obsidian 可按日期排序检索
- **网页历史卡**：新增 `GET /api/history`，展示最近 10 次运行的合集名、成功/跳过/
  无字幕/失败计数与更新时间（复用 runs 状态表，与 subtitle-cli-status 同源，零新
  持久化）；页面加载与每次任务完成后刷新，无历史时整卡隐藏
- **mypy 渐进**：dev 依赖与配置就位，CI 以报告制运行（continue-on-error），按模块
  渐进收紧后转门禁

[0.3.0]: https://github.com/wu7726/subtitle-cli/releases/tag/v0.3.0

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
