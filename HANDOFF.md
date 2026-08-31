# JobWinner — 项目交接文档（HANDOFF）

> 最后更新：2026-08-31 · 版本 v2.4.0 · 仓库 `D:\Desktop\MYNOTE\project\jobwinner`（独立 git，vault 外）

## 1. 项目一句话

本地自动化求职 Agent：**Boss直聘 + 智联招聘** 双渠道「采集 → AI 评分 → 招呼语生成 → 人工确认 → 发送 → HR 回复监测 → 定制简历」，全部操作直连用户已登录的 Chrome（CDP）。

## 2. 运行环境（重要）

- **Python 一律用 `.venv`**：`D:\Desktop\MYNOTE\project\jobwinner\.venv\Scripts\python.exe`（系统 python 3.9 会因 `Path | None` 语法收集全炸）。
- **Dashboard**：`.venv\Scripts\python.exe -m jobwinner.main web --no-open`（端口 8686）。
- **前端**：改动 `frontend/src` 后必须 `npm run build`（frontend/ 目录，约 40s），`frontend/dist` 入库随 commit 发布（git 追踪）。
- **测试**：`python -m pytest -q`（基线 389 passed + 11 subtests；sender.py 的 CRLF→LF warning 属正常）。
- **Git push**（仓库已配 http.proxy，-c 必须在子命令前）：
  `git -C <dir> -c http.sslBackend=openssl -c http.proxy=http://127.0.0.1:7897 push origin master`
- **Browser Runtime**：内置 CDP 代理（端口 3456）桥接调试 Chrome（9222）；`/eval` sendCDP 硬超时 5s。

## 3. 当前状态（v2.4.0）

- [x] 双渠道并行采集（`channels.active: ["bosszp","zhaopin"]`），岗位 DB 标注 `channel` 字段（bosszp 1269 / zhaopin 20 条）。
- [x] 智联采集 = 搜索页 `__INITIAL_STATE__` 直读（真实薪资/全文 JD/免点击/免详情页）。
- [x] 智联发送 = 「投递 + 招呼语」专属链路（配置 `channels.zhaopin.resume` 选简历；平台默认打招呼语 + 会话内自定义招呼语）——**已实机投递易企查并发送招呼语成功**。
- [x] 智联监测 = `i.zhaopin.com/im` 消息中心回复检测（`_check_zhaopin_channel_replies`），与 BOSS 监测并行。
- [x] **双渠道独立分控**（`throttle.channel_overrides`）：日配额/间隔/退避各自独立，风控只停本渠道。

## 4. 关键架构（改前必读）

- **channels 层**：`src/jobwinner/channels/` —— `ChannelAdapter` 基类（`lock_key` / `pages_cap` / `detail_required` / `supports_send` / `build_chat_url`）+ 注册表 `@register_channel`；`get_active_channels(config)` 返回启用列表，`get_active_channel` 返回首个（主渠道）。
- **流水线按渠道分派**：
  - 采集：`scraper/jobs.py` 外层渠道循环，每渠道独立 `PageThrottle`（`throttle_for(channel.key)`）。
  - 发送：`executor/sender.py::send_greetings` 内按 `job.channel` 分派 —— BOSS 走 `_send_greeting_once`（聊天窗输入），智联走 `_send_zhaopin_greeting_once`（投递+招呼语）；分控状态在 `send_greetings` 内 `channel_states`（配额/间隔/退避/风控暂停）。
  - 监测：`executor/monitor.py::check_replies` 按 `boss_jobs` / `zhaopin_jobs` 分流，BOSS 走聊天列表 JS，智联走 `_check_zhaopin_channel_replies`（`JS_EXTRACT_CHAT_LIST_ZHAOPIN`）。
- **平台锁**：`platform_browser_lock(lock_key)` —— 同平台内采集/发送/监测互斥（发送优先、采集/监测低优先），跨平台可并行。
- **智联专用 JS**：`JS_EXTRACT_LIST`（列表，state 直读）在 `channels/zhaopin.py`；`JS_EXTRACT_CHAT_LIST_ZHAOPIN`（im 会话列表）在 `executor/monitor.py`。选择器已实测固化（详见 site-patterns）。

## 5. 决策铁律（用户钦定）

- 凡「多选一/需要拍板」的决策点（目标群、方案 A/B、**发不发/投不投**、删不删），一律把选项发给用户选择后再执行，不替用户决定。
- 智联投递是**一次性动作（无二次确认）**：任何批量投递前必须经用户确认目标岗位与简历。

## 6. 已知边界 / 坑

1. **智联翻页**：登录后仍无分页控件，`?page=N` 被忽略 → `pages_cap=1`（20 条/城×词），别浪费时间摸索翻页。
2. **智联打招呼语不可自由输入**（投递弹窗无输入框）→ 使用平台默认模板；自定义招呼语在投递后进「继续沟通」会话发送。
3. **智联简历选择**靠 `channels.zhaopin.resume` 关键词匹配（`.a-attachment-select__item` 内文本含关键词）；`"在线"` = 在线简历。
4. **已投递岗位再发送**：按钮变「继续沟通」，发送器幂等处理（不再重复投递，直接进会话发招呼语）。
5. **风控信号**（rate_limit/captcha/blocked）只暂停对应渠道（`risk_paused`），不中断另一渠道。
6. **历史数据兼容**：`channel` 为空的旧岗位在分控计数中计入 bosszp。
7. **JS 桩坑**：Python 里写 JS 用三重引号按文件级双反斜杠（`\s`/`\n`）；run_code 写含反引号内容用数组 join + 变量注入，禁止模板串。

## 7. 下一步（候选）

1. 智联回复的深度处理：对齐 BOSS `_handle_conversation`（自动回复 / 再发简历 / 跟进动作）。
2. 智联翻页解锁（登录后若出现控件再放开 pages_cap）。
3. 投递策略模板：按渠道/方向预设评分阈值与发送节奏。

## 8. 近期提交

- `dabe234` 智联采集 __INITIAL_STATE__ 直读
- `9ca80b8` 多渠道并行采集 + 来源标注
- `5aaf159` 智联投递+招呼语发送链路（实测成功）
- `9db070c` 智联消息中心回复监测
- `5cd5718` 双渠道独立分控（采集/发送 per-channel 配额/间隔/退避/风控隔离） + README/HANDOFF 补齐