# 渠道扩展交接文档（JobWinner → 多招聘渠道）

> 目的：为「增加其他投递渠道」提供完整改造地图。当前 JobWinner 仅支持某直聘（zhipin.com），
> 渠道相关代码分散且硬编码。本文档梳理现状、扩展点、改造步骤与注意项。

---

## 1. 当前架构（单渠道硬编码）

JobWinner 目前**只支持一个渠道：某直聘**。渠道相关逻辑分散在各模块，且大量硬编码：

| 关注点 | 当前位置 | 硬编码点 |
|--------|---------|---------|
| 采集 | `src/jobwinner/scraper/jobs.py` | `SEARCH_URL` 写死 zhipin URL；`JS_EXTRACT_LIST` 选择器写死 `.job-card-wrap` 等 |
| 评分 | `src/jobwinner/ai/scorer.py` | 无平台耦合（纯 AI 处理 JD 文本）— **可复用** |
| 招呼语 | `src/jobwinner/ai/greeter.py` | 无平台耦合（纯文本生成）— **可复用** |
| 发送 | `src/jobwinner/executor/sender.py` | 跳转/继续沟通 URL 写死 zhipin；`platform_browser_lock("boss")` |
| 监测 | `src/jobwinner/executor/monitor.py` | `chat_url` 写死 zhipin chat；平台锁 "boss" |
| 浏览器锁 | `src/jobwinner/browser_lock.py` | 按平台字符串分锁（"boss" 为唯一平台） |
| 登录检测 | `src/jobwinner/browser/diagnostics.py` | 检测 zhipin.com 页面 |
| 平台特征 | `src/jobwinner/browser/runtime/site-patterns/zhipin.com.md` | 唯一 site-pattern 文件（选择器/陷阱） |
| 页面模式 | `src/jobwinner/browser/runtime/site-patterns/` | 目前只有 zhipin.com.md 一个 |
| 状态机 | `src/jobwinner/tracker/status.py` | 状态定义通用（可复用），无平台耦合 |
| 配置 | `config.example.yaml` | `search.chat_url` 等含 zhipin 默认值 |
| Web 前端 | `src/jobwinner/web/frontend/src/` | 无渠道字段，仅展示岗位数据 |

**结论**：AI 评分、招呼语、状态机、DB 层**与渠道无关可直接复用**；采集、发送、监测、浏览器锁、site-pattern **强耦合 zhipin，是改造重点**。

---

## 2. 建议的渠道抽象层

目标是**引入「渠道适配器」模式**：每渠道一个实现，核心流水线面向接口编程。

```text
src/jobwinner/
├── channels/                  # 新增：渠道适配层
│   ├── __init__.py
│   ├── base.py                # ChannelAdapter 抽象基类
│   ├── bosszp.py              # 现 zhipin 逻辑迁入（原 scraper/sender 拆解）
│   └── providers/             # 后续新渠道（如 前程/猎聘/拉勾 等）
│       └── example_channel.py
├── browser/
│   └── runtime/site-patterns/ # 每渠道一个 .md（zhipin.com.md 为模板）
├── config.example.yaml       # 增加 channels 段
└── web/frontend/             # 渠道下拉/筛选
```

### ChannelAdapter 抽象基类（建议接口）

```python
class ChannelAdapter(ABC):
    key: str                 # 唯一标识："bosszp" / "liepin" 等
    domain: str              # 主域名，用于登录态检测
    search_url_template: str # 搜索 URL 模板

    @abstractmethod
    def extract_jobs(self, page) -> list[dict]: ...        # 列表页→岗位数据
    @abstractmethod
    def build_job_url(self, job) -> str: ...                # 岗位详情 URL
    @abstractmethod
    def open_chat(self, job) -> str: ...                    # 进入聊天页
    @abstractmethod
    def send_greeting(self, job, greeting) -> bool: ...     # 发送招呼语
    @abstractmethod
    def detect_login(self, page) -> bool: ...               # 登录态检测
    @abstractmethod
    def monitor_replies(self) -> list[dict]: ...            # HR 回复监测

    # 可选：平台特定风控策略
    def throttle_policy(self) -> dict: ...
```

---

## 3. 改造步骤（建议顺序）

### 阶段 A：抽取抽象层（不动行为）
1. 新建 `channels/base.py`，定义 `ChannelAdapter` 基类。
2. 新建 `channels/bosszp.py`，把 `scraper/jobs.py` 的采集逻辑、`sender.py` 的发送逻辑、`monitor.py` 的监测逻辑**按接口方法迁移**。
3. `scraper/jobs.py` 改为：根据配置选择 adapter 并调用。
4. 跑全量测试，确保**行为不变**（重构不加功能）。

### 阶段 B：配置化渠道选择
1. `config.example.yaml` 增加：
```yaml
channels:
  active: bosszp        # 当前启用渠道
  bosszp:
    enabled: true
    search: {...}
  # 后续渠道在此追加
```
2. DB `jobs` 表增加 `channel` 列（默认 "bosszp"），历史数据兼容。
3. 前端工作台增加渠道筛选/展示。

### 阶段 C：新增第一个渠道（验证抽象）
1. 挑一个目标渠道，写 `site-patterns/<domain>.md`（参考 zhipin.com.md 的选择器/陷阱记录法）。
2. 实现该渠道的 adapter（采集→聊天→发送→监测）。
3. 登录态检测接入 `browser/diagnostics.py`（按 domain 分支）。
4. 在 Web 面板增加该渠道的登录检测与状态展示。

---

## 4. 关键改造点（复用 vs 重写）

| 模块 | 改造类型 | 说明 |
|------|---------|------|
| `ai/scorer.py` | **直接复用** | 纯文本评分，无渠道耦合 |
| `ai/greeter.py` | **直接复用** | 纯文本招呼语生成 |
| `db.py` / `tracker/status.py` | **小幅扩展** | jobs 表加 channel 列；状态机加渠道字段 |
| `scraper/jobs.py` | **抽象重构** | 迁到 channels/bosszp.py，按接口调用 |
| `executor/sender.py` | **抽象重构** | 发送路径迁入 adapter |
| `executor/monitor.py` | **抽象重构** | 监测迁入 adapter |
| `browser_lock.py` | **保持** | 平台锁已按字符串分键，天然支持多平台 |
| `browser/runtime/site-patterns/` | **新增** | 每渠道一个 .md 模式文件 |
| `web/` 前端 | **扩展** | 渠道选择、渠道状态、渠道漏斗 |

---

## 5. 注意事项

1. **登录态**：每个渠道需在其独立 Chrome 窗口登录（CDP 直连不保存凭证）。浏览器锁已按平台分键，天然隔离。
2. **风控**：不同渠道的节流参数应独立配置（`throttle_policy`），不可共用 daily_limit（否则一个渠道占满额度）。
3. **选择器脆弱性**：平台改版会破坏 site-pattern，升级时优先维护 .md 模式文件而非代码。
4. **历史数据**：现有 jobs 全部归入 "bosszp" 渠道，不做迁移即可兼容。
5. **隐私**：新增渠道的凭证/ Cookie 依旧不入库，保持现有「CDP 直连日常 Chrome」模式。
6. **测试**：`tests/` 已有大量 zhipin 相关测试，渠道抽象后需保证原测试通过 + 新增 adapter 测试。

---

## 6. 当前已完成的直接可复用资产

- AI 评分/招呼语（纯文本，多模型）— 已稳定
- 工作台并行任务 + 发送 FIFO + 额度控制 — 已稳定
- 发送失败可见 + 延后可见 + 真实待评分计数 — 已稳定
- CDP 卡顿修复（5s 超时 + 自动重连）— 已稳定
- 浏览器平台锁（按平台字符串分锁）— 随时可接新渠道
- site-pattern 机制（zhipin.com.md 为模板）— 新渠道照此编写
- **渠道抽象层（阶段 A 已完成，见第 3 节）**：
  - `channels/base.py` — `ChannelAdapter` 抽象基类（key/domain/base_url/lock_key、URL 构建、登录检测、风控策略、生命周期钩子）
  - `channels/bosszp.py` — Boss直聘适配器（原 scraper 的 SEARCH_URL/JS 提取脚本迁入）
  - `channels/__init__.py` — 注册表 + `get_active_channel`(按 config 选择) + `set_active_channel`/`current_channel`(进程级缓存，供深调用链取渠道)
  - `scraper/jobs.py` — 采集链路改为按 adapter 构建 URL / 取选择器 / 取锁名（行为不变，全量测试通过）
  - `executor/sender.py` / `monitor.py` — 锁名、chat_url 默认值、域名判断改为读取活动渠道（行为不变）
  - `config.yaml` / `config.example.yaml` — 新增 `channels.active`（默认 `bosszp`，行为不变）
  - `tests/test_channels.py` — 15 项渠道层测试

---

## 7. 交接状态

- 代码基线：master @ c6a3c12（含渠道抽象层阶段 A，全部已推送 origin/master）
- 仓库公开：是（https://github.com/HUliangwei/jobwinner）
- 隐私：config.yaml / data/ / *.db / 简历 全部 .gitignore，公开仓库无敏感数据
- 下一步：**阶段 B（配置化 + DB channel 列）**或**阶段 C（新增首个非 boss 渠道验证抽象）**，见第 3 节