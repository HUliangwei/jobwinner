---
domain: zhaopin.com
aliases: [智联招聘, zhaopin]
updated: 2026-08-31
---
## 平台特征
- 需要登录态才能看到完整薪资（未登录列表与详情薪资均被模糊为 `**-**元`，登录后恢复真实区间）。
- 新版已用 Vue SPA 重构：`sou.zhaopin.com` 会 302 到 `www.zhaopin.com/jobs?jl={城市码}&kw={关键词}`。
- 搜索页 = 左列表 + 右详情面板 split 布局（一条 DOM 路由，无独立列表页跳转）。
- 页面内嵌脚本会写 `LastCity` / `LastCity_id` cookie（最近浏览城市），可据此反查官方城市码。
- fe-api.zhaopin.com 列表接口带 page-request-id/client-id 签名与请求头校验，页面外重放拿不到数据（勿依赖）。

## 有效模式（已验证 2026-08-31，JobWinnerChrome 实测）
### URL / 参数
- 搜索：`https://www.zhaopin.com/jobs?jl=530&kw=java`（`jl` 城市码，`kw` 关键词；翻页参数 `page=N` 优先用右上分页器点击，直接改 URL 可能兜底重定向）
- 岗位详情：`https://www.zhaopin.com/jobdetail/CC{数字}J{数字}.htm?srccode=401903`（去掉 query 即纯净详情 URL）

### 列表页（.job-list-panel）
- 卡片：`.job-list-panel > .job-card`，激活卡带 `job-card--active`；卡片自身**没有**岗位链接/data 属性
- 标题：`.job-card__title-main`（vue-clamp 内文本在 `.vue-clamp__text`）
- 薪资：`.job-card__salary`（未登录 `**-**元`）
- 技能标签：`.job-card__skill-tag`（**顺序 = 学历→经验→技能**，与 BOSS 相反）
- 公司：`.job-card__company-name`（a → /companydetail/）
- 地点：`.job-card__location span`

### 右侧详情面板（提取岗位 URL 的唯一来源）
- 岗位 URL：`.job-company-info__view-all`（href=`/jobdetail/CC….htm`, target=_blank）
- 标题：`.job-detail-summary__title-text`
- 薪资：`.job-detail-summary__salary`
- 摘要整块 `.job-detail-summary` innerText 行序：标题 / 薪资 / 收藏分享举报 / 城区(地点`·`纵隔) / 经验 / 学历 /(可选 招N人) / 公司名 / 融资·规模·行业 / 立即投递
- 公司卡：`.job-company-info`（首行为公司名；含规模/行业/已审核/在招数/公司介绍）

### 独立详情页（/jobdetail/CC….htm）
- 标题：`h1`
- 薪资：`[class*="salary"]`
- JD 全文：`.describtion-card__detail-content`（平台把 description 拼成 **describtion**，勿改）
- 公司：`.company-info__header-left` / `.company-info__meta` / `.company-info__header`（首段公司名）
- 公司融资/规模/行业：`.company-info__desc`（`未融资 · 10000人以上 · 软件/IT服务` 按 `·` 切分）
- 沟通按钮：`.deliver-greeting-modal__btn--primary` 文案「继续沟通」（打招呼模态框，未来发送功能的地基）

### 城市码（jl 参数，2026-08-31 实机标题验证）
| 城市 | 码 | 备注 |
|------|-----|------|
| 北京 | 530 | |
| 上海 | 538 | |
| 深圳 | 765 | |
| 杭州 | 653 | |
| 合肥 | 517 | |
| 长沙 | 749 | 显示为「长株潭」区域 |
| 南昌 | 691 | |
| 嘉兴 | 656 | |
| 鹤壁 | 724 | |
| 濮阳 | 727 | |

## 提取策略（适配器已实现 · 2026-08-31 实测定稿）
- **列表提取（首选，免点击）**：搜索页在 <script> 内联 `window.__INITIAL_STATE__`（SSR 状态）。读 `__INITIAL_STATE__.positionList`：
  - `name`（标题）、`positionUrl/positionURL`（完整详情 URL）、`number`（岗位号）
  - `salary60`（展示薪资，如 "2.5-5万"）；`jobDetailData.position.base.salaryReal/salary` 为真实薪资
  - `jobDetailData.position.base`：education / positionWorkingExp / positionNumber / recruitNumber
  - `jobDetailData.position.desc.description`：**全文 JD（HTML）**，stripHtml 后即用
  - `companyName` / `companySize` / `industryName` / `cardCustomJson.address`（如 "北京 东城 朝阳门"）
  - `__INITIAL_STATE__` 在**未登录/登录墙激活时依然携带真实薪资与完整 JD**（模糊只在渲染层）。
- **详情页不再需要**：列表 state 已含 JD/公司/薪资 → 适配器 `detail_required=False`，采集只开列表页（省掉每岗位一次详情页加载）。
- 每次 evaluate 直读 ≈10ms，且不产生点击风控特征；Runtime 5s 命令超时完全无压力。
- **翻页暂不可用**：新版 SPA 忽略 `?page=N`，滚动/无分页控件（登录墙下 UI 降级），`hasMore` 恒 true 但无触发入口 → `pages_cap=1`，每城市×关键词取 20 条/页。登录后若解锁翻页控件再放开。
- **发送已接入（投递 + 招呼语，2026-08-31 实测定稿）**：岗位页「立即投递」→ 弹窗选简历(`.a-attachment-select__item`，按配置 `channels.zhaopin.resume` 关键词匹配，含"简历/在线"字样)→「投递简历」(`.a-attachment-select__action-btn__delivery`)→ 平台自动带默认招呼语投递 → 按钮变「继续沟通」→ 点击进入 `i.zhaopin.com/im` 会话 → `textarea.im-sender__input` 输入自定义招呼语 → Enter 发送。发送器：`executor/sender.py::_send_zhaopin_greeting_once`，锁使用 `zhaopin` 平台锁。
- 已投递岗位再发送时直接走「继续沟通」路径（幂等）。
- 监测（消息中心回复检测）尚未接入：回复发生在 `i.zhaopin.com/im` 会话页，后续在 monitor 中补。

## 尝试过的失败路径（勿重复踩）
- **逐卡点击读取右侧面板**：2026-08-31 首次可用（9 条），但多次高频点击后风控冻结面板（view-all href 不再随点击变化），且 `.job-list-login-gate` 激活。不可作为稳定方案。
- **fe-api 列表重放**：`/c/i/search/base/data` 只返回筛选元数据（companyType/subway 等）；猜测的 `/c/i/search/position`、`/c/i/jobs/list` 均 404；带签名的真实列表接口未知。
- **Vue 内部状态**（`__vue__`/`__vue_app__`）：线上构建不可用。
- **滚动加载**：滚动不触发下一页。

## 已知陷阱
- 未登录：薪资全部 `**-**元`，评分对薪资的依赖会失效 —— **用户需先在 JobWinnerChrome 中登录智联**。
- 逐卡点击方案比 BOSS 的整页 JSON 提取慢（约 0.3s/卡），采集每页额外消耗数秒，属预期。
- 点击过快可能让右侧面板来不及刷新 → 得到的仍是上一张卡的 view-all，已用 URL 去重兜底，但会漏掉少量岗位（重跑可补）。
- Vue 内部状态（`__vue__` / `__vue_app__`）在线上构建中不可用，不要依赖组件数据拿岗位列表。
- 岗位 URL 必须去掉 `srccode` 等 query 再入库/去重，否则同一岗位不同来源会生成不同 job_id。
