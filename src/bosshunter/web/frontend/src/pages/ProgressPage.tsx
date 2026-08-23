import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { getActionLabel, getStatusLabel } from '@/lib/status'
import { cn } from '@/lib/utils'
import {
  BriefcaseBusiness,
  ChevronDown,
  ExternalLink,
  FileDown,
  MapPin,
  Plus,
  RefreshCw,
  Wallet,
} from 'lucide-react'
import { Input } from '@/components/ui/input'

/* ------------------------------------------------------------------ */
/* Types                                                               */
/* ------------------------------------------------------------------ */

interface TimelineItem {
  action: string
  detail: string
  created_at: string
}

interface ProgressJob {
  id: string
  title: string
  company: string
  salary: string
  city: string
  experience: string
  jd: string
  score: number
  score_reason: string
  greeting: string
  status: string
  hr_name: string
  hr_title: string
  hr_active: string
  company_size: string
  company_industry: string
  url: string
  source?: string
  priority?: string
  created_at: string
  updated_at?: string
  deleted_at?: string | null
  deleted_reason?: string | null
  resume_path?: string
  last_error?: string
  stage?: string
  /** 招聘状态来源：'auto' 官网同步 / 'manual' 手动标注 / 'unknown' 查不到 */
  stage_source?: string
  stage_updated_at?: string
  first_sent_at?: string
  last_reply_at?: string
  last_follow_up_at?: string
  timeline?: TimelineItem[]
}

interface ProgressResponse {
  jobs: ProgressJob[]
}

/* ------------------------------------------------------------------ */
/* 常量：阶段、状态、action 中文映射、筛选分组                          */
/* ------------------------------------------------------------------ */

const STAGE_OPTIONS = ['筛选', '笔试', '一面', '二面', '三面', 'HR面', '谈薪', 'Offer', '已拒绝', '等结果']

/** 官网投递表单的优先级选项（值为后端存储的字母） */
const PRIORITY_OPTIONS = [
  { value: 'A', label: 'A-重点' },
  { value: 'B', label: 'B-稳妥' },
  { value: 'C', label: 'C-保底' },
]

/** 官网投递表单的初始阶段（默认"已投递"） */
const PORTAL_DEFAULT_STAGE = '已投递'

/** 官网投递表单可选阶段 */
const PORTAL_STAGE_OPTIONS = ['已投递', ...STAGE_OPTIONS]

/**
 * 来源徽标（job.source）：'portal' 官网 → 蓝色系；BOSS（缺省/其他）→ 橙色系。
 */
const SOURCE_META: Record<string, { label: string; className: string }> = {
  portal: { label: '官网', className: 'bg-blue-50 text-blue-600 border-blue-200' },
  boss: { label: 'BOSS', className: 'bg-[#FFF0E5] text-primary border-primary/20' },
}

/** 优先级徽标（job.priority）：A 红 / B 蓝 / C 灰 */
const PRIORITY_META: Record<string, { label: string; className: string }> = {
  A: { label: 'A', className: 'bg-red-50 text-red-600 border-red-200' },
  B: { label: 'B', className: 'bg-blue-50 text-blue-600 border-blue-200' },
  C: { label: 'C', className: 'bg-zinc-100 text-zinc-500 border-zinc-200' },
}

/**
 * 招聘状态来源徽标（job.stage_source）：
 * 'auto' = 官网巡检自动同步 → 绿色系；'manual' = 手动标注 → 橙色系；'unknown' = 官网查不到 → 红色系。
 * 空/未知值按 undefined 处理，不显示徽标。
 */
const STAGE_SOURCE_META: Record<string, { label: string; className: string }> = {
  auto: { label: '官网同步', className: 'bg-emerald-50 text-emerald-600 border-emerald-200' },
  manual: { label: '手动', className: 'bg-orange-50 text-orange-600 border-orange-200' },
  unknown: { label: '查不到', className: 'bg-red-50 text-red-600 border-red-200' },
}

/**
 * 状态 chip 的颜色样式。文案统一用 @/lib/status 的 getStatusLabel(status)，
 * 这里只保留 Tailwind 颜色类，避免与共享映射重复定义。
 */
const STATUS_META: Record<string, { className: string }> = {
  sent: { className: 'bg-green-600/10 text-green-700 border-green-600/20' },
  replied: { className: 'bg-emerald-600/10 text-emerald-700 border-emerald-600/20' },
  needs_resume: { className: 'bg-yellow-500/10 text-yellow-700 border-yellow-500/20' },
  rejected: { className: 'bg-red-600/10 text-red-700 border-red-600/20' },
  resume_sent: { className: 'bg-purple-600/10 text-purple-700 border-purple-600/20' },
  follow_up_sent: { className: 'bg-sky-600/10 text-sky-700 border-sky-600/20' },
  error: { className: 'bg-red-600/10 text-red-700 border-red-600/20' },
}

/**
 * 时间线 action 的补充映射：共享的 ACTION_LABELS 已覆盖 sent/replied/rejected/resume_sent
 * 等 action（详见 src/lib/status.ts），这里只补后端新增的 'stage'（阶段变更）。
 * 未命中的 action 由 getActionLabel 的 fallback 原样返回。
 */
const EXTRA_ACTION_LABELS: Record<string, string> = {
  stage: '阶段变更',
}

const GROUP_TABS = [
  { key: 'all', label: '全部' },
  { key: 'pending', label: '待反馈' },
  { key: 'interview', label: '面试中' },
  { key: 'offer', label: '已 Offer' },
  { key: 'rejected', label: '已拒绝' },
] as const

type GroupKey = (typeof GROUP_TABS)[number]['key']

/** 查看方式：按岗位平铺（现状）/ 按公司分组折叠 */
const VIEW_MODES = [
  { key: 'job', label: '按岗位' },
  { key: 'company', label: '按公司' },
] as const

type ViewMode = (typeof VIEW_MODES)[number]['key']

/* ------------------------------------------------------------------ */
/* 工具函数                                                            */
/* ------------------------------------------------------------------ */

function getStatusMeta(status: string | null | undefined) {
  if (!status) return STATUS_META.sent
  return STATUS_META[status] ?? { className: 'bg-zinc-600/10 text-zinc-600 border-zinc-600/20' }
}

function getActionDisplay(action: string | null | undefined) {
  if (!action) return '未知'
  return EXTRA_ACTION_LABELS[action] ?? getActionLabel(action)
}

function formatTime(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

/** 判断岗位是否“面试中”（stage 含“面试”） */
function isInterviewing(job: ProgressJob) {
  return Boolean(job.stage && job.stage.includes('面试'))
}

/** 判断岗位是否“待反馈”（sent/needs_resume/replied 且未结束） */
function isPending(job: ProgressJob) {
  if (job.stage === 'Offer' || job.stage === '已拒绝') return false
  return ['sent', 'needs_resume', 'replied'].includes(job.status)
}

/** 按分组 key 过滤岗位 */
function matchesGroup(job: ProgressJob, group: GroupKey) {
  switch (group) {
    case 'pending':
      return isPending(job)
    case 'interview':
      return isInterviewing(job)
    case 'offer':
      return job.stage === 'Offer'
    case 'rejected':
      return job.stage === '已拒绝'
    default:
      return true
  }
}

/* ------------------------------------------------------------------ */
/* 子组件：状态 chip                                                   */
/* ------------------------------------------------------------------ */

function StatusChip({ status }: { status?: string }) {
  const meta = getStatusMeta(status)
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 text-[11px] font-black',
        meta.className
      )}
    >
      {getStatusLabel(status)}
    </span>
  )
}

/* ------------------------------------------------------------------ */
/* 子组件：招聘状态来源小徽标                                          */
/* ------------------------------------------------------------------ */

/**
 * 招聘状态来源小徽标：'auto' 官网同步（绿）/ 'manual' 手动（橙）/ 'unknown' 查不到（红）。
 * 无来源（stage_source 为空或未知）时不渲染。
 */
function StageSourceBadge({ source }: { source?: string }) {
  const meta = source ? STAGE_SOURCE_META[source] : undefined
  if (!meta) return null
  return (
    <span
      title={source === 'unknown' ? '手动标注：官网未查到进度' : undefined}
      className={cn(
        'inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 text-[10px] font-black',
        meta.className
      )}
    >
      {meta.label}
    </span>
  )
}

/* ------------------------------------------------------------------ */
/* 子组件：单张岗位卡片                                                */
/* ------------------------------------------------------------------ */

interface JobCardProps {
  job: ProgressJob
  expanded: boolean
  onToggle: () => void
  onUpdateStage: (stage: string) => void
  stagePending: boolean
  stageError: string
}

function JobCard({ job, expanded, onToggle, onUpdateStage, stagePending, stageError }: JobCardProps) {
  const timeline = job.timeline ?? []
  const title = [job.company, job.title].filter(Boolean).join('｜') || '未知岗位'

  return (
    <div className="rounded-2xl border border-card-border bg-[#FFFCFA] shadow-sm">
      {/* 摘要头（点击展开/收起） */}
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full flex-wrap items-center gap-3 px-4 py-3 text-left transition hover:bg-[#FFF0E5]/40"
        aria-expanded={expanded}
      >
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate font-black text-foreground">{title}</span>
            {/* 来源徽标：官网投递 → 官网（蓝），BOSS 投递 → BOSS（橙） */}
            {job.source && SOURCE_META[job.source] && (
              <span
                className={cn(
                  'inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 text-[10px] font-black',
                  SOURCE_META[job.source].className
                )}
              >
                {SOURCE_META[job.source].label}
              </span>
            )}
            {/* 优先级徽标：A=红 / B=蓝 / C=灰 */}
            {job.priority && PRIORITY_META[job.priority] && (
              <span
                className={cn(
                  'inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px] font-black',
                  PRIORITY_META[job.priority].className
                )}
              >
                {PRIORITY_META[job.priority].label}
              </span>
            )}
            <StatusChip status={job.status} />
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
            {job.city && (
              <span className="inline-flex items-center gap-1">
                <MapPin className="h-3 w-3" />
                {job.city}
              </span>
            )}
            {job.salary && (
              <span className="inline-flex items-center gap-1">
                <Wallet className="h-3 w-3" />
                {job.salary}
              </span>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span
            className={cn(
              'rounded-full border px-2.5 py-1 text-xs font-black',
              job.stage
                ? 'border-primary/30 bg-[#FFF0E5] text-primary'
                : 'border-card-border bg-white text-muted'
            )}
          >
            {job.stage || '已投递'}
          </span>
          <StageSourceBadge source={job.stage_source} />
          <ChevronDown
            className={cn('h-4 w-4 shrink-0 text-muted transition-transform', expanded && 'rotate-180')}
          />
        </div>
      </button>

      {/* 展开区：阶段下拉 + 时间 + 链接 + 时间线 */}
      {expanded && (
        <div className="border-t border-card-border px-4 py-4">
          {/* 阶段编辑 */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-xs font-bold text-muted">招聘阶段</div>
            <Select
              value={job.stage || ''}
              disabled={stagePending}
              onChange={event => onUpdateStage(event.target.value)}
              className="h-8 w-36 text-xs"
            >
              <option value="">已投递</option>
              {STAGE_OPTIONS.map(stage => (
                <option key={stage} value={stage}>
                  {stage}
                </option>
              ))}
            </Select>
            {stagePending && <span className="text-xs text-muted">保存中...</span>}
            {stageError && <span className="text-xs font-bold text-danger">{stageError}</span>}
          </div>

          {/* 关键时间 */}
          <div className="mt-3 grid grid-cols-1 gap-x-6 gap-y-2 text-xs text-muted sm:grid-cols-3">
            <div>
              <div className="font-bold">首投时间</div>
              <div className="mt-0.5 font-bold text-foreground">{formatTime(job.first_sent_at)}</div>
            </div>
            <div>
              <div className="font-bold">最后回复</div>
              <div className="mt-0.5 font-bold text-foreground">
                {formatTime(job.last_reply_at || job.last_follow_up_at)}
              </div>
            </div>
            <div className="flex items-end justify-end">
              {job.url && (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => window.open(job.url, '_blank', 'noopener,noreferrer')}
                >
                  <ExternalLink className="mr-1.5 h-3.5 w-3.5" />
                  岗位链接
                </Button>
              )}
            </div>
          </div>

          {/* 时间线 */}
          <div className="mt-4 rounded-xl border border-card-border bg-white p-3">
            <div className="mb-2 flex items-center justify-between">
              <div className="text-xs font-black text-foreground">时间线</div>
              <div className="text-[10px] font-bold text-muted">{timeline.length} 条记录</div>
            </div>
            {timeline.length ? (
              <ol className="space-y-2.5">
                {timeline.map((item, index) => (
                  <li key={`${item.created_at}-${index}`} className="flex gap-2.5 text-xs">
                    <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-primary/50" />
                    <div className="min-w-0">
                      <div className="font-bold text-foreground">
                        {formatTime(item.created_at)}
                        <span className="text-muted"> — {getActionDisplay(item.action)}</span>
                        {item.detail ? `：${item.detail}` : ''}
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="text-xs text-muted">暂无时间线记录。</div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 子组件：按公司折叠卡片                                              */
/* ------------------------------------------------------------------ */

interface CompanyCardProps {
  company: string
  jobs: ProgressJob[]
  expanded: boolean
  expandedJobId: string | null
  onToggle: () => void
  onToggleJob: (jobId: string) => void
  onUpdateStage: (job: ProgressJob, stage: string) => void
  onAnnotateStage: (company: string, jobs: ProgressJob[], stage: string, stageSource: 'manual' | 'unknown') => void
  stagePendingIds: Set<string>
  stageErrors: Record<string, string>
  annotatePending: boolean
  annotateError: string
}

/** 公司内各岗位的来源集合：含 'portal' 即显示"官网"蓝徽标，其余显示 BOSS 橙 */
function companySourceMeta(jobs: ProgressJob[]) {
  const hasPortal = jobs.some(job => job.source === 'portal')
  return hasPortal ? SOURCE_META.portal : SOURCE_META.boss
}

/**
 * 公司内招聘状态来源聚合：取“最值得注意”的一个。
 * 有 unknown → '查不到'；有 auto → '官网同步'；否则（manual/空）→ '手动'。
 */
function companyStageSource(jobs: ProgressJob[]): 'auto' | 'manual' | 'unknown' {
  if (jobs.some(job => job.stage_source === 'unknown')) return 'unknown'
  if (jobs.some(job => job.stage_source === 'auto')) return 'auto'
  return 'manual'
}

/** 公司内最高优先级：按 A > B > C 取最先出现的一个 */
function companyTopPriority(jobs: ProgressJob[]): string | null {
  for (const value of ['A', 'B', 'C']) {
    const hit = jobs.find(job => job.priority === value)
    if (hit) return value
  }
  return null
}

/** 公司内最进展的 stage：按 STAGE_OPTIONS 定义顺序取最靠后的（Offer > 已拒绝 优先） */
function companyDeepestStage(jobs: ProgressJob[]): string | null {
  let best: string | null = null
  let bestIndex = -1
  for (const job of jobs) {
    if (!job.stage) continue
    const index = STAGE_OPTIONS.indexOf(job.stage)
    if (index !== -1 && index > bestIndex) {
      bestIndex = index
      best = job.stage
    }
  }
  return best
}

function CompanyCard({
  company,
  jobs,
  expanded,
  expandedJobId,
  onToggle,
  onToggleJob,
  onUpdateStage,
  onAnnotateStage,
  stagePendingIds,
  stageErrors,
  annotatePending,
  annotateError,
}: CompanyCardProps) {
  const source = companySourceMeta(jobs)
  const topPriority = companyTopPriority(jobs)
  const deepest = companyDeepestStage(jobs)
  const stageSource = companyStageSource(jobs)
  // 手动标注面板状态：当前选中的阶段（''=未选，__UNKNOWN__=官网查不到）
  const [annotateStage, setAnnotateStage] = useState('')

  const handleAnnotateSave = (stage: string, stageSourceValue: 'manual' | 'unknown') => {
    onAnnotateStage(company, jobs, stage, stageSourceValue)
    setAnnotateStage('')
  }

  return (
    <div className="rounded-2xl border border-card-border bg-[#FFFCFA] shadow-sm">
      {/* 公司头（点击展开/收起） */}
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full flex-wrap items-center gap-3 px-4 py-3 text-left transition hover:bg-[#FFF0E5]/40"
        aria-expanded={expanded}
      >
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <span className="truncate font-black text-foreground">{company}</span>
          {/* 公司来源徽标：含官网 → 官网（蓝）；否则 BOSS（橙） */}
          {source && (
            <span
              className={cn(
                'inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 text-[10px] font-black',
                source.className
              )}
            >
              {source.label}
            </span>
          )}
          {/* 岗位数 badge */}
          <span className="inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded-full bg-[#FFF0E5] px-1.5 text-[11px] font-black text-primary">
            {jobs.length} 岗
          </span>
          {/* 最高优先级 */}
          {topPriority && PRIORITY_META[topPriority] && (
            <span
              className={cn(
                'inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px] font-black',
                PRIORITY_META[topPriority].className
              )}
            >
              {PRIORITY_META[topPriority].label}
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {/* 当前最进展 stage + 招聘状态来源标签（聚合） */}
          <span
            className={cn(
              'rounded-full border px-2.5 py-1 text-xs font-black',
              deepest
                ? 'border-primary/30 bg-[#FFF0E5] text-primary'
                : 'border-card-border bg-white text-muted'
            )}
          >
            {deepest || '已投递'}
          </span>
          <StageSourceBadge source={stageSource} />
          <ChevronDown
            className={cn('h-4 w-4 shrink-0 text-muted transition-transform', expanded && 'rotate-180')}
          />
        </div>
      </button>

      {/* 展开区：手动标注 + 公司内岗位紧凑行（点击行展开该岗位的完整 JobCard，叠放在下方） */}
      {expanded && (
        <div className="border-t border-card-border px-4 py-3">
          {/* 手动标注工具栏：给该公司所有岗位统一标注招聘状态 / 官网查不到 */}
          <div className="mb-3 rounded-xl border border-card-border bg-white p-3">
            <div className="flex flex-wrap items-center gap-2">
              <div className="text-xs font-black text-foreground">手动标注</div>
              <div className="text-[10px] font-bold text-muted">为该公司全部岗位设置招聘状态</div>
              <div className="ml-auto flex flex-wrap items-center gap-2">
                <Select
                  value={annotateStage}
                  disabled={annotatePending}
                  onChange={event => setAnnotateStage(event.target.value)}
                  className="h-8 w-36 text-xs"
                >
                  <option value="">选择状态...</option>
                  {STAGE_OPTIONS.map(stage => (
                    <option key={stage} value={stage}>
                      {stage}
                    </option>
                  ))}
                  <option value="__UNKNOWN__">官网查不到</option>
                </Select>
                <Button
                  size="sm"
                  disabled={annotatePending || !annotateStage}
                  onClick={() => {
                    if (annotateStage === '__UNKNOWN__') handleAnnotateSave('', 'unknown')
                    else handleAnnotateSave(annotateStage, 'manual')
                  }}
                >
                  {annotatePending ? '标注中...' : '标注'}
                </Button>
              </div>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] font-bold text-muted">
              <span>· 选择阶段 → 记录为「手动标注」；选择「官网查不到」→ 手动记录官网未查到该岗位进度（红色「查不到」标签）。</span>
              {annotateError && <span className="font-black text-danger">{annotateError}</span>}
            </div>
          </div>

          <ul className="space-y-1">
            {jobs.map(job => (
              <li key={job.id}>
                {/* 紧凑行 */}
                <button
                  type="button"
                  onClick={() => onToggleJob(job.id)}
                  className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-transparent px-2 py-2 text-left transition hover:border-card-border hover:bg-white"
                  aria-expanded={expandedJobId === job.id}
                >
                  <span className="min-w-0 flex-1 truncate text-sm font-bold text-foreground">
                    {job.title || '未知岗位'}
                  </span>
                  {job.city && (
                    <span className="inline-flex shrink-0 items-center gap-1 text-xs text-muted">
                      <MapPin className="h-3 w-3" />
                      {job.city}
                    </span>
                  )}
                  <span
                    className={cn(
                      'inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 text-[11px] font-black',
                      job.stage
                        ? 'border-primary/30 bg-[#FFF0E5] text-primary'
                        : 'border-card-border bg-white text-muted'
                    )}
                  >
                    {job.stage || '已投递'}
                  </span>
                  <StageSourceBadge source={job.stage_source} />
                  {job.priority && PRIORITY_META[job.priority] && (
                    <span
                      className={cn(
                        'inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px] font-black',
                        PRIORITY_META[job.priority].className
                      )}
                    >
                      {PRIORITY_META[job.priority].label}
                    </span>
                  )}
                  {job.source && SOURCE_META[job.source] && (
                    <span
                      className={cn(
                        'inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 text-[10px] font-black',
                        SOURCE_META[job.source].className
                      )}
                    >
                      {SOURCE_META[job.source].label}
                    </span>
                  )}
                  <ChevronDown
                    className={cn(
                      'h-3.5 w-3.5 shrink-0 text-muted transition-transform',
                      expandedJobId === job.id && 'rotate-180'
                    )}
                  />
                </button>

                {/* 该岗位展开 → 完整 JobCard 叠放 */}
                {expandedJobId === job.id && (
                  <div className="mt-2 mb-1">
                    <JobCard
                      job={job}
                      expanded
                      onToggle={() => onToggleJob(job.id)}
                      onUpdateStage={stage => onUpdateStage(job, stage)}
                      stagePending={stagePendingIds.has(job.id)}
                      stageError={stageErrors[job.id] || ''}
                    />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 子组件：官网投递新增表单                                            */
/* ------------------------------------------------------------------ */

interface PortalFormState {
  company: string
  title: string
  city: string
  url: string
  priority: string
  stage: string
  notes: string
  applied_date: string
}

const EMPTY_PORTAL_FORM: PortalFormState = {
  company: '',
  title: '',
  city: '',
  url: '',
  priority: '',
  stage: PORTAL_DEFAULT_STAGE,
  notes: '',
  applied_date: '',
}

interface PortalAddFormProps {
  onClose: () => void
  onSubmitted: () => void
}

function PortalAddForm({ onClose, onSubmitted }: PortalAddFormProps) {
  const [form, setForm] = useState<PortalFormState>(EMPTY_PORTAL_FORM)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const set = (key: keyof PortalFormState, value: string) => setForm(prev => ({ ...prev, [key]: value }))

  const submit = async () => {
    const company = form.company.trim()
    const title = form.title.trim()
    if (!company || !title) {
      setError('公司名和岗位名不能为空')
      return
    }
    setSubmitting(true)
    setError('')
    try {
      const res = await fetch('/api/jobs/portal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          company,
          title,
          url: form.url.trim(),
          city: form.city.trim(),
          priority: form.priority,
          stage: form.stage,
          notes: form.notes.trim(),
          applied_date: form.applied_date,
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || `添加失败：接口返回 ${res.status}`)
      setForm(EMPTY_PORTAL_FORM)
      onSubmitted()
    } catch (err) {
      setError(err instanceof Error ? err.message : '添加失败')
    } finally {
      setSubmitting(false)
    }
  }

  const inputClass = 'mt-1'

  return (
    <div className="rounded-2xl border border-card-border bg-[#FFFCFA] p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-black text-foreground">新增官网投递</div>
        <div className="text-[11px] font-bold text-muted">在官网/内推渠道手动投递后，记录到这里统一跟踪进度。</div>
      </div>

      <div className="grid grid-cols-1 gap-x-4 gap-y-3 sm:grid-cols-2">
        <label className="block text-xs font-bold text-muted">
          公司 <span className="text-danger">*</span>
          <Input
            className={inputClass}
            value={form.company}
            onChange={event => set('company', event.target.value)}
            placeholder="如 字节跳动"
          />
        </label>
        <label className="block text-xs font-bold text-muted">
          岗位 <span className="text-danger">*</span>
          <Input
            className={inputClass}
            value={form.title}
            onChange={event => set('title', event.target.value)}
            placeholder="如 前端开发工程师"
          />
        </label>
        <label className="block text-xs font-bold text-muted">
          城市
          <Input
            className={inputClass}
            value={form.city}
            onChange={event => set('city', event.target.value)}
            placeholder="如 北京"
          />
        </label>
        <label className="block text-xs font-bold text-muted">
          官网链接
          <Input
            className={inputClass}
            value={form.url}
            onChange={event => set('url', event.target.value)}
            placeholder="https://..."
          />
        </label>
        <label className="block text-xs font-bold text-muted">
          优先级
          <Select
            className={inputClass}
            value={form.priority}
            onChange={event => set('priority', event.target.value)}
          >
            <option value="">不设置</option>
            {PRIORITY_OPTIONS.map(option => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </label>
        <label className="block text-xs font-bold text-muted">
          阶段
          <Select className={inputClass} value={form.stage} onChange={event => set('stage', event.target.value)}>
            {PORTAL_STAGE_OPTIONS.map(stage => (
              <option key={stage} value={stage}>
                {stage}
              </option>
            ))}
          </Select>
        </label>
        <label className="block text-xs font-bold text-muted">
          备注
          <Input
            className={inputClass}
            value={form.notes}
            onChange={event => set('notes', event.target.value)}
            placeholder="投递渠道 / 内推人 / 其他说明"
          />
        </label>
        <label className="block text-xs font-bold text-muted">
          投递日期（可选）
          <Input
            type="date"
            className={inputClass}
            value={form.applied_date}
            onChange={event => set('applied_date', event.target.value)}
          />
        </label>
      </div>

      {error && <div className="mt-3 rounded-2xl bg-red-50 px-4 py-3 text-sm text-danger">{error}</div>}

      <div className="mt-4 flex justify-end gap-2">
        <Button variant="secondary" size="sm" onClick={onClose} disabled={submitting}>
          取消
        </Button>
        <Button size="sm" onClick={() => void submit()} disabled={submitting}>
          {submitting ? '提交中...' : '保存到看板'}
        </Button>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 主组件                                                              */
/* ------------------------------------------------------------------ */

export default function ProgressPage() {
  const [jobs, setJobs] = useState<ProgressJob[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [group, setGroup] = useState<GroupKey>('all')
  const [viewMode, setViewMode] = useState<ViewMode>('job')
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  const [expandedCompanies, setExpandedCompanies] = useState<Set<string>>(new Set())
  const [expandedCompanyJobId, setExpandedCompanyJobId] = useState<string | null>(null)
  const [stagePendingIds, setStagePendingIds] = useState<Set<string>>(new Set())
  const [stageErrors, setStageErrors] = useState<Record<string, string>>({})
  const [annotatePending, setAnnotatePending] = useState(false)
  const [annotateError, setAnnotateError] = useState('')
  const [portalOpen, setPortalOpen] = useState(false)
  const [portalNotice, setPortalNotice] = useState('')

  const refreshingRef = useRef(false)

  const fetchProgress = useCallback(async () => {
    if (refreshingRef.current) return
    refreshingRef.current = true
    setRefreshing(true)
    try {
      const res = await fetch('/api/progress', { cache: 'no-store' })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || `接口返回 ${res.status}`)
      }
      const data = (await res.json()) as ProgressResponse
      setJobs(Array.isArray(data.jobs) ? data.jobs : [])
      setError('')
    } catch (err) {
      console.error('Failed to load progress:', err)
      setError('无法读取岗位进度数据，请确认 BossHunter 后端已启动。')
    } finally {
      refreshingRef.current = false
      setRefreshing(false)
      setLoading(false)
    }
  }, [])

  // 首次加载
  useEffect(() => {
    void fetchProgress()
  }, [fetchProgress])

  // 页面可见时每 30s 轮询
  useEffect(() => {
    if (typeof document === 'undefined') return
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') void fetchProgress()
    }, 30_000)
    const onVisible = () => {
      if (document.visibilityState === 'visible') void fetchProgress()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [fetchProgress])

  // 顶部概览统计
  const stats = useMemo(() => {
    return {
      pending: jobs.filter(isPending).length,
      interviewing: jobs.filter(isInterviewing).length,
      offer: jobs.filter(job => job.stage === 'Offer').length,
      rejected: jobs.filter(job => job.stage === '已拒绝').length,
    }
  }, [jobs])

  const filteredJobs = useMemo(() => jobs.filter(job => matchesGroup(job, group)), [jobs, group])

  /**
   * 按公司分组：保持 jobs 原顺序（先到先分组），公司组内保持原相对顺序。
   * 过滤后的岗位（filteredJobs）先经过 tab 筛选，因此"按公司视图"下分组 tab 依然生效。
   */
  const companyGroups = useMemo(() => {
    const groups = new Map<string, ProgressJob[]>()
    for (const job of filteredJobs) {
      const key = job.company || '未知公司'
      const list = groups.get(key)
      if (list) list.push(job)
      else groups.set(key, [job])
    }
    return Array.from(groups.entries()).map(([company, groupJobs]) => ({ company, jobs: groupJobs }))
  }, [filteredJobs])

  // 乐观更新招聘阶段
  const updateStage = async (job: ProgressJob, stage: string) => {
    const jobId = job.id
    setStageErrors(prev => {
      const next = { ...prev }
      delete next[jobId]
      return next
    })
    const previousStage = job.stage
    // 乐观更新
    setJobs(prev => prev.map(item => (item.id === jobId ? { ...item, stage: stage || undefined } : item)))
    setStagePendingIds(prev => new Set(prev).add(jobId))
    try {
      const res = await fetch(`/api/jobs/${jobId}/stage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stage, note: '' }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || `更新失败：接口返回 ${res.status}`)
      }
      // 成功后刷新，拿回 stage + stage_updated_at + 时间线
      void fetchProgress()
    } catch (err) {
      setJobs(prev => prev.map(item => (item.id === jobId ? { ...item, stage: previousStage } : item)))
      setStageErrors(prev => ({ ...prev, [jobId]: err instanceof Error ? err.message : '更新失败' }))
    } finally {
      setStagePendingIds(prev => {
        const next = new Set(prev)
        next.delete(jobId)
        return next
      })
    }
  }

  /**
   * 手动标注：把某一阶段（或"官网查不到"）应用到公司内的所有岗位。
   * - 选普通阶段 → POST {stage, note: "手动标注", stage_source: "manual"}
   * - 选"官网查不到" → POST {stage: "", note: "手动标注：官网查不到", stage_source: "unknown"}
   * 成功后 fetchProgress() 刷新，公司卡会按聚合结果显示红/橙/绿来源标签。
   */
  const annotateCompanyJobs = async (
    company: string,
    jobsToAnnotate: ProgressJob[],
    stage: string,
    stageSource: 'manual' | 'unknown'
  ) => {
    setAnnotateError('')
    const note = stageSource === 'unknown' ? '手动标注：官网查不到' : '手动标注'
    setAnnotatePending(true)
    try {
      // 逐岗位发送（每个岗位独立一条历史记录，方便追溯）
      for (const job of jobsToAnnotate) {
        const res = await fetch(`/api/jobs/${job.id}/stage`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ stage: stageSource === 'unknown' ? '' : stage, note, stage_source: stageSource }),
        })
        if (!res.ok) {
          const data = await res.json().catch(() => ({}))
          throw new Error(`${job.title || job.id}：${data.error || `接口返回 ${res.status}`}`)
        }
      }
      void fetchProgress()
      setPortalNotice(`已标注 ${company} 的 ${jobsToAnnotate.length} 个岗位：${stageSource === 'unknown' ? '官网查不到' : stage}`)
    } catch (err) {
      setAnnotateError(err instanceof Error ? err.message : '标注失败')
    } finally {
      setAnnotatePending(false)
    }
  }

  const toggleExpanded = (jobId: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(jobId)) next.delete(jobId)
      else next.add(jobId)
      return next
    })
  }

  // 按公司视图：展开/收起某家公司（默认全部折叠）
  const toggleCompany = (company: string) => {
    setExpandedCompanies(prev => {
      const next = new Set(prev)
      if (next.has(company)) next.delete(company)
      else next.add(company)
      return next
    })
  }

  // 按公司视图：展开/收起公司内的某个岗位（同一时刻最多展开一个完整 JobCard）
  const toggleCompanyJob = (jobId: string) => {
    setExpandedCompanyJobId(prev => (prev === jobId ? null : jobId))
  }

  // 展开/收起全部公司
  const expandAllCompanies = () => {
    setExpandedCompanies(new Set(companyGroups.map(group => group.company)))
  }
  const collapseAllCompanies = () => {
    setExpandedCompanies(new Set())
  }

  // 官网投递保存成功：刷新列表 + 关闭表单 + 提示
  const handlePortalSubmitted = () => {
    setPortalOpen(false)
    setPortalNotice('已添加到看板。')
    void fetchProgress()
  }

  const handlePortalClose = () => {
    setPortalOpen(false)
    setPortalNotice('')
  }

  // 导出 md 跟踪文件（同步到 Obsidian vault 的 tmp/胡良玮_2027届秋招投递跟踪.md）
  const [exportingMd, setExportingMd] = useState(false)
  const exportMd = async () => {
    if (exportingMd) return
    setExportingMd(true)
    try {
      const res = await fetch('/api/progress/export', { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok || !data.ok) {
        throw new Error(data.error || '导出失败')
      }
      setPortalNotice(`已导出 ${data.count} 个岗位到 ${data.path}`)
    } catch (err) {
      setPortalNotice(err instanceof Error ? err.message : '导出失败')
    } finally {
      setExportingMd(false)
    }
  }

  const statCards = [
    { label: '待反馈', value: stats.pending, highlight: true },
    { label: '面试中', value: stats.interviewing, highlight: true },
    { label: '已 Offer', value: stats.offer, highlight: false },
    { label: '已拒绝', value: stats.rejected, highlight: false },
  ]

  return (
    <div className="space-y-5">
      {/* 页面标题 */}
      <section className="scroll-mt-6 rounded-3xl border border-card-border bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-xs font-black tracking-[0.18em] text-primary">PROGRESS BOARD</div>
            <h2 className="mt-1 text-3xl font-black tracking-tight">岗位进度看板</h2>
            <p className="mt-1.5 text-xs text-muted">按招聘阶段跟踪已投递岗位，展开卡片可查看完整时间线。</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="default"
              size="sm"
              onClick={() => {
                setPortalNotice('')
                setPortalOpen(open => !open)
              }}
            >
              <Plus className="mr-2 h-4 w-4" />
              {portalOpen ? '收起' : '官网投递'}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void exportMd()}
              disabled={exportingMd}
            >
              <FileDown className={cn('mr-2 h-4 w-4', exportingMd && 'animate-pulse')} />
              {exportingMd ? '导出中' : '导出 md'}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void fetchProgress()}
              disabled={refreshing}
            >
              <RefreshCw className={cn('mr-2 h-4 w-4', refreshing && 'animate-spin')} />
              {refreshing ? '刷新中' : '刷新'}
            </Button>
            <span className="rounded-full bg-[#FFF0E5] px-3 py-2 text-xs font-black text-primary">
              共 {jobs.length} 个已投递岗位
            </span>
          </div>
        </div>

        {error && <div className="mt-3 rounded-2xl bg-red-50 px-4 py-3 text-sm text-danger">{error}</div>}
        {portalNotice && (
          <div className="mt-3 rounded-2xl bg-[#FFF0E5] px-4 py-3 text-sm text-primary">{portalNotice}</div>
        )}

        {/* 顶部概览卡片 */}
        <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
          {statCards.map(card => (
            <div key={card.label} className="rounded-2xl border border-card-border bg-[#FFFCFA] p-4">
              <div className="text-xs font-bold text-muted">{card.label}</div>
              <div className={cn('mt-1 text-2xl font-black', card.highlight ? 'text-primary' : 'text-foreground')}>
                {card.value}
              </div>
            </div>
          ))}
        </div>

        {/* 查看方式切换：按岗位 / 按公司 */}
        <div className="mt-4 inline-flex flex-wrap rounded-full border border-card-border bg-white p-1">
          {VIEW_MODES.map(mode => (
            <button
              key={mode.key}
              type="button"
              onClick={() => setViewMode(mode.key)}
              className={cn(
                'rounded-full px-4 py-1.5 text-xs font-black transition',
                viewMode === mode.key ? 'bg-primary text-white shadow-sm' : 'text-muted hover:text-primary'
              )}
            >
              {mode.label}
            </button>
          ))}
        </div>

        {/* 分组筛选 tab */}
        <div className="mt-2 inline-flex flex-wrap rounded-full border border-card-border bg-white p-1">
          {GROUP_TABS.map(tab => (
            <button
              key={tab.key}
              type="button"
              onClick={() => setGroup(tab.key)}
              className={cn(
                'rounded-full px-3 py-1.5 text-xs font-black transition',
                group === tab.key ? 'bg-primary text-white shadow-sm' : 'text-muted hover:text-primary'
              )}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {/* 官网投递新增表单 */}
        {portalOpen && (
          <div className="mt-4">
            <PortalAddForm onClose={handlePortalClose} onSubmitted={handlePortalSubmitted} />
          </div>
        )}
      </section>

      {/* 岗位卡片列表 */}
      <section>
        {loading ? (
          <div className="flex h-32 items-center justify-center text-sm text-muted">加载中...</div>
        ) : filteredJobs.length ? (
          viewMode === 'company' ? (
            <div className="space-y-3">
              {/* 展开全部 / 收起全部 */}
              <div className="flex items-center justify-end gap-2">
                <Button variant="secondary" size="sm" onClick={expandAllCompanies}>
                  展开全部
                </Button>
                <Button variant="secondary" size="sm" onClick={collapseAllCompanies}>
                  收起全部
                </Button>
              </div>
              {companyGroups.map(group => (
                <CompanyCard
                  key={group.company}
                  company={group.company}
                  jobs={group.jobs}
                  expanded={expandedCompanies.has(group.company)}
                  expandedJobId={expandedCompanyJobId}
                  onToggle={() => toggleCompany(group.company)}
                  onToggleJob={toggleCompanyJob}
                  onUpdateStage={(job, stage) => void updateStage(job, stage)}
                  onAnnotateStage={(companyName, jobsToAnnotate, stage, stageSource) =>
                    void annotateCompanyJobs(companyName, jobsToAnnotate, stage, stageSource)
                  }
                  stagePendingIds={stagePendingIds}
                  stageErrors={stageErrors}
                  annotatePending={annotatePending}
                  annotateError={annotateError}
                />
              ))}
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
              {filteredJobs.map(job => (
                <JobCard
                  key={job.id}
                  job={job}
                  expanded={expandedIds.has(job.id)}
                  onToggle={() => toggleExpanded(job.id)}
                  onUpdateStage={stage => void updateStage(job, stage)}
                  stagePending={stagePendingIds.has(job.id)}
                  stageError={stageErrors[job.id] || ''}
                />
              ))}
            </div>
          )
        ) : jobs.length ? (
          <div className="rounded-2xl border border-dashed border-card-border bg-[#FFFCFA] p-5 text-center text-sm text-muted">
            当前分组下没有匹配的岗位。
          </div>
        ) : (
          <div className="flex flex-col items-center gap-3 rounded-2xl border border-dashed border-card-border bg-[#FFFCFA] p-10 text-center">
            <BriefcaseBusiness className="h-10 w-10 text-primary/40" />
            <div className="text-sm text-muted">暂无已投递岗位</div>
            <p className="max-w-md text-xs leading-5 text-muted">
              完成投递后，这里会按照招聘阶段（筛选 / 笔试 / 面试 / 谈薪 / Offer / 已拒绝）展示每个岗位的进度和时间线。
            </p>
          </div>
        )}
      </section>
    </div>
  )
}
