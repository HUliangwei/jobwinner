import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useDashboard, type FunnelData, type HistoryItem, type Job, type WorkbenchTask } from '@/hooks/useDashboard'
import { useJobSearch } from '@/hooks/useJobSearch'
import { Button } from '@/components/ui/button'
import { JobsTable } from '@/components/dashboard/JobsTable'
import { RecycleBinPanel } from '@/components/dashboard/RecycleBinPanel'
import { ScoreJobsDialog } from '@/components/dashboard/ScoreJobsDialog'
import { TaskPipelineStages } from '@/components/dashboard/TaskPipelineStages'
import { JobFilterBar } from '@/components/jobs/JobFilterBar'
import { parseHistoryDetail } from '@/lib/historyDetail'
import {
  EMPTY_JOB_FILTERS,
  filterJobs,
  hasInvalidSalaryRange,
  useDebouncedValue,
  type JobFilters,
} from '@/lib/jobFilters'
import { getActionLabel, getStatusLabel } from '@/lib/status'
import { cn } from '@/lib/utils'
import {
  AlertTriangle,
  BriefcaseBusiness,
  Clock,
  Download,
  ExternalLink,
  Eye,
  FileText,
  Lock,
  MessageCircle,
  Pencil,
  Play,
  RefreshCw,
  Send,
  Sparkles,
  Square,
  Star,
  Trash2,
  XCircle,
} from 'lucide-react'

type WorkbenchMode = 'full' | 'collect' | 'rescore' | 'score' | 'monitor' | 'deliver'
type DashboardView = 'workbench' | 'jobs' | 'monitor'
type StatsScope = 'today' | 'total'

const TASK_STAGE_LABELS = [
  '开始采集岗位',
  '开始 AI 评分',
  '开始重新评分',
  'AI 评分进度',
  '等待前端确认投递',
  '发送失败待处理',
  '执行一轮监测',
  '本轮监测完成，30 分钟后再次检查',
]

function currentTaskStage(logs: string[] = []) {
  for (const log of logs.slice().reverse()) {
    if (log.includes('AI 评分进度')) return log
    const stage = TASK_STAGE_LABELS.find(label => log.includes(label))
    if (stage) return stage
  }
  const last = logs[logs.length - 1]
  if (last) return last
  return '后端已就绪，等待任务指令'
}

function taskStatusText(status: string) {
  if (status === 'failed') return '运行失败'
  if (status === 'completed') return '已结束'
  if (status === 'stopped') return '已停止'
  if (status === 'stopping') return '停止中'
  return '运行中'
}

function taskStatusClass(status: string) {
  if (status === 'failed') return 'border-red-100 bg-red-50'
  if (status === 'completed' || status === 'stopped') return 'border-card-border bg-white'
  return 'border-primary/20 bg-[#FFF0E5]'
}

function taskStatusTitle(status: string) {
  if (status === 'completed' || status === 'stopped') return '最近任务状态'
  return '当前阶段'
}

function taskErrorFeedback(error: string) {
  const normalized = error.toLowerCase()
  if (
    normalized.includes('api key')
    || normalized.includes('authentication')
    || normalized.includes('unauthorized')
    || normalized.includes('401')
    || normalized.includes('403')
  ) {
    return {
      title: 'AI 接口认证失败',
      detail: '请到“配置 → AI 设置”检查 API Key、Base URL 和模型名称，保存后点击“测试连接”。',
    }
  }
  if (
    normalized.includes('chrome')
    || normalized.includes('cdp')
    || normalized.includes('websocket')
    || normalized.includes('browser runtime')
    || normalized.includes('not connected')
  ) {
    return {
      title: 'Google Chrome 连接中断',
      detail: '请确认 Google Chrome 正在运行且已开启远程调试，再点击上方“重新检查”。',
    }
  }
  if (normalized.includes('zhipin') || normalized.includes('登录') || normalized.includes('login')) {
    return {
      title: '招聘平台页面或登录状态异常',
      detail: '请在已连接的 Google Chrome 中打开 BOSS 直聘并确认账号仍处于登录状态。',
    }
  }
  return {
    title: '任务运行失败',
    detail: '请查看原始错误；修复配置或连接问题后，重新运行启动检查。',
  }
}

interface DashboardPageProps {
  view?: DashboardView
}

interface PreflightCheck {
  id: string
  title: string
  status: 'pass' | 'warning' | 'error'
  message: string
  detail: string
  action?: 'config' | 'browser' | ''
}

// 流水线管道语义：①采集 → ②评分 → ③确认投递（打招呼合一）→ ④发送 → ⑤监测与发简历
//（③确认是"今日待确认+待发送招呼语"人工环节，无独立任务卡片；⑤的简历由监测触发）
const modes: Array<{ mode: WorkbenchMode; title: string; description: string; stage: string }> = [
  {
    mode: 'full',
    title: '全流程常驻（可挂机）',
    description: '采集+评分+监测 三线程并行：持续采集写待评分池、评分 FIFO 消费、监听 HR 回复；常驻运行直到手动停止',
    stage: '①②⑤',
  },
  {
    mode: 'collect',
    title: '采集',
    description: '持续采集岗位写入待评分池，可与发送/评分/监测并行，不评分',
    stage: '①',
  },
  {
    mode: 'score',
    title: '评分',
    description: '持续消费待评分池评分（FIFO，无岗位时空闲等待），通过者自动生成招呼语；可与发送/采集/监测并行，常驻运行直到手动停止',
    stage: '②',
  },
  {
    mode: 'monitor',
    title: '监测',
    description: '监测已投递岗位的 HR 回复/要简历；可与发送/采集/评分并行，常驻运行直到手动停止',
    stage: '⑤',
  },
]

// 管道状态条：实时漏斗（采集 → 初筛 → 评分 → 确认 → 发送），颜色随环节推进由浅到深
// 注：①②③④⑤ 为工作台 5 段结构（③=确认投递/打招呼合一，⑤=监测与发简历），
// 状态条仍沿用后端累计漏斗 5 项（采集 → 初筛 → AI评分 → 人工确认 → 发送）
const PIPELINE_FUNNEL: Array<{ label: string; key: string }> = [
  { label: '采集', key: '采集总数' },
  { label: '初筛', key: '初筛通过' },
  { label: 'AI评分', key: 'AI评分' },
  { label: '确认', key: '人工确认' },
  { label: '发送', key: '发送' },
]

function PipelineStatusBar({ funnel }: { funnel: FunnelData }) {
  const head = funnel['采集总数'] || 0
  return (
    <div className="rounded-2xl border border-card-border bg-[#FFFCFA] p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs font-black tracking-[0.18em] text-primary">PIPELINE 管道状态</div>
        <div className="text-[10px] font-bold text-muted">采集 → 评分 → 确认 → 发送（累计漏斗）</div>
      </div>
      <div className="flex gap-1.5">
        {PIPELINE_FUNNEL.map((stage, index) => {
          const value = funnel[stage.key] || 0
          const ratio = head > 0 ? Math.min(1, value / head) : 0
          const intensity = 0.10 + ratio * 0.85
          const dark = intensity > 0.55
          return (
            <div
              key={stage.key}
              className="min-w-0 flex-1 overflow-hidden rounded-xl border border-card-border/60 px-2.5 py-2"
              style={{ backgroundColor: `rgba(251, 101, 17, ${Math.min(0.95, intensity).toFixed(3)})` }}
            >
              <div className={`truncate text-[10px] font-bold ${dark ? 'text-white/80' : 'text-primary'}`}>{stage.label}</div>
              <div className={`mt-0.5 text-lg font-black tabular-nums ${dark ? 'text-white' : 'text-foreground'}`}>{value}</div>
              <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-white/30">
                <div
                  className="h-full rounded-full bg-white/90"
                  style={{ width: `${(ratio * 100).toFixed(1)}%` }}
                />
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

const statItems = [
  { key: '采集总数', todayLabel: '今日新增岗位', totalLabel: '累计采集岗位' },
  { key: '初筛通过', todayLabel: '今日初筛通过', totalLabel: '累计初筛通过', highlight: true },
  { key: 'AI评分', todayLabel: '今日 AI 评分', totalLabel: '累计 AI 评分' },
  { key: 'pending', todayLabel: '当前待确认', totalLabel: '当前待确认', highlight: true, current: true },
  { key: '发送', todayLabel: '今日已投递', totalLabel: '累计已投递', highlight: true },
]

const METRIC_DEFS: Record<string, { key: string; label: string }> = {
  collect_seen: { key: 'collect_seen', label: '本轮扫描' },
  collect_new: { key: 'collect_new', label: '本轮新增' },
  collect_duplicate: { key: 'collect_duplicate', label: '重复岗位' },
  ai_completed: { key: 'ai_completed', label: '已评分' },
  ai_total: { key: 'ai_total', label: '待评分' },
  ai_passed: { key: 'ai_passed', label: 'AI通过' },
  ai_filtered: { key: 'ai_filtered', label: 'AI过滤' },
  ai_failed: { key: 'ai_failed', label: 'AI失败' },
  send_sent: { key: 'send_sent', label: '本轮已发送' },
  send_remaining: { key: 'send_remaining', label: '待发送' },
  send_failed: { key: 'send_failed', label: '本轮失败' },
  monitor_replied: { key: 'monitor_replied', label: 'HR新回复' },
  monitor_pending: { key: 'monitor_pending', label: '待回复' },
  monitor_rejected: { key: 'monitor_rejected', label: '已拒绝' },
  monitor_checks: { key: 'monitor_checks', label: '检查轮次' },
}

// 每环节只显示与自身相关的指标，避免满屏无意义的 0（视觉优化）
const MODE_METRIC_KEYS: Record<string, string[]> = {
  full: ['collect_seen', 'collect_new', 'ai_passed', 'ai_filtered', 'send_sent', 'send_remaining', 'send_failed'],
  collect: ['collect_seen', 'collect_new', 'collect_duplicate'],
  score: ['ai_completed', 'ai_total', 'ai_passed', 'ai_filtered', 'ai_failed'],
  rescore: ['ai_completed', 'ai_total', 'ai_passed', 'ai_filtered', 'ai_failed'],
  monitor: ['monitor_replied', 'monitor_pending', 'monitor_rejected', 'monitor_checks', 'send_sent', 'send_remaining', 'send_failed'],
  deliver: ['send_sent', 'send_remaining', 'send_failed'],
}

function visibleMetricItems(task: WorkbenchTask) {
  const keys = MODE_METRIC_KEYS[task.mode] || []
  return keys
    .map(key => METRIC_DEFS[key])
    .filter(def => def && task.metrics && def.key in task.metrics!)
}

function jobSubtitle(job: Job) {
  return [job.score ? `匹配 ${job.score}` : '', job.salary, job.hr_active || '活跃度未知', getStatusLabel(job.status)].filter(Boolean).join(' · ')
}

async function parsePreflightResponse(res: Response) {
  const rawText = await res.text()
  let data: { ok?: boolean; messages?: unknown; checks?: unknown; error?: string } = {}
  try {
    data = rawText ? JSON.parse(rawText) : {}
  } catch {
    const message = `无法解析预检响应：预检接口返回 ${res.status}`
    return {
      ok: false,
      messages: [message],
      checks: [{ id: 'preflight_api', title: '启动检查', status: 'error', message, detail: '请重启 JobWinner 后重试。' }] as PreflightCheck[],
    }
  }
  const messages = Array.isArray(data.messages) ? data.messages.map(String).filter(Boolean) : []
  const checks = Array.isArray(data.checks)
    ? data.checks.filter((item): item is PreflightCheck => Boolean(
      item
      && typeof item === 'object'
      && 'id' in item
      && 'status' in item
      && 'message' in item
    ))
    : []
  if (data.error) messages.push(String(data.error))
  if (!res.ok) messages.push(`预检接口返回 ${res.status}`)
  if (!data.ok && messages.length === 0) messages.push('后端未返回具体原因')
  if (checks.length === 0 && messages.length > 0) {
    checks.push(...messages.map((message, index) => ({
      id: `legacy-${index}`,
      title: '启动检查',
      status: 'error' as const,
      message,
      detail: '请按提示修复后重新检测。',
    })))
  }
  return { ok: Boolean(res.ok && data.ok), messages, checks }
}

function PipelineSectionHeader({
  seq,
  title,
  description,
  icon,
  right,
}: {
  seq: string
  title: string
  description: string
  icon?: ReactNode
  right?: ReactNode
}) {
  return (
    <div className="mb-4 flex flex-wrap items-start justify-between gap-4">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-9 w-9 shrink-0 flex-col items-center justify-center gap-0.5 rounded-xl bg-primary text-white">
          <span className="text-[9px] font-black leading-none">{seq}</span>
          {icon && <span className="flex">{icon}</span>}
        </span>
        <div>
          <h3 className="text-lg font-black">{title}</h3>
          <p className="mt-1 text-xs leading-5 text-muted">{description}</p>
        </div>
      </div>
      {right}
    </div>
  )
}

function PipelineJobCard({
  job,
  badge,
  badgeClass = 'bg-[#FFF0E5] text-primary',
  children,
}: {
  job: Job
  badge: string
  badgeClass?: string
  children: ReactNode
}) {
  return (
    <div className="rounded-2xl border border-card-border bg-[#FFFCFA] p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-black">{job.company}｜{job.title}</div>
          <div className="mt-1 text-xs text-muted">{jobSubtitle(job)}</div>
        </div>
        <span className={`shrink-0 rounded-full px-2 py-1 text-[11px] font-black ${badgeClass}`}>{badge}</span>
      </div>
      {children}
    </div>
  )
}

function PreflightPanel({
  checks,
  checking,
  onRetry,
}: {
  checks: PreflightCheck[]
  checking: boolean
  onRetry: () => void
}) {
  const actionableChecks = checks.filter(check => check.status !== 'pass')
  if (actionableChecks.length === 0) return null

  const errors = actionableChecks.filter(check => check.status === 'error').length
  const warnings = actionableChecks.filter(check => check.status === 'warning').length
  const needsConfig = actionableChecks.some(check => check.action === 'config')
  const heading = errors ? `启动检查发现 ${errors} 个问题` : `启动检查有 ${warnings} 项提醒`

  return (
    <div className={`mt-3 rounded-3xl border p-4 ${
      errors ? 'border-red-200 bg-red-50' : 'border-amber-200 bg-amber-50'
    }`}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          {errors
            ? <XCircle className="h-5 w-5 text-danger" />
            : <AlertTriangle className="h-5 w-5 text-amber-600" />}
          <div className="text-sm font-black text-foreground">{heading}</div>
        </div>
        <div className="flex items-center gap-2">
          {needsConfig && (
            <Button variant="secondary" size="sm" onClick={() => window.location.assign('/config')}>
              打开配置
            </Button>
          )}
          <Button variant="secondary" size="sm" onClick={onRetry} disabled={checking}>
            <RefreshCw className={`mr-2 h-4 w-4 ${checking ? 'animate-spin' : ''}`} />
            {checking ? '检查中' : '重新检查'}
          </Button>
        </div>
      </div>
      <div className="mt-3 grid gap-2 lg:grid-cols-2">
        {actionableChecks.map(check => {
          const isError = check.status === 'error'
          return (
            <div
              key={`${check.id}-${check.title}`}
              className={`rounded-2xl border bg-white px-3 py-3 ${isError ? 'border-red-200' : 'border-amber-200'}`}
            >
              <div className="flex items-start gap-2">
                {isError
                  ? <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger" />
                  : <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />}
                <div>
                  <div className="text-xs font-black text-muted">{check.title}</div>
                  <div className="mt-0.5 text-sm font-black text-foreground">{check.message}</div>
                  <p className="mt-1 text-xs leading-5 text-muted">{check.detail}</p>
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default function DashboardPage({ view = 'workbench' }: DashboardPageProps) {
  const {
    workbench,
    history,
    loading,
    error,
    refreshing,
    lastRefreshedAt,
    refresh,
    startTask,
    stopTask,
  } = useDashboard(view)
  const [selected, setSelected] = useState<string[]>([])
  const [notice, setNotice] = useState('')
  const [preflightChecks, setPreflightChecks] = useState<PreflightCheck[]>([])
  const [preflightMode, setPreflightMode] = useState<WorkbenchMode>('full')
  const [selectedJob, setSelectedJob] = useState<Job | null>(null)
  const [modePending, setModePending] = useState<WorkbenchMode | null>(null)
  const [confirmedDeliveryIds, setConfirmedDeliveryIds] = useState<Set<string>>(new Set())
  const [todayFilters, setTodayFilters] = useState<JobFilters>({ ...EMPTY_JOB_FILTERS })
  const [statsScope, setStatsScope] = useState<StatsScope>('today')
  const [editGreetingJob, setEditGreetingJob] = useState<Job | null>(null)
  const [editGreetingText, setEditGreetingText] = useState('')
  const [editGreetingSaving, setEditGreetingSaving] = useState(false)
  const [editGreetingPolishing, setEditGreetingPolishing] = useState(false)

  const todayJobs = useMemo(
    () => workbench.pending_confirmation.filter(job => !confirmedDeliveryIds.has(job.id)),
    [workbench.pending_confirmation, confirmedDeliveryIds]
  )
  const debouncedTodayQuery = useDebouncedValue(todayFilters.query, 250)
  const effectiveTodayFilters = useMemo(
    () => ({ ...todayFilters, query: debouncedTodayQuery }),
    [todayFilters, debouncedTodayQuery]
  )
  const filteredTodayJobs = useMemo(
    () => filterJobs(todayJobs, effectiveTodayFilters),
    [todayJobs, effectiveTodayFilters]
  )
  const pendingGreetingJobs = workbench.pending_greetings
  // Unified selection pool: jobs awaiting confirmation + greeting-ready jobs
  const visibleJobIds = useMemo(() => new Set([...filteredTodayJobs.map(job => job.id), ...pendingGreetingJobs.map(job => job.id)]), [filteredTodayJobs, pendingGreetingJobs])
  const greetingReadyIds = useMemo(() => new Set(pendingGreetingJobs.map(job => job.id)), [pendingGreetingJobs])
  const actionableSelected = useMemo(() => selected.filter(id => visibleJobIds.has(id)), [selected, visibleJobIds])

  useEffect(() => {
    setSelected(previous => {
      const next = previous.filter(id => visibleJobIds.has(id))
      return next.length === previous.length ? previous : next
    })
  }, [visibleJobIds])
  const resumePendingJobs = [...(workbench.resume_pending ?? []), ...(workbench.needs_resume ?? [])]
  const resumeItems = resumePendingJobs.filter((job, index, arr) => arr.findIndex(candidate => candidate.id === job.id) === index)
  const totalConfirmCount = todayJobs.length + pendingGreetingJobs.length
  const pendingScoreCount = Math.max(0, (workbench.funnel['采集总数'] || 0) - (workbench.funnel['初筛通过'] || 0) - (workbench.funnel['AI评分'] || 0))
  const activeTask = workbench.task
  const visibleTask = activeTask || workbench.last_task
  const activeTasks = workbench.active_tasks?.length ? workbench.active_tasks : (activeTask ? [activeTask] : [])
  // 状态区展示所有活跃任务；无活跃任务时回退显示最近一次“已失败”的任务（错误信息需要可见），
  // 不回退显示 stopped/completed，避免“停止后仍显示运行中”的误导
  const lastFailedTask = (!activeTasks.length && workbench.last_task?.status === 'failed') ? [workbench.last_task] : []
  const statusTasks = activeTasks.length ? activeTasks : lastFailedTask
  // 正在发送的宿主任务（监测/全流程/monitor 消费发送队列时，metrics 带 send_total/send_remaining）
  const sendingTask = activeTasks.find(t =>
    t.metrics && (Number(t.metrics.send_total) > 0 || Number(t.metrics.send_remaining) > 0)
  )
  const sendPhase = sendingTask?.metrics?.send_phase as string | undefined
  // 有独立“发送”任务卡时，卡片自身已展示发送进度；顶部横幅只用于“发送被内嵌在监测/全流程里”时兜底，
  // 避免出现两个发送状态UI
  const hasDeliverTask = activeTasks.some(t => t.mode === 'deliver' && (t.status === 'running' || t.status === 'stopping'))
  const showSendBanner = !hasDeliverTask && (sendingTask?.mode !== 'deliver')
  const sendProgress = showSendBanner && sendingTask?.metrics
    ? {
        sent: Number(sendingTask.metrics.send_sent || 0),
        remaining: Number(sendingTask.metrics.send_remaining || 0),
        failed: Number(sendingTask.metrics.send_failed || 0),
        total: Number(sendingTask.metrics.send_total || 0),
        phase: sendPhase || (Number(sendingTask.metrics.send_remaining || 0) > 0 ? 'sending' : 'done'),
      }
    : null
  // 已进入发送环节（锁定·正在发送）的岗位 id 集合：从所有活跃任务的发送队列汇总
  const sendingJobIds = useMemo(() => {
    const ids = new Set<string>()
    for (const t of activeTasks) {
      for (const jobId of (t.sending_job_ids || [])) ids.add(jobId)
    }
    return ids
  }, [activeTasks])
  // 某模式是否正在运行/停止（支持并行：遍历所有活跃任务，而非只看第一个）
  const isModeRunning = (mode: WorkbenchMode) =>
    activeTasks.some(t => t.mode === mode && (t.status === 'running' || t.status === 'stopping'))
  // Parallelism model: every mode is its own group, so collect/score/monitor/
  // deliver can all run concurrently and independently. Only the same mode
  // twice, or full (which bundles everything) alongside anything, is blocked.
  const MODE_GROUPS_FRONT: Record<string, string> = {
    deliver: 'deliver',
    collect: 'collect',
    score: 'score',
    rescore: 'rescore',
    monitor: 'monitor',
    full: 'full',
  }
  const sameGroupActive = (mode: WorkbenchMode): boolean => {
    // 并行模型下每一环节独立成组：仅当目标 mode 与任一活跃任务同组才算冲突
    const group = MODE_GROUPS_FRONT[mode]
    return activeTasks.some(t => {
      if (t.status !== 'running' && t.status !== 'stopping') return false
      if (t.mode === mode) return true
      return MODE_GROUPS_FRONT[t.mode] === group
    })
  }
  const modeIsActive = (mode: WorkbenchMode) =>
    activeTasks.some(t => t.mode === mode && (t.status === 'running' || t.status === 'stopping'))
  const visibleTaskError = visibleTask?.error ? taskErrorFeedback(visibleTask.error) : null
  const pendingReplies = history.filter(item => item.action === 'reply_pending')

  const toggleJob = (id: string) => {
    setSelected(prev => (prev.includes(id) ? prev.filter(item => item !== id) : [...prev, id]))
  }

  const runPreflight = async (mode: WorkbenchMode) => {
    setPreflightMode(mode)
    const res = await fetch(`/api/workbench/preflight?mode=${mode}`)
    const data = await parsePreflightResponse(res)
    setPreflightChecks(data.checks)
    if (!data.ok) {
      setNotice('请按提示处理后再启动')
      return false
    }
    return true
  }

  const handleModeClick = async (mode: WorkbenchMode) => {
    try {
      const runningForMode = activeTasks.find(t => t.mode === mode && (t.status === 'running' || t.status === 'stopping'))
      if (runningForMode) {
        if (runningForMode.status === 'stopping') {
          setNotice(`当前${runningForMode.label}正在停止，请等待后台完全结束。`)
          return
        }
        if (window.confirm(`是否停止当前${runningForMode.label}任务？已入库岗位会保留。`)) {
          setModePending(mode)
          setNotice(`正在停止${runningForMode.label}...`)
          await stopTask(runningForMode.id)
          setNotice(`${runningForMode.label}已请求停止。`)
        }
        return
      }
      if (modePending) return
      if (sameGroupActive(mode)) {
        const blocking = activeTasks.find(t => (t.status === 'running' || t.status === 'stopping') && (
          t.mode === mode || MODE_GROUPS_FRONT[t.mode] === MODE_GROUPS_FRONT[mode]
        ))
        setNotice(
          blocking?.status === 'stopping'
            ? `当前${blocking.label}正在停止，请等待后台完全结束后再启动同类型任务。`
            : `当前正在运行${blocking?.label || '同类型任务'}，请先停止它再启动同类型任务；其他环节（如发送/采集/评分）可以与它并行。`
        )
        return
      }
      const target = modes.find(item => item.mode === mode)
      setModePending(mode)
      setNotice(`${target?.title || '任务'}启动前预检中...`)
      if (!(await runPreflight(mode))) return
      setNotice(`${target?.title || '任务'}启动中，请稍候...`)
      await startTask(mode)
      setNotice(`${target?.title || '任务'}已启动，日志会在下方更新。`)
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '操作失败')
    } finally {
      setModePending(null)
    }
  }

  const handleStopTask = async (task: WorkbenchTask) => {
    if (!window.confirm(`是否停止${task.label}？已入库岗位会保留，其他并行任务不受影响。`)) return
    try {
      setModePending(task.mode as WorkbenchMode)
      setNotice(`正在停止${task.label}...`)
      await stopTask(task.id)
      setNotice(`${task.label}已请求停止。`)
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '停止失败')
    } finally {
      setModePending(null)
    }
  }

  const retryPreflight = async () => {
    if (modePending) return
    try {
      setModePending(preflightMode)
      setNotice('正在重新检查运行环境...')
      const ok = await runPreflight(preflightMode)
      setNotice(ok ? '' : '仍有问题需要处理，请查看检查结果。')
    } catch {
      setNotice('重新检查失败，请确认 JobWinner 后端仍在运行。')
    } finally {
      setModePending(null)
    }
  }

  // Unified delivery for the confirmation section: pending-confirm jobs go
  // through normal delivery (greeting generated if needed), greeting-ready
  // jobs are sent directly (direct_send) without regenerating.
  const deliverSelection = async (ids: string[]) => {
    if (!ids.length) return
    const confirmIds = ids.filter(id => !greetingReadyIds.has(id))
    const readyIds = ids.filter(id => greetingReadyIds.has(id))
    if (confirmIds.length) await confirmDeliver(confirmIds)
    if (readyIds.length) await sendReadyGreetings(readyIds)
  }

  const confirmDeliver = async (ids: string[]) => {
    if (!ids.length) return
    const count = ids.length
    if (!window.confirm(`是否投递以下 ${count} 个岗位？确认后将进入投递/打招呼流程。`)) return
    try {
      const res = await fetch('/api/workbench/deliver', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_ids: ids }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || '投递失败')
      }
      const data = await res.json().catch(() => ({}))
      if (!ids.some(id => workbench.send_errors.some(job => job.id === id))) {
        setConfirmedDeliveryIds(prev => new Set([...prev, ...ids]))
      }
      await refresh()
      setNotice(
        data.already_queued_count === count
          ? `所选 ${count} 个岗位已在发送队列中，等待依次发送。`
          : data.queued_count
            ? `已将 ${data.queued_count} 个岗位加入发送环节，正在依次发送。`
            : `已进入发送环节 ${count} 个岗位，状态区实时显示“正在发送 / 本轮已发送”。`
      )
      setSelected(prev => prev.filter(id => !new Set(ids).has(id)))
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '投递失败')
    }
  }

  const rejectSelectedJobs = async (ids: string[]) => {
    if (!ids.length) return
    const count = ids.length
    if (!window.confirm(`确定放弃这 ${count} 个岗位吗？放弃后不会进入投递，可在岗位池中查看已拒绝状态。`)) return
    try {
      const res = await fetch('/api/workbench/reject', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_ids: ids }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || '放弃失败')
      }
      const rejectedIds = new Set(ids)
      setSelected(prev => prev.filter(id => !rejectedIds.has(id)))
      setConfirmedDeliveryIds(prev => new Set([...prev, ...ids]))
      await refresh()
      setNotice(`已放弃 ${count} 个岗位。`)
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '放弃失败')
    }
  }

  const sendReadyGreetings = async (ids: string[]) => {
    if (!ids.length) return
    const count = ids.length
    try {
      const res = await fetch('/api/workbench/deliver', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_ids: ids, direct_send: true }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || '发送失败')
      }
      const data = await res.json().catch(() => ({}))
      await refresh()
      setNotice(
        data.already_queued_count === count
          ? `所选 ${count} 个岗位已在当前发送队列中，请等待依次发送。`
          : data.queued_count
            ? `已将 ${data.queued_count} 个岗位追加到当前发送队列。`
            : `已直接进入发送流程 ${count} 个岗位。`
      )
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '发送失败')
    }
  }

  const polishGreetingText = async () => {
    if (editGreetingPolishing) return
    const text = editGreetingText.trim()
    if (!text) {
      setNotice('先输入内容再润色')
      return
    }
    try {
      setEditGreetingPolishing(true)
      const res = await fetch('/api/greeting/polish', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ greeting: text }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || '润色失败')
      }
      const data = await res.json().catch(() => ({}))
      if (data.polished) setEditGreetingText(data.polished)
      setNotice('已按你的内容润色，可继续编辑后保存。')
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '润色失败')
    } finally {
      setEditGreetingPolishing(false)
    }
  }

  const openGreetingEditor = (job: Job) => {
    setEditGreetingJob(job)
    setEditGreetingText(job.greeting || '')
  }

  const closeGreetingEditor = () => {
    if (editGreetingSaving) return
    setEditGreetingJob(null)
    setEditGreetingText('')
  }

  const saveGreeting = async () => {
    if (!editGreetingJob) return
    const text = editGreetingText.trim()
    if (!text) {
      setNotice('招呼语不能为空')
      return
    }
    try {
      setEditGreetingSaving(true)
      const url = '/api/jobs/' + editGreetingJob.id + '/greeting'
      const res = await fetch(url, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ greeting: text }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || '保存失败')
      }
      setNotice('招呼语已更新，发送时将使用修改后的内容。')
      setEditGreetingJob(null)
      setEditGreetingText('')
      await refresh()
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '保存失败')
    } finally {
      setEditGreetingSaving(false)
    }
  }

  const openJobDetail = async (job: Job) => {
    try {
      const res = await fetch(`/api/jobs/${job.id}`)
      if (!res.ok) throw new Error('读取岗位详情失败')
      setSelectedJob(await res.json())
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '读取岗位详情失败')
    }
  }

  const downloadResume = (job: Job) => {
    window.open(`/api/jobs/${job.id}/resume/download`, '_blank')
  }

  const confirmSendResume = async (job: Job) => {
    if (!window.confirm(`确认向 ${job.company}｜${job.title} 发送定制简历吗？确认后系统会代为发送，并从待确认列表移除。`)) return
    try {
      const res = await fetch(`/api/jobs/${job.id}/confirm-send-resume`, { method: 'POST' })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || '确认发送失败')
      }
      await refresh()
      setNotice(`已确认发送 ${job.company}｜${job.title} 的定制简历。`)
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '确认发送失败')
    }
  }

  if (loading) {
    return <div className="flex h-full items-center justify-center text-sm text-muted">加载中...</div>
  }

  if (view === 'jobs') {
    return <JobsPoolView />
  }

  if (view === 'monitor') {
    return <MonitorExecutionView history={history} refresh={refresh} />
  }

  return (
    <div className="space-y-5">
      <section id="today-workbench" className="scroll-mt-6 rounded-3xl border border-card-border bg-white p-5 shadow-sm">
        <div className="flex items-start justify-between gap-4 mb-4">
          <div>
            <div className="text-xs font-black tracking-[0.18em] text-primary">TODAY WORKBENCH</div>
            <h2 className="mt-1 text-3xl font-black tracking-tight">今日求职行动</h2>
          </div>
          <div className="flex items-center gap-2">
            <div className="text-right">
              <Button variant="secondary" size="sm" onClick={refresh} disabled={refreshing}>
                <RefreshCw className={cn('mr-2 h-4 w-4', refreshing && 'animate-spin')} />
                {refreshing ? '刷新中' : '刷新'}
              </Button>
              {lastRefreshedAt && (
                <div className="mt-1 text-[10px] text-muted">
                  最后刷新：{lastRefreshedAt.toLocaleTimeString('zh-CN', { hour12: false })}
                </div>
              )}
            </div>
            <span className="rounded-full bg-[#FFF0E5] px-3 py-2 text-xs font-black text-primary">
              {activeTasks.length ? `${activeTasks.length} 个任务运行中` : '当前空闲'}
            </span>
          </div>
        </div>


        <PipelineStatusBar funnel={workbench.funnel} />

        {/* 任务状态卡已下放到各功能区（采集/评分/发送/监测），此处不再重复展示 */}
      </section>

      <section>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-lg font-black">求职数据</h3>
            <p className="mt-0.5 text-xs text-muted">今日看行动节奏，累计看岗位池沉淀。</p>
          </div>
          <div className="inline-flex rounded-full border border-card-border bg-white p-1">
            {([
              { value: 'today' as const, label: '今日数据' },
              { value: 'total' as const, label: '累计数据' },
            ]).map(option => (
              <button
                key={option.value}
                type="button"
                onClick={() => setStatsScope(option.value)}
                className={`rounded-full px-3 py-1.5 text-xs font-black transition ${
                  statsScope === option.value ? 'bg-primary text-white shadow-sm' : 'text-muted hover:text-primary'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          {statItems.map(item => {
            const currentValue = workbench.pending_confirmation.length
            const selectedFunnel = statsScope === 'today' ? workbench.funnel_today : workbench.funnel
            const alternateFunnel = statsScope === 'today' ? workbench.funnel : workbench.funnel_today
            const value = item.current ? currentValue : (selectedFunnel[item.key] || 0)
            const supportingText = item.current
              ? '实时待处理数量'
              : `${statsScope === 'today' ? '累计' : '今日'} ${alternateFunnel[item.key] || 0}`
            return (
              <div key={item.key} className="rounded-2xl border border-card-border bg-white p-4">
                <div className="text-xs text-muted">{statsScope === 'today' ? item.todayLabel : item.totalLabel}</div>
                <div className={`mt-1 text-2xl font-black ${item.highlight ? 'text-primary' : 'text-foreground'}`}>
                  {value}
                </div>
                <div className="mt-1 text-[10px] font-bold text-muted">{supportingText}</div>
              </div>
            )
          })}
        </div>
      </section>

      {/* 工作流 —— 模式开关一行 + 任务状态区一行（状态卡带模式标签对应） */}
      <section className="rounded-3xl border border-card-border bg-white p-5">
        <PipelineSectionHeader
          seq="①"
          title="工作流"
          description="按需启动各环节任务，任务可并行互不干涉；下方实时显示各环节运行状态。"
        />
        {/* 模式开关行：四卡等高（发送不在此列——发送由「一键投递/确认」区触发，仅以任务状态区展示运行状态） */}
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4">
          {modes.map(item => {
            const isActive = modeIsActive(item.mode)
            const isPending = modePending === item.mode
            // full 与所有环节互斥：full 运行→其他卡禁用；其他任务运行→full 卡禁用；同组（同环节）运行→该卡禁用（此时 isActive 为真，故不影响已运行卡）
            const anyNonFullRunning = activeTasks.some(t => t.mode !== 'full' && (t.status === 'running' || t.status === 'stopping'))
            const fullRunning = activeTasks.some(t => t.mode === 'full' && (t.status === 'running' || t.status === 'stopping'))
            const disabled = !isActive && !isPending && (
              (item.mode === 'full' && anyNonFullRunning)
              || (item.mode !== 'full' && (fullRunning || sameGroupActive(item.mode)))
            )
            const modeBadge = item.mode === 'score' ? `待评分池 ${pendingScoreCount} 个` : item.mode === 'monitor' ? `待确认 ${resumeItems.length} 个` : ''
            return (
              <button
                key={item.mode}
                onClick={() => handleModeClick(item.mode)}
                aria-disabled={disabled}
                className={`flex h-full min-h-[120px] flex-col rounded-3xl p-4 text-left transition ${
                  isActive
                    ? 'border-2 border-primary bg-primary text-white shadow-xl shadow-primary/20'
                    : isPending
                      ? 'border-2 border-primary/70 bg-[#FFF0E5] text-primary shadow-md'
                      : disabled
                        ? 'cursor-not-allowed border border-card-border bg-white text-muted opacity-45'
                        : 'border border-card-border bg-[#FFFCFA] text-foreground hover:border-primary/60 hover:shadow-md'
                }`}
              >
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className={`rounded-lg px-1.5 py-0.5 text-[11px] font-black ${isActive ? 'bg-white/20 text-white' : isPending ? 'bg-primary/10 text-primary' : 'bg-[#FFF0E5] text-primary'}`}>
                      {item.stage}
                    </span>
                    <div className="text-sm font-black leading-5">
                      {isPending
                        ? isActive ? '任务停止中' : '任务启动中'
                        : isActive ? `${item.title}中` : item.title}
                    </div>
                  </div>
                  {isActive
                    ? <Square className="h-4 w-4 shrink-0 fill-current" />
                    : isPending
                      ? <span className="h-4 w-4 animate-spin rounded-full border-2 border-primary/40 border-t-primary" />
                      : <Play className="h-4 w-4 shrink-0" />}
                </div>
                <p className={`flex-1 text-[11px] leading-5 ${isActive ? 'text-white/85' : isPending ? 'text-primary' : 'text-muted'}`}>{item.description}</p>
                {modeBadge && !isActive && <div className="mt-2 inline-block self-start rounded-full bg-[#FFF0E5] px-2 py-0.5 text-[11px] font-black text-primary">{modeBadge}</div>}
              </button>
            )
          })}
        </div>
        {/* 评分说明：FIFO 消费规则（整合自原评分区说明） */}
        <div className="mt-4 flex items-start gap-2 rounded-2xl border border-card-border bg-[#FFFCFA] p-3 text-xs leading-6 text-muted">
          <Star className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
          <span>AI 按岗位库 FIFO 依次评分子，通过者自动生成招呼语并进入“确认投递”。当前待评分池 = 采集总数 − 初筛通过 − AI评分（{workbench.funnel['采集总数'] || 0} − {workbench.funnel['初筛通过'] || 0} − {workbench.funnel['AI评分'] || 0}）。</span>
        </div>
        {/* 发送窗口提示 */}
        {workbench.send_window && (
          <div className={"mt-2 flex items-start gap-2 rounded-2xl border px-3 py-2.5 text-xs leading-5 " + (workbench.send_window.active ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-amber-200 bg-amber-50 text-amber-700")}>
            <Clock className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              发送窗口 {workbench.send_window.windows.join("、")}，当前{workbench.send_window.active ? "在窗口内，可正常发送" : "不在窗口内，发送任务会保留队列、到窗口后自动发出"}。
              {!workbench.send_window.active && workbench.send_window.next && <span className="font-bold">{workbench.send_window.next}</span>}
            </span>
          </div>
        )}
        {/* 发送进行中：确认投递后实时展示“模拟浏览 / 正在发送 / 本轮已发送 / 待发送” */}
        {sendProgress && (
          <div className="mt-4 rounded-3xl border-2 border-primary/40 bg-[#FFF7F0] p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex items-center gap-2">
                <Send className="h-4 w-4 text-primary" />
                <div className="text-sm font-black">发送环节</div>
                {sendProgress.remaining > 0 ? (
                  <span className="rounded-full bg-primary/10 px-2.5 py-0.5 text-[11px] font-black text-primary">
                    {sendProgress.phase === 'browsing'
                      ? '模拟浏览中'
                      : sendProgress.phase === 'opening'
                        ? '打开岗位页中'
                        : '正在输入发送'}
                  </span>
                ) : (
                  <span className="rounded-full bg-emerald-50 px-2.5 py-0.5 text-[11px] font-black text-emerald-600">本轮已完成</span>
                )}
              </div>
              <div className="text-xs font-bold text-muted">
                本轮已发送 <span className="text-primary">{sendProgress.sent}</span> / {sendProgress.total}
                {sendProgress.remaining > 0 && <> · 待发送 <span className="text-primary">{sendProgress.remaining}</span></>}
                {sendProgress.failed > 0 && <> · 失败 <span className="text-danger">{sendProgress.failed}</span></>}
              </div>
            </div>
            <div className="mt-3 h-2 overflow-hidden rounded-full bg-primary/15">
              <div
                className="h-full rounded-full bg-primary transition-all duration-500"
                style={{ width: `${sendProgress.total ? Math.min(100, (sendProgress.sent / sendProgress.total) * 100) : 0}%` }}
              />
            </div>
            <p className="mt-2 text-xs leading-5 text-muted">
              {sendProgress.remaining > 0
                ? '模拟人类节奏：打开岗位页阅览片刻 → 输入打招呼语 → 发送，逐条进行；下方岗位列表会显示“锁定·正在发送”。'
                : '本轮发送已完成，已发送岗位不重复锁定。发送全程不影响并行任务。'}
            </p>
          </div>
        )}
        {/* 任务状态区：全宽展示所有运行中任务（状态卡头部带模式标签对应）；任务失败时保留展示错误以便查看 */}
        {statusTasks.length > 0 && (
          <div className="mt-4 flex items-center gap-2">
            <span className="text-sm font-black">任务运行状态</span>
            {activeTasks.length > 0 ? (
              <span className="rounded-full bg-[#FFF0E5] px-2.5 py-1 text-[11px] font-black text-primary">{statusTasks.length} 个任务运行中</span>
            ) : (
              <span className="rounded-full bg-red-50 px-2.5 py-1 text-[11px] font-black text-danger">最近任务运行失败</span>
            )}
          </div>
        )}
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {statusTasks.map(task => (
            <TaskStatusCard key={task.id} task={task} onStop={() => handleStopTask(task)} />
          ))}
        </div>
        {notice && <div className="mt-3 rounded-2xl bg-[#FFF0E5] px-4 py-3 text-sm text-primary">{notice}</div>}
        {preflightChecks.some(check => check.status !== 'pass') && (
          <PreflightPanel checks={preflightChecks} checking={Boolean(modePending)} onRetry={retryPreflight} />
        )}
        {error && <div className="mt-3 rounded-2xl bg-red-50 px-4 py-3 text-sm text-danger">{error}</div>}
      </section>

      {/* ③ 确认投递（打招呼合一）—— 今日待确认 + 待发送招呼语 */}
      <section className="rounded-3xl border border-primary/20 bg-[#FFF0E5] p-5">
        <PipelineSectionHeader
          seq="③"
          title="确认投递"
          description="勾选岗位后一键投递（打招呼），已生成招呼语的岗位一并发出。"
          icon={<Send className="h-3.5 w-3.5" />}
          right={
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" size="sm" onClick={() => setSelected([...filteredTodayJobs.map(job => job.id), ...pendingGreetingJobs.map(job => job.id)])}>全选</Button>
              <Button variant="secondary" size="sm" onClick={() => setSelected([])}>清空</Button>
              <Button variant="secondary" size="sm" disabled={actionableSelected.length === 0} onClick={() => rejectSelectedJobs(actionableSelected)}>放弃已选 {actionableSelected.length} 个</Button>
              <Button size="sm" disabled={actionableSelected.length === 0} onClick={() => deliverSelection(actionableSelected)}>一键投递已选 {actionableSelected.length} 个</Button>
            </div>
          }
        />
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className="rounded-full bg-white px-3 py-1 text-[11px] font-black text-primary">待你确认 {todayJobs.length} 个</span>
          <span className="rounded-full bg-white px-3 py-1 text-[11px] font-black text-primary">已生成可发 {pendingGreetingJobs.length} 个</span>
          <span className="rounded-full bg-white px-3 py-1 text-[11px] font-black text-primary">合计 {totalConfirmCount} 个</span>
        </div>
        <JobFilterBar
          filters={todayFilters}
          onChange={setTodayFilters}
          onReset={() => setTodayFilters({ ...EMPTY_JOB_FILTERS })}
          resultCount={filteredTodayJobs.length}
          totalCount={todayJobs.length}
          invalidSalary={hasInvalidSalaryRange(todayFilters)}
        />
        {filteredTodayJobs.length ? (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {filteredTodayJobs.map(job => (
              <JobActionCard
                key={job.id}
                job={job}
                selected={selected.includes(job.id)}
                onToggle={() => toggleJob(job.id)}
                onDetail={() => openJobDetail(job)}
                onReject={() => rejectSelectedJobs([job.id])}
              />
            ))}
          </div>
        ) : todayJobs.length ? (
          <div className="rounded-2xl border border-dashed border-card-border bg-[#FFFCFA] p-5 text-center text-sm text-muted">
            <p>没有符合当前条件的岗位</p>
            <Button className="mt-3" variant="secondary" size="sm" onClick={() => setTodayFilters({ ...EMPTY_JOB_FILTERS })}>重置筛选</Button>
          </div>
        ) : (
          <div className="rounded-2xl border border-dashed border-card-border bg-[#FFFCFA] p-5 text-sm text-muted">今天暂时没有待确认岗位。</div>
        )}
        {pendingGreetingJobs.length > 0 && (
          <>
            <div className="mt-5 mb-3 text-sm font-black">已生成招呼语（勾选后随上方「一键投递」一起发出）{sendingJobIds.size > 0 && <span className="ml-1 text-xs font-bold text-primary">· 已锁定 {sendingJobIds.size} 个正在发送</span>}</div>
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              {pendingGreetingJobs.map(job => {
                const locked = sendingJobIds.has(job.id)
                return (
                <div key={job.id} className={"rounded-2xl border p-4 transition " + (locked ? 'border-primary/40 bg-[#FFF7F0]/70 opacity-80' : selected.includes(job.id) ? 'border-primary bg-[#FFF0E5]/40' : 'border-primary/20 bg-white')}>
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="font-black">{job.company}｜{job.title}</div>
                      <div className="mt-1 text-base font-black text-primary">{job.salary || '薪资未填'}</div>
                      <div className="mt-1 text-xs text-primary">{job.score ? `匹配 ${job.score} · ` : ''}{locked ? '正在发送，无需重复操作' : '已生成招呼语，等待发送'}</div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <input type="checkbox" checked={selected.includes(job.id)} disabled={locked} onChange={() => toggleJob(job.id)} className="h-4 w-4 accent-primary" title={locked ? '正在发送中，已锁定' : '勾选后可从上方批量投递'} />
                      {locked ? (
                        <span className="inline-flex items-center gap-1 rounded-full bg-primary px-2 py-1 text-[11px] font-black text-white"><Lock className="h-3 w-3" />锁定·正在发送</span>
                      ) : (
                        <span className="rounded-full bg-[#FFF0E5] px-2 py-1 text-[11px] font-black text-primary">待发送</span>
                      )}
                    </div>
                  </div>
                  <p className="mt-3 line-clamp-2 text-sm leading-6 text-muted">{job.greeting || '招呼语已生成，等待发送。'}</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button size="sm" disabled={locked} onClick={() => sendReadyGreetings([job.id])}>{locked ? '正在发送' : '发送招呼语'}</Button>
                    <Button size="sm" variant="secondary" disabled={locked} onClick={() => openGreetingEditor(job)}><Pencil className="mr-2 h-4 w-4" />编辑招呼语</Button>
                    <Button variant="secondary" size="sm" disabled={locked} onClick={() => rejectSelectedJobs([job.id])}>{locked ? '发送中' : '放弃'}</Button>
                    <Button variant="secondary" size="sm" onClick={() => openJobDetail(job)}><Eye className="mr-2 h-4 w-4" />查看详情</Button>
                    <Button variant="secondary" size="sm" disabled={!job.url} onClick={() => window.open(job.url, '_blank', 'noopener,noreferrer')}><ExternalLink className="mr-2 h-4 w-4" />跳转岗位链接</Button>
                  </div>
                </div>
                )
              })}
            </div>
          </>
        )}
      </section>

      {/* ④ 发送 —— 失败重试 */}
      {workbench.send_errors.length > 0 && (
        <section className="rounded-3xl border border-red-100 bg-red-50 p-5">
          <PipelineSectionHeader
            seq="④"
            title="发送"
            description="招呼语发送失败待处理。你可以重试，或放弃已失效岗位。"
            right={
              <div className="flex flex-wrap gap-2">
                <Button size="sm" onClick={() => confirmDeliver(workbench.send_errors.map(job => job.id))}>重新发送全部 {workbench.send_errors.length} 个</Button>
                <Button variant="secondary" size="sm" onClick={() => rejectSelectedJobs(workbench.send_errors.map(job => job.id))}>放弃全部</Button>
              </div>
            }
          />
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {workbench.send_errors.map(job => (
              <div key={job.id} className="rounded-2xl border border-red-100 bg-white p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="font-black">{job.company}｜{job.title}</div>
                    <div className="mt-1 text-sm font-black text-danger">{job.salary || '薪资未填'}</div>
                    <div className="mt-1 text-xs text-danger">最近失败原因：{job.last_error || '发送失败，等待重试'}</div>
                  </div>
                  <span className="rounded-full bg-red-50 px-2 py-1 text-[11px] font-black text-danger">发送失败</span>
                </div>
                <p className="mt-3 line-clamp-2 text-sm leading-6 text-muted">{job.greeting || '招呼语已生成，等待重新发送。'}</p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button size="sm" onClick={() => sendReadyGreetings([job.id])}>重新发送</Button>
                  <Button size="sm" variant="secondary" onClick={() => openGreetingEditor(job)}><Pencil className="mr-2 h-4 w-4" />编辑招呼语</Button>
                  <Button variant="secondary" size="sm" onClick={() => rejectSelectedJobs([job.id])}>放弃</Button>
                  <Button variant="secondary" size="sm" onClick={() => openJobDetail(job)}><Eye className="mr-2 h-4 w-4" />查看详情</Button>
                  <Button variant="secondary" size="sm" disabled={!job.url} onClick={() => window.open(job.url, '_blank', 'noopener,noreferrer')}><ExternalLink className="mr-2 h-4 w-4" />跳转岗位链接</Button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ⑤ 监测与发简历 —— HR 要简历时自动生成定制 PDF，等你确认后发送 */}
      <section className="rounded-3xl border border-card-border bg-white p-5">
        <PipelineSectionHeader
          seq="⑤"
          title="监测与发简历"
          description="监测发现 HR 要简历时自动生成定制 PDF，等你确认后发送。"
          icon={<FileText className="h-3.5 w-3.5" />}
          right={
            <span className="rounded-2xl border border-primary/20 bg-[#FFF0E5] px-4 py-2 text-sm font-black text-primary">
              待确认发送 {resumeItems.length} 个
            </span>
          }
        />
        {resumeItems.length ? (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {resumeItems.map(job => {
              const isPending = (workbench.resume_pending ?? []).some(item => item.id === job.id)
              return (
                <PipelineJobCard
                  key={job.id}
                  job={job}
                  badge={isPending ? '待确认发送' : '待发简历'}
                  badgeClass={isPending ? 'bg-[#FFF0E5] text-primary' : 'bg-white text-primary'}
                >
                  <p className="mt-2 text-sm leading-6 text-muted">
                    {isPending
                      ? '定制简历已生成，等待你确认发送。'
                      : 'HR 已请求简历，系统已准备定制化简历下载入口。'}
                  </p>
                  {job.resume_path && (
                    <div className="mt-3 flex items-center gap-2 rounded-xl border border-card-border bg-white px-3 py-2">
                      <FileText className="h-4 w-4 shrink-0 text-primary" />
                      <span className="truncate text-xs font-bold text-muted" title={job.resume_path}>
                        {job.resume_path.split(/[\\/]/).pop() || job.resume_path}
                      </span>
                    </div>
                  )}
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button size="sm" onClick={() => confirmSendResume(job)}><Send className="mr-2 h-4 w-4" />确认发送</Button>
                    <Button variant="secondary" size="sm" onClick={() => downloadResume(job)}><Download className="mr-2 h-4 w-4" />下载 PDF</Button>
                    <Button variant="secondary" size="sm" onClick={() => openJobDetail(job)}><Eye className="mr-2 h-4 w-4" />查看详情</Button>
                  </div>
                </PipelineJobCard>
              )
            })}
          </div>
        ) : (
          <div className="rounded-2xl border border-dashed border-card-border bg-[#FFFCFA] p-5 text-sm text-muted">当前没有待确认发送的定制简历。</div>
        )}
      </section>

      {selectedJob && <JobDetailModal job={selectedJob} onClose={() => setSelectedJob(null)} />}
      {editGreetingJob && (
        <GreetingEditorModal
          job={editGreetingJob}
          text={editGreetingText}
          saving={editGreetingSaving}
          polishing={editGreetingPolishing}
          onChange={setEditGreetingText}
          onPolish={polishGreetingText}
          onSave={saveGreeting}
          onClose={closeGreetingEditor}
        />
      )}
    </div>
  )
}

function JobActionCard({ job, selected, onToggle, onDetail, onReject }: { job: Job; selected: boolean; onToggle: () => void; onDetail: () => void; onReject: () => void }) {
  return (
    <div className={`rounded-2xl border p-4 ${selected ? 'border-primary bg-[#FFFCFA]' : 'border-card-border bg-[#FFFCFA]'}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-black">{job.company}｜{job.title}</div>
          <div className="mt-1 text-xs text-muted">{jobSubtitle(job)}</div>
        </div>
        <input type="checkbox" checked={selected} onChange={onToggle} className="mt-1 h-4 w-4 accent-primary" />
      </div>
      <p className="mt-3 line-clamp-2 text-sm leading-6 text-muted">{job.score_reason || job.greeting || '等待继续推进。'}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="secondary" size="sm" onClick={onDetail}><Eye className="mr-2 h-4 w-4" />查看详情</Button>
        <Button variant="secondary" size="sm" disabled={!job.url} onClick={() => window.open(job.url, '_blank', 'noopener,noreferrer')}><ExternalLink className="mr-2 h-4 w-4" />跳转岗位链接</Button>
        <Button variant="secondary" size="sm" onClick={onReject}><XCircle className="mr-2 h-4 w-4" />放弃岗位</Button>
      </div>
    </div>
  )
}

function JobDetailModal({ job, onClose }: { job: Job; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-6">
      <div className="max-h-[86vh] w-full max-w-3xl overflow-y-auto rounded-3xl border border-card-border bg-white p-6 shadow-2xl">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <div className="text-xs font-black tracking-[0.18em] text-primary">岗位详情</div>
            <h3 className="mt-1 text-2xl font-black">{job.company}｜{job.title}</h3>
            <p className="mt-1 text-sm text-muted">{job.salary || '薪资未填'} · {job.city || '城市未填'} · {getStatusLabel(job.status)}</p>
          </div>
          <Button variant="secondary" size="sm" onClick={onClose}>关闭</Button>
        </div>
        <div className="grid gap-3 text-sm lg:grid-cols-2">
          <InfoBlock label="HR" value={[job.hr_name, job.hr_title].filter(Boolean).join(' · ') || '-'} />
          <InfoBlock label="招聘者活跃" value={job.hr_active || '活跃度未知'} />
          <InfoBlock label="公司" value={[job.company_size, job.company_industry].filter(Boolean).join(' · ') || '-'} />
          <InfoBlock label="匹配分" value={String(job.score || '-')} />
          <InfoBlock label="定制简历" value={job.resume_path || '未生成'} />
        </div>
        <div className="mt-4 rounded-2xl border border-card-border bg-[#FFFCFA] p-4">
          <div className="text-sm font-black">评分理由</div>
          <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-muted">{job.score_reason || '-'}</p>
        </div>
        <div className="mt-4 rounded-2xl border border-card-border bg-[#FFFCFA] p-4">
          <div className="text-sm font-black">招呼语</div>
          <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-muted">{job.greeting || '未生成'}</p>
        </div>
        <div className="mt-4 rounded-2xl border border-card-border bg-[#FFFCFA] p-4">
          <div className="text-sm font-black">JD</div>
          <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-muted">{job.jd || '-'}</p>
        </div>
      </div>
    </div>
  )
}


function TaskStatusCard({ task, onStop }: { task: WorkbenchTask; onStop?: () => void }) {
  const taskError = task.error ? taskErrorFeedback(task.error) : null
  const metricItems = visibleMetricItems(task)
  const running = task.status === 'running' || task.status === 'stopping'
  return (
          <div key={task.id} className={"rounded-3xl border border-card-border bg-[#FFFCFA] p-4"}>
            <div className={"flex flex-wrap items-start justify-between gap-3"}>
              <div className="flex items-center gap-2">
                {running && (
                  <span className="relative flex h-2.5 w-2.5">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-60" />
                    <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-primary" />
                  </span>
                )}
                <div className={"text-sm font-black"}>{task.label}</div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {task.status === 'failed' && <span className="rounded-full bg-red-50 px-2 py-1 text-[11px] font-black text-danger">运行失败</span>}
                {onStop && running && (
                  <Button
                    variant="secondary" size="sm"
                    disabled={task.status === 'stopping'}
                    onClick={onStop}
                    title="仅停止此环节任务，其他并行任务不受影响"
                  >
                    <Square className="mr-1.5 h-3.5 w-3.5" />
                    {task.status === 'stopping' ? '停止中' : '停止'}
                  </Button>
                )}
              </div>
            </div>
            <div className={`mt-3 rounded-2xl border px-4 py-3 ${taskStatusClass(task.status)}`}>
              <div className={"flex items-center justify-between gap-2"}>
                <div className={"text-xs font-black tracking-wider text-primary"}>{taskStatusTitle(task.status)}</div>
                <div className={"rounded-full px-2 py-0.5 text-[11px] font-black " + (
                  task.status === 'failed'
                    ? 'bg-red-50 text-danger'
                    : task.status === 'stopping'
                      ? 'bg-amber-50 text-amber-600'
                      : running
                        ? 'bg-primary/10 text-primary'
                        : 'bg-[#F3F3F0] text-muted'
                )}>
                  {task.status === 'stopping' ? '正在停止' : taskStatusText(task.status)}
                </div>
              </div>
              <div className={"mt-2 text-sm font-black text-foreground"}>{currentTaskStage(task.logs)}</div>
              <TaskPipelineStages task={task} />
              {task.deadline_at && (
                <div className={"mt-1 text-xs font-bold text-muted"}>
                  自动截止：{new Date(task.deadline_at).toLocaleString('zh-CN', { hour12: false })}
                </div>
              )}
              {metricItems.length > 0 && (
                <div className={
                  "mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3 " +
                  (metricItems.length >= 5 ? 'lg:grid-cols-5' : metricItems.length >= 4 ? 'lg:grid-cols-4' : 'lg:grid-cols-3')
                }>
                  {metricItems.map(item => (
                    <div key={item.key} className={"rounded-xl border border-card-border bg-white px-3 py-2"}>
                      <div className={"text-[10px] font-bold text-muted"}>{item.label}</div>
                      <div className={"mt-0.5 text-lg font-black " + (Number(task.metrics?.[item.key] ?? 0) > 0 ? 'text-foreground' : 'text-muted')}>{task.metrics?.[item.key] ?? 0}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
            {task.error && taskError && (
              <div className={"mt-3 rounded-2xl border border-red-100 bg-red-50 px-4 py-3 text-sm text-danger"}>
                <div className={"font-black"}>{taskError.title}</div>
                <p className={"mt-1 text-xs leading-5"}>{taskError.detail}</p>
                <details className={"mt-2 text-xs text-muted"}>
                  <summary className={"cursor-pointer font-bold"}>查看原始错误</summary>
                  <pre className={"mt-2 whitespace-pre-wrap break-words rounded-lg bg-white p-2"}>{task.traceback || task.error}</pre>
                </details>
              </div>
            )}
            {task.stop_reason && <div className={"mt-3 rounded-2xl bg-[#FFF0E5] px-3 py-2 text-sm text-primary"}>{task.stop_reason}</div>}
          </div>
  )
}



function GreetingEditorModal({
  job,
  text,
  saving,
  polishing,
  onChange,
  onPolish,
  onSave,
  onClose,
}: {
  job: Job
  text: string
  saving: boolean
  polishing: boolean
  onChange: (value: string) => void
  onPolish: () => void
  onSave: () => void
  onClose: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-6">
      <div className="max-h-[86vh] w-full max-w-xl overflow-y-auto rounded-3xl border border-card-border bg-white p-6 shadow-2xl">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <div className="text-xs font-black tracking-[0.18em] text-primary">编辑招呼语</div>
            <h3 className="mt-1 text-xl font-black">{job.company}｜{job.title}</h3>
            <p className="mt-1 text-sm text-muted">发送前可自由修改，保存后发送时使用新内容。</p>
          </div>
          <Button variant="secondary" size="sm" onClick={onClose} disabled={saving}>关闭</Button>
        </div>
        <textarea
          value={text}
          onChange={e => onChange(e.target.value)}
          placeholder="输入发给 HR 的招呼语…"
          rows={8}
          className="w-full resize-y rounded-2xl border border-card-border bg-[#FFFCFA] p-4 text-sm leading-6 outline-none focus:border-primary/50"
        />
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <span className="text-xs text-muted">{text.length} 字（建议 50–150 字）</span>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" size="sm" onClick={onPolish} disabled={polishing || saving || !text.trim()}>
              <Sparkles className="mr-2 h-4 w-4" />
              {polishing ? '润色中…' : 'AI 润色'}
            </Button>
            <Button variant="secondary" size="sm" onClick={onClose} disabled={saving}>取消</Button>
            <Button size="sm" onClick={onSave} disabled={saving || !text.trim()}>
              {saving ? '保存中…' : '保存'}
            </Button>
          </div>
        </div>
      </div>
    </div>
  )
}
function InfoBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-card-border bg-[#FFFCFA] p-4">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-1 font-bold text-foreground">{value}</div>
    </div>
  )
}

function JobsPoolView() {
  const pageSize = 15
  const [page, setPage] = useState(0)
  const [filters, setFilters] = useState<JobFilters>({ ...EMPTY_JOB_FILTERS })
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [notice, setNotice] = useState('')
  const [showRecycleBin, setShowRecycleBin] = useState(false)
  const [showScoreDialog, setShowScoreDialog] = useState(false)
  const [recycleJobs, setRecycleJobs] = useState<Job[]>([])
  const [recycleSelectedIds, setRecycleSelectedIds] = useState<string[]>([])
  const [recycleLoading, setRecycleLoading] = useState(false)
  const [permanentDeleteIds, setPermanentDeleteIds] = useState<string[]>([])
  const [permanentDeleteAcknowledged, setPermanentDeleteAcknowledged] = useState(false)
  const { items, total, allTotal, loading, error, refresh: refreshJobs } = useJobSearch(filters, page, pageSize)

  useEffect(() => {
    setPage(0)
  }, [filters.query, filters.minScore, filters.salaryMin, filters.salaryMax, filters.status, filters.createdWithin])

  const toggleSelected = (jobId: string) => {
    setSelectedIds(previous => previous.includes(jobId) ? previous.filter(id => id !== jobId) : [...previous, jobId])
  }

  const allPageSelected = items.length > 0 && items.every(job => selectedIds.includes(job.id))
  const toggleCurrentPage = () => {
    const pageIds = new Set(items.map(job => job.id))
    setSelectedIds(previous => allPageSelected
      ? previous.filter(id => !pageIds.has(id))
      : [...new Set([...previous, ...pageIds])])
  }

  const loadRecycleBin = async () => {
    setRecycleLoading(true)
    try {
      const collected: Job[] = []
      let offset = 0
      const limit = 200
      while (true) {
        const res = await fetch(`/api/jobs?deleted=only&limit=${limit}&offset=${offset}`, { cache: 'no-store' })
        if (!res.ok) throw new Error(`回收站接口返回 ${res.status}`)
        const pageItems = await res.json()
        if (!Array.isArray(pageItems)) throw new Error('回收站响应格式无效')
        collected.push(...pageItems)
        const totalCount = Number(res.headers.get('X-Total-Count'))
        if (!pageItems.length || pageItems.length < limit || (Number.isFinite(totalCount) && collected.length >= totalCount)) break
        offset += pageItems.length
      }
      const unique = new Map(collected.map(job => [String(job.id), job]))
      setRecycleJobs([...unique.values()])
      setRecycleSelectedIds(previous => previous.filter(id => unique.has(id)))
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '读取回收站失败')
    } finally {
      setRecycleLoading(false)
    }
  }

  useEffect(() => {
    void loadRecycleBin()
  }, [])

  const postJobAction = async (path: string, payload: Record<string, unknown>) => {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      const blocked = Array.isArray(data.blocked)
        ? data.blocked.map((item: { job_id?: string; reasons?: string[] }) => `${item.job_id || '岗位'}：${(item.reasons || []).join('、')}`).join('；')
        : ''
      throw new Error([data.error || '岗位操作失败', blocked].filter(Boolean).join('；'))
    }
    return res.json()
  }

  const softDelete = async (jobIds: string[]) => {
    if (!jobIds.length || !window.confirm(`确认将 ${jobIds.length} 个岗位移入回收站吗？岗位不会永久删除。`)) return
    try {
      const result = await postJobAction('/api/jobs/soft-delete', { job_ids: jobIds, confirmed: true })
      setSelectedIds(previous => previous.filter(id => !jobIds.includes(id)))
      refreshJobs()
      await loadRecycleBin()
      setNotice(`已移入回收站 ${result.affected_count || 0} 条岗位。`)
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '移入回收站失败')
    }
  }

  const restoreJobs = async (jobIds: string[]) => {
    if (!jobIds.length || !window.confirm(`确认恢复 ${jobIds.length} 个岗位吗？恢复后不会自动评分或投递。`)) return
    try {
      const result = await postJobAction('/api/jobs/restore', { job_ids: jobIds, confirmed: true })
      setRecycleSelectedIds(previous => previous.filter(id => !jobIds.includes(id)))
      refreshJobs()
      await loadRecycleBin()
      setNotice(`已恢复 ${result.affected_count || 0} 条岗位。`)
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '恢复失败')
    }
  }

  const requestPermanentDelete = (jobIds: string[]) => {
    if (!jobIds.length) return
    setPermanentDeleteIds(jobIds)
    setPermanentDeleteAcknowledged(false)
  }

  const confirmPermanentDelete = async () => {
    if (!permanentDeleteIds.length || !permanentDeleteAcknowledged) return
    try {
      const result = await postJobAction('/api/jobs/permanent-delete', {
        job_ids: permanentDeleteIds,
        confirmed: true,
        confirmation: 'PERMANENT_DELETE',
      })
      setRecycleSelectedIds(previous => previous.filter(id => !permanentDeleteIds.includes(id)))
      setPermanentDeleteIds([])
      setPermanentDeleteAcknowledged(false)
      await loadRecycleBin()
      setNotice(`已永久删除 ${result.affected_count || 0} 条岗位。`)
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '永久删除失败')
    }
  }

  const exportJobs = async (format: 'xlsx' | 'csv', scope: 'all' | 'filtered' | 'selected') => {
    try {
      const res = await fetch('/api/jobs/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          format,
          scope,
          job_ids: scope === 'selected' ? selectedIds : [],
          filters: scope === 'filtered' ? {
            q: filters.query.trim(),
            min_score: filters.minScore,
            salary_min: filters.salaryMin,
            salary_max: filters.salaryMax,
            status: filters.status,
            created_within: filters.createdWithin,
          } : {},
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || '导出失败')
      }
      const blob = await res.blob()
      const disposition = res.headers.get('Content-Disposition') || ''
      const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || `jobwinner-jobs.${format}`
      const url = window.URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      anchor.click()
      window.URL.revokeObjectURL(url)
      const exportedCount = Number(res.headers.get('X-Exported-Count'))
      setNotice(`已导出 ${Number.isFinite(exportedCount) ? exportedCount : 0} 条岗位。`)
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '导出失败')
    }
  }

  const startScoring = async (options: {
    scope: 'pending' | 'failed' | 'selected' | 'all_scored'
    limit: number | null
    job_ids: string[]
    force_rescore: boolean
  }) => {
    const res = await fetch('/api/scoring/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ options }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      const checks = Array.isArray(data.messages) ? data.messages.join('；') : ''
      throw new Error([data.error || '启动评分失败', checks].filter(Boolean).join('：'))
    }
    setNotice(`独立评分已启动，共 ${data.run?.remaining_job_ids?.length || 0} 个岗位。`)
  }

  if (showRecycleBin) {
    return (
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <Button variant="ghost" size="sm" onClick={() => setShowRecycleBin(false)}>返回岗位池</Button>
          <Button variant="secondary" size="sm" onClick={() => void loadRecycleBin()} disabled={recycleLoading}>刷新回收站</Button>
        </div>
        {notice && <div className="rounded-xl bg-[#FFF0E5] px-4 py-3 text-sm text-primary">{notice}</div>}
        <RecycleBinPanel
          jobs={recycleJobs}
          selectedIds={recycleSelectedIds}
          loading={recycleLoading}
          onToggleSelected={id => setRecycleSelectedIds(previous => previous.includes(id) ? previous.filter(item => item !== id) : [...previous, id])}
          onSelectAll={setRecycleSelectedIds}
          onRestore={ids => void restoreJobs(ids)}
          onPermanentDelete={requestPermanentDelete}
        />
        {permanentDeleteIds.length > 0 && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4" role="dialog" aria-modal="true">
            <div className="w-full max-w-lg rounded-3xl border border-red-200 bg-white p-6 shadow-2xl">
              <div className="flex items-start gap-3"><AlertTriangle className="mt-0.5 h-6 w-6 shrink-0 text-danger" /><div><h3 className="text-xl font-black">确认永久删除</h3><p className="mt-2 text-sm leading-6 text-muted">将永久删除 {permanentDeleteIds.length} 条岗位及其历史，无法恢复。存在发送或回复证据的岗位会被后端拒绝删除。</p></div></div>
              <label className="mt-5 flex cursor-pointer items-start gap-3 rounded-2xl border border-red-100 bg-red-50 p-3 text-sm font-bold"><input type="checkbox" checked={permanentDeleteAcknowledged} onChange={event => setPermanentDeleteAcknowledged(event.target.checked)} className="mt-0.5 h-4 w-4 accent-danger" /><span>我确认永久删除，并了解此操作无法撤销。</span></label>
              <div className="mt-6 flex justify-end gap-3"><Button variant="secondary" size="sm" onClick={() => setPermanentDeleteIds([])}>取消</Button><Button variant="destructive" size="sm" disabled={!permanentDeleteAcknowledged} onClick={() => void confirmPermanentDelete()}>永久删除</Button></div>
            </div>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="rounded-3xl border border-card-border bg-white p-5">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-black">岗位池</h2>
          <p className="mt-1 text-sm text-muted">集中查看已采集岗位、AI 分数、状态和详情入口。</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" onClick={() => { setShowRecycleBin(true); void loadRecycleBin() }}><Trash2 className="mr-1 h-4 w-4" />回收站 ({recycleJobs.length})</Button>
          <BriefcaseBusiness className="h-6 w-6 text-primary" />
        </div>
      </div>
      <JobFilterBar
        filters={filters}
        onChange={setFilters}
        onReset={() => setFilters({ ...EMPTY_JOB_FILTERS })}
        resultCount={total}
        totalCount={allTotal}
        invalidSalary={hasInvalidSalaryRange(filters)}
        showStatus
      />
      <div className="mb-4 flex flex-wrap items-center gap-2 text-xs">
        <Button variant="secondary" size="sm" disabled={!items.length} onClick={toggleCurrentPage}>
          {allPageSelected ? '取消选择本页' : '选择本页'}
        </Button>
        <span className="rounded-full bg-[#FFF0E5] px-3 py-2 font-bold text-primary">已选择 {selectedIds.length} 条</span>
        {selectedIds.length > 0 && <Button variant="ghost" size="sm" onClick={() => setSelectedIds([])}>清空选择</Button>}
        <Button variant="destructive" size="sm" disabled={!selectedIds.length} onClick={() => void softDelete(selectedIds)}>移入回收站</Button>
        <Button size="sm" onClick={() => setShowScoreDialog(true)}>单独 AI 评分</Button>
        <ExportMenu onExport={exportJobs} hasSelection={selectedIds.length > 0} hasFiltered={total > 0} />
      </div>
      {notice && <div className="mb-4 rounded-xl bg-[#FFF0E5] px-4 py-3 text-sm text-primary">{notice}</div>}
      {error && <div className="mb-4 rounded-xl border border-red-100 bg-red-50 px-4 py-3 text-sm text-danger">{error}</div>}
      <JobsTable
        jobs={items}
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        selectedIds={selectedIds}
        onToggleSelected={toggleSelected}
        onSoftDelete={job => void softDelete([job.id])}
        loading={loading}
      />
      <ScoreJobsDialog
        open={showScoreDialog}
        selectedJobIds={selectedIds}
        onClose={() => setShowScoreDialog(false)}
        onStart={startScoring}
      />
    </div>
  )
}

function ExportMenu({
  onExport,
  hasSelection,
  hasFiltered,
}: {
  onExport: (format: 'xlsx' | 'csv', scope: 'all' | 'filtered' | 'selected') => void
  hasSelection: boolean
  hasFiltered: boolean
}) {
  const [format, setFormat] = useState<'xlsx' | 'csv'>('xlsx')
  return (
    <div className="ml-auto flex flex-wrap items-center gap-2">
      <select
        value={format}
        onChange={event => setFormat(event.target.value as 'xlsx' | 'csv')}
        className="rounded-xl border border-card-border bg-white px-2 py-2 text-xs outline-none focus:border-primary"
      >
        <option value="xlsx">XLSX</option>
        <option value="csv">CSV</option>
      </select>
      <Button variant="secondary" size="sm" disabled={!hasFiltered} onClick={() => onExport(format, 'filtered')}>导出筛选结果</Button>
      <Button variant="secondary" size="sm" disabled={!hasSelection} onClick={() => onExport(format, 'selected')}>导出所选岗位</Button>
      <Button variant="secondary" size="sm" onClick={() => onExport(format, 'all')}>导出全部岗位</Button>
    </div>
  )
}

type MonitorFilter = 'pending' | 'resume' | 'follow_up' | 'replied'
const REPLY_RESOLUTION_ACTIONS = ['reply_dismissed', 'replied', 'auto_replied']

function uniqueLatestByJob(items: HistoryItem[]) {
  const seen = new Set<string>()
  return items.filter(item => {
    const key = item.job_id || `${item.company}-${item.title}-${item.action}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function sameHistoryJob(left: HistoryItem, right: HistoryItem) {
  if (left.job_id && right.job_id) return left.job_id === right.job_id
  return left.company === right.company && left.title === right.title
}

function isReplyPendingResolved(item: HistoryItem, history: HistoryItem[]) {
  return history.some(candidate =>
    candidate.id !== item.id
    && sameHistoryJob(item, candidate)
    && REPLY_RESOLUTION_ACTIONS.includes(candidate.action)
    && candidate.created_at >= item.created_at
  )
}

function isResumeFailureResolved(item: HistoryItem, history: HistoryItem[]) {
  return Boolean(item.resolved || item.resume_path) || history.some(candidate =>
    candidate.id > item.id
    && sameHistoryJob(item, candidate)
    && (candidate.action === 'needs_resume' || candidate.action === 'resume_sent')
  )
}

function latestHrText(item: HistoryItem) {
  const parsed = parseHistoryDetail(item)
  const latestHr = [...parsed.conversationTail].reverse().find(message => message.sender === 'hr' && message.text.trim())
  return parsed.hrQuestion || latestHr?.text || ''
}

function MonitorExecutionView({ history, refresh }: { history: HistoryItem[]; refresh: () => Promise<void> }) {
  const pendingReplies = uniqueLatestByJob(history.filter(item =>
    item.action === 'reply_pending' && !isReplyPendingResolved(item, history)
  ))
  const resumeFailures = uniqueLatestByJob(history.filter(item =>
    item.action === 'resume_failed' && !isResumeFailureResolved(item, history)
  ))
  const pendingItems = uniqueLatestByJob(
    [...pendingReplies, ...resumeFailures].sort((left, right) => right.id - left.id)
  )
  const resumeRequests = uniqueLatestByJob(history.filter(item =>
    item.action === 'needs_resume' || item.action === 'resume_sent' || item.action === 'resume_failed'
  ))
  const resumeRequestJobIds = new Set(resumeRequests.map(item => item.job_id).filter(Boolean))
  const followUpRecords = uniqueLatestByJob(history.filter(item => item.action === 'follow_up_sent'))
  const repliedRecords = uniqueLatestByJob(history.filter(item =>
    (item.action === 'replied' || item.action === 'auto_replied')
      && !resumeRequestJobIds.has(item.job_id)
  ))
  const [activeMonitorFilter, setActiveMonitorFilter] = useState<MonitorFilter>('pending')
  const visibleHistory = activeMonitorFilter === 'resume'
    ? resumeRequests
    : activeMonitorFilter === 'follow_up'
      ? followUpRecords
      : activeMonitorFilter === 'replied'
        ? repliedRecords
        : pendingItems
  const displayedHistory = activeMonitorFilter === 'pending' || activeMonitorFilter === 'resume'
    ? visibleHistory
    : visibleHistory.slice(0, 8)
  const [replyDrafts, setReplyDrafts] = useState<Record<number, string>>({})
  const [notice, setNotice] = useState('')

  const draftFor = (item: HistoryItem) => {
    const parsed = parseHistoryDetail(item)
    return replyDrafts[item.id] ?? parsed.aiReply ?? item.detail ?? ''
  }

  const sendManualReply = async (item: HistoryItem) => {
    try {
      const res = await fetch(`/api/history/${item.id}/reply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: draftFor(item) }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || '回复失败')
      }
      await refresh()
      setNotice('回复已记录，请在招聘平台手动发送。')
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '回复失败')
    }
  }

  const dismissPendingReply = async (item: HistoryItem) => {
    if (!window.confirm('确定放弃这条待回复建议吗？放弃后不会发送消息，也不会把岗位标记为拒绝。')) return
    try {
      const res = await fetch(`/api/history/${item.id}/dismiss`, { method: 'POST' })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || '放弃失败')
      }
      await refresh()
      setNotice('已放弃这条待回复建议。')
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '放弃失败')
    }
  }

  return (
    <div className="rounded-3xl border border-card-border bg-white p-5">
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-2xl font-black">监测执行</h2>
          <p className="mt-1 text-sm text-muted">这里不启动监测，只处理监测发现的 HR 问题、回复建议和结果。</p>
        </div>
        <span className="rounded-full bg-[#FFF0E5] px-3 py-2 text-xs font-black text-primary">待处理 {pendingItems.length}</span>
      </div>
      <div className="mb-4 flex flex-wrap gap-2">
        {[
          { key: 'pending' as const, label: '待处理', count: pendingItems.length },
          { key: 'resume' as const, label: '简历请求', count: resumeRequests.length },
          { key: 'follow_up' as const, label: '自动跟进', count: followUpRecords.length },
          { key: 'replied' as const, label: '已回复', count: repliedRecords.length },
        ].map(item => {
          const active = activeMonitorFilter === item.key
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => setActiveMonitorFilter(item.key)}
              className={`rounded-full px-3 py-1 text-xs font-bold transition ${active ? 'bg-primary text-white' : 'border border-card-border text-muted hover:border-primary/60 hover:text-primary'}`}
            >
              {item.label} {item.count}
            </button>
          )
        })}
      </div>
      {notice && <div className="mb-3 rounded-2xl bg-[#FFF0E5] px-4 py-3 text-sm text-primary">{notice}</div>}
      <div className="space-y-3">
        {displayedHistory.map((item, index) => {
          const canReply = item.action === 'reply_pending'
          const isFollowUp = item.action === 'follow_up_sent'
          const isResumeFailure = item.action === 'resume_failed'
          const isResumeRequest = item.action === 'needs_resume' || item.action === 'resume_sent' || isResumeFailure
          const isReplied = item.action === 'replied' || item.action === 'auto_replied'
          const parsed = parseHistoryDetail(item)
          const hrText = latestHrText(item)
          const isLegacyReplied = item.action === 'replied' && parsed.schema === 'legacy_text'
          const hasGeneratedReply = Boolean(parsed.aiReply) && !isLegacyReplied
          const showReplyContent = canReply || Boolean(parsed.hrQuestion) || hasGeneratedReply || isResumeRequest || isReplied
          const aiReplyText = parsed.aiReply || item.detail || getActionLabel(item.action)
          const systemFailureReason = parsed.systemReason || (isResumeFailure ? '未获得更具体的错误信息，请查看运行日志。' : '')
          return (
            <div key={`${item.created_at}-${index}`} className="grid gap-3 rounded-2xl border border-card-border bg-[#FFFCFA] p-4 lg:grid-cols-[130px_1fr_160px]">
              <div className="text-xs text-muted">
                <div>{item.created_at}</div>
                <div className="mt-2 rounded-full bg-white px-2 py-1 text-center font-bold text-primary">{getActionLabel(item.action)}</div>
              </div>
              <div>
                <div className="font-black">{item.company || '岗位'}｜{item.title || '监测记录'}</div>
                {showReplyContent ? (
                  <div className="mt-3 space-y-3">
                    {(isFollowUp || hrText) && (
                      <div>
                        <div className="text-xs font-black text-primary">{isFollowUp ? '自动跟进说明' : '对方问题 / HR'}</div>
                        <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-muted">
                          {isFollowUp ? 'HR 超过设定时间未回复，系统已自动执行一次跟进。' : hrText}
                        </p>
                      </div>
                    )}
                    {isResumeFailure && (
                      <div className="rounded-2xl border border-danger/30 bg-red-50 p-3">
                        <div className="text-xs font-black text-danger">系统失败原因</div>
                        <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-danger">{systemFailureReason}</p>
                      </div>
                    )}
                    {canReply ? (
                      <div>
                        <div className="mb-1 text-xs font-black text-primary">AI 建议回复</div>
                        <textarea
                          value={draftFor(item)}
                          onChange={event => setReplyDrafts(prev => ({ ...prev, [item.id]: event.target.value }))}
                          className="min-h-[92px] w-full rounded-2xl border border-card-border bg-white p-3 text-sm leading-6 text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/20"
                        />
                      </div>
                    ) : isResumeRequest || !hasGeneratedReply ? null : (
                      <div className="rounded-2xl border border-card-border bg-white p-3">
                        <div className="text-xs font-black text-primary">AI 回复</div>
                        <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-muted">{aiReplyText}</p>
                      </div>
                    )}
                  </div>
                ) : (
                  <p className="mt-2 text-sm leading-6 text-muted">{item.detail || getActionLabel(item.action)}</p>
                )}
                {canReply ? (
                  <p className="mt-2 text-xs text-primary">AI 建议：需要人工确认后再回复。</p>
                ) : item.action === 'needs_resume' ? (
                  <p className="mt-2 text-xs text-primary">简历请求：监测发现 HR 要简历，已生成定制简历，等待手动发送。</p>
                ) : item.action === 'resume_sent' ? (
                  <p className="mt-2 text-xs text-primary">简历生成：定制简历已生成，并已标记发送。</p>
                ) : isResumeFailure ? (
                  <p className="mt-2 text-xs text-danger">待处理：定制简历生成失败，尚无可下载文件，请手动处理或稍后重试生成。</p>
                ) : isReplied ? (
                  <p className="mt-2 text-xs text-primary">已回复：HR 已有反馈或系统已完成回复处理。</p>
                ) : null}
              </div>
              <div className="grid gap-2">
                <Button size="sm" disabled={!canReply} onClick={() => sendManualReply(item)}><MessageCircle className="mr-2 h-4 w-4" />确认回复</Button>
                <Button variant="secondary" size="sm" disabled={!canReply} onClick={() => setReplyDrafts(prev => ({ ...prev, [item.id]: draftFor(item) }))}>编辑回复</Button>
                <Button variant="secondary" size="sm" disabled={!canReply} onClick={() => dismissPendingReply(item)}>放弃</Button>
              </div>
            </div>
          )
        })}
        {!visibleHistory.length && <div className="rounded-2xl border border-dashed border-card-border bg-[#FFFCFA] p-5 text-sm text-muted">暂无待处理 HR 问题。</div>}
      </div>
    </div>
  )
}
