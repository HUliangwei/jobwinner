
import type { WorkbenchTask } from '@/hooks/useDashboard'

type StageState = 'pending' | 'active' | 'done' | 'skipped'

export interface PipelineStage {
  key: string
  label: string
  state: StageState
  detail?: string
}

// 6-stage pipeline with user-facing verb labels:
// 采集 → 评分 → 待确认 → 发送 → 监测（+ 常驻 idle）
const STAGE_DEFS: { key: string; label: string; activeLabel: string; active: string[]; done: string[] }[] = [
  {
    key: 'collect',
    label: '采集',
    activeLabel: '采集中',
    active: ['开始采集', '开始一轮全量扫描', '流水线采集', '采集线程', '第 1 批：开始采集', '第 2 批：开始采集'],
    done: ['采集结束', '流水线采集-评分结束', '本轮扫描完成', '已达批次上限', '没有采到新岗位', '本轮采集完成'],
  },
  {
    key: 'score',
    label: '评分',
    activeLabel: '评分中',
    active: ['开始 AI 评分', '评分流水线', '单独 AI 评分', 'AI 评分进度', '开始重新评分', '评分池', '自动生成招呼语'],
    done: ['评分完成', '评分流水线已停止', '流水线采集-评分结束', '评分批次异常', '自动生成招呼语完成'],
  },
  {
    key: 'confirm',
    label: '待确认',
    activeLabel: '待你确认',
    active: ['等待前端确认投递', '前端已确认', '确认投递'],
    done: ['没有待确认岗位', '流程结束', '等待前端确认投递，已确认'],
  },
  {
    key: 'send',
    label: '发送',
    activeLabel: '发送中',
    active: ['发送招呼语', '发送队列', '优先续发', '继续处理队列', '发送:'],
    done: ['发送完成', '发送失败', '发送额度未执行', '已直接进入发送流程'],
  },
  {
    key: 'monitor',
    label: '监测',
    activeLabel: '监测中',
    active: ['执行一轮监测', '监测线程', '开始监测', '开始监听', '持续监听'],
    done: ['本轮监测完成', '监测线程异常', '监测期间新增'],
  },
]

const MODE_STAGES: Record<string, string[]> = {
  full: ['collect', 'score', 'confirm', 'send', 'monitor'],
  collect: ['collect'],
  score: ['score', 'confirm'],
  rescore: ['score', 'confirm'],
  monitor: ['monitor'],
  deliver: ['send'],
}

export function inferTaskStages(task: WorkbenchTask | null | undefined): PipelineStage[] {
  if (!task) return []
  const logs = task.logs || []
  const mode = task.mode || ''
  const status = task.status || ''
  const relevant = MODE_STAGES[mode] || ['collect', 'score', 'confirm', 'send', 'monitor']
  const finished = status === 'completed' || status === 'stopped' || status === 'failed'

  return STAGE_DEFS
    .filter(def => relevant.includes(def.key))
    .map(def => {
      let state: StageState = 'pending'
      let detail: string | undefined
      for (const log of logs) {
        const doneHit = def.done.find(m => log.includes(m))
        const activeHit = def.active.find(m => log.includes(m))
        if (doneHit) {
          state = 'done'
          detail = log
        } else if (activeHit) {
          state = 'active'
          detail = log
        }
      }
      if (finished && state === 'pending') state = 'skipped'
      return { key: def.key, label: def.label, state, detail }
    })
}

const DOT: Record<StageState, string> = { pending: '○', active: '●', done: '✓', skipped: '—' }
const STATE_TEXT: Record<StageState, string> = { pending: '未开始', active: '进行中', done: '已完成', skipped: '跳过' }
const STATE_STYLE: Record<StageState, string> = {
  pending: 'border-card-border bg-white text-muted',
  active: 'border-primary/50 bg-[#FFF0E5] text-primary shadow-sm',
  done: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  skipped: 'border-card-border bg-[#F8F8F6] text-muted/50 line-through',
}

export function TaskPipelineStages({ task }: { task: WorkbenchTask | null | undefined }) {
  const stages = inferTaskStages(task)
  if (!stages.length) return null
  return (
    <div className="mt-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        {stages.map((stage, index) => (
          <div key={stage.key} className={'rounded-xl border px-3 py-2.5 ' + STATE_STYLE[stage.state]}>
            <div className="flex items-center gap-1.5">
              <span className={'text-sm font-black ' + (stage.state === 'active' ? 'animate-pulse' : '')}>{DOT[stage.state]}</span>
              <span className="text-xs font-black">{stage.label}</span>
              <span className="ml-auto text-[10px] font-bold text-muted/70">{index + 1}</span>
            </div>
            <div className="mt-1 text-[11px] font-bold">
              {stage.state === 'active' ? STAGE_DEFS.find(def => def.key === stage.key)?.activeLabel || '进行中' : STATE_TEXT[stage.state]}
            </div>
          </div>
        ))}
      </div>
      {task?.status === 'failed' && (
        <div className="mt-2 text-xs font-bold text-danger">任务运行失败，请查看错误信息。</div>
      )}
    </div>
  )
}
