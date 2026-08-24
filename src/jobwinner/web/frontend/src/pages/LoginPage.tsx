import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, Chrome, ExternalLink, Github, Globe, KeyRound, Loader2, LogIn, RefreshCw, ShieldCheck, XCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

interface PlatformEntry {
  key: string
  name: string
  url: string
  opened: boolean
  logged_in: boolean
  tab_url?: string | null
  tab_title?: string | null
  login_hint: string
}

interface LoginStatus {
  ok: boolean
  runtime: boolean
  chrome: boolean
  browser_name: string
  errors: string[]
  platforms: PlatformEntry[]
}

function BriefcaseIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-6 w-6">
      <rect x="2" y="7" width="20" height="14" rx="2" />
      <path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" />
    </svg>
  )
}

const PLATFORM_LOGOS: Record<string, { icon: React.ReactNode; className: string }> = {
  boss: { icon: <BriefcaseIcon />, className: 'bg-[#FFF0E5] text-primary' },
}

function StatusBadge({ loggedIn, opened, chrome }: { loggedIn: boolean; opened: boolean; chrome: boolean }) {
  if (!chrome) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-red-200 bg-red-50 px-2.5 py-1 text-xs font-black text-red-600">
        <XCircle className="h-3.5 w-3.5" /> 未连接 Chrome
      </span>
    )
  }
  if (!opened) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-xs font-black text-amber-600">
        <AlertTriangle className="h-3.5 w-3.5" /> 页面未打开
      </span>
    )
  }
  if (!loggedIn) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-xs font-black text-amber-600">
        <KeyRound className="h-3.5 w-3.5" /> 待登录
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-xs font-black text-emerald-600">
      <CheckCircle2 className="h-3.5 w-3.5" /> 已登录
    </span>
  )
}

function GuideBox({ title, steps }: { title: string; steps: string[] }) {
  return (
    <div className="rounded-2xl border border-card-border bg-[#FFFCFA] px-4 py-4">
      <div className="flex items-center gap-2 text-sm font-black text-foreground">
        <LogIn className="h-4 w-4 text-primary" />
        {title}
      </div>
      <ol className="mt-3 space-y-2">
        {steps.map((step, i) => (
          <li key={i} className="flex items-start gap-2.5 text-sm leading-6 text-muted">
            <span className="mt-1 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-[#FFF0E5] text-[11px] font-black text-primary">
              {i + 1}
            </span>
            {step.startsWith('CODE:') ? (
              <code className="rounded-lg bg-zinc-100 px-2 py-0.5 font-mono text-xs text-foreground">
                {step.slice(5)}
              </code>
            ) : (
              <span>{step}</span>
            )}
          </li>
        ))}
      </ol>
    </div>
  )
}

function PlatformCard({
  platform,
  chrome,
  loading,
  onOpen,
  onRefresh,
  actionFeedback,
}: {
  platform: PlatformEntry
  chrome: boolean
  loading: boolean
  onOpen: () => void
  onRefresh: () => void
  actionFeedback: string
}) {
  const logo = PLATFORM_LOGOS[platform.key]
  return (
    <div className="overflow-hidden rounded-3xl border border-card-border bg-white shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-card-border p-6">
        <div className="flex items-center gap-4">
          <div className={cn('flex h-14 w-14 items-center justify-center rounded-2xl', logo?.className)}>
            {logo?.icon}
          </div>
          <div>
            <div className="flex items-center gap-3">
              <h3 className="text-xl font-black tracking-tight text-foreground">{platform.name}</h3>
              <StatusBadge loggedIn={platform.logged_in} opened={platform.opened} chrome={chrome} />
            </div>
            <p className="mt-1 text-xs text-muted">
              {platform.opened ? platform.tab_title || '页面已打开' : '尚未在 Chrome 中打开该平台页面'}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" onClick={onRefresh} disabled={loading}>
            <RefreshCw className={cn('mr-2 h-3.5 w-3.5', loading && 'animate-spin')} />
            {loading ? '检测中…' : '重新检测'}
          </Button>
          <Button size="sm" onClick={onOpen} disabled={loading || !chrome}>
            <ExternalLink className="mr-2 h-3.5 w-3.5" />
            打开页面
          </Button>
        </div>
      </div>

      <div className="space-y-5 p-6">
        <div className="rounded-2xl border border-card-border bg-[#FFFCFA] px-4 py-3">
          <div className="flex items-center gap-2 text-xs font-bold text-muted">
            <Globe className="h-3.5 w-3.5" /> 平台地址
          </div>
          <div className="mt-1 flex items-center gap-2">
            <code className="text-sm text-foreground">{platform.url}</code>
            <a href={platform.url} target="_blank" rel="noreferrer" className="text-xs font-bold text-primary hover:underline">
              浏览器打开 ↗
            </a>
          </div>
          {platform.tab_url && (
            <div className="mt-2 border-t border-card-border pt-2 text-xs text-muted">
              当前标签页：<code className="text-foreground">{platform.tab_url}</code>
            </div>
          )}
        </div>

        {!chrome ? (
          <GuideBox
            title="需要连接带远程调试的 Google Chrome"
            steps={[
              '关闭所有 Chrome 窗口后，在终端启动：',
              'CODE:chrome.exe --remote-debugging-port=9222',
              '启动后点击本页「重新检测」，确认变为「已连接 Chrome」。',
            ]}
          />
        ) : !platform.opened ? (
          <GuideBox
            title="先打开平台页面"
            steps={[
              '点击右上角「打开页面」，将在你已连接的 Chrome 中新开标签页。',
              '若按钮不可用，请确认 Chrome 已开启远程调试（浏览器运行组件已连接）。',
              '打开后点击「重新检测」确认页面状态。',
            ]}
          />
        ) : !platform.logged_in ? (
          <GuideBox
            title="完成登录"
            steps={[
              '在刚打开的 Chrome 标签页中，用手机 BOSS直聘 App 扫码登录（推荐）或输入账号密码。',
              '登录完成后，平台会自动跳转到求职者首页。',
              '回到本页点击「重新检测」，确认状态变为「已登录」。',
            ]}
          />
        ) : (
          <div className="flex items-start gap-3 rounded-2xl border border-emerald-200 bg-emerald-50/60 px-4 py-3">
            <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
            <div>
              <div className="text-sm font-black text-emerald-700">登录状态正常</div>
              <div className="mt-0.5 text-xs text-emerald-600/90">
                已检测到 {platform.name} 页面且不处于登录页。可以开始采集、评分与投递任务。
              </div>
            </div>
          </div>
        )}

        {platform.login_hint && platform.opened && (
          <div className="rounded-2xl bg-[#FFF0E5] px-4 py-3 text-sm text-primary">{platform.login_hint}</div>
        )}

        {actionFeedback && (
          <div className="rounded-2xl border border-card-border bg-[#FFFCFA] px-4 py-3 text-sm text-foreground">
            {actionFeedback}
          </div>
        )}
      </div>
    </div>
  )
}

export default function LoginPage() {
  const [status, setStatus] = useState<LoginStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [feedback, setFeedback] = useState('')

  const fetchStatus = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await fetch('/api/platforms/login', { cache: 'no-store' })
      const data = await res.json()
      if (!res.ok || !data.ok) {
        throw new Error(data.error || '接口返回 ' + res.status)
      }
      setStatus(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : '检测失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchStatus()
  }, [fetchStatus])

  const openPlatform = async (key: string) => {
    setFeedback('正在打开页面…')
    try {
      const res = await fetch('/api/platforms/login/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ platform: key }),
      })
      const data = await res.json()
      if (!res.ok || !data.ok) {
        throw new Error(data.error || '打开失败')
      }
      setFeedback('已在 Chrome 中打开新标签页，请完成登录后点击「重新检测」。')
      fetchStatus()
    } catch (e) {
      setFeedback(e instanceof Error ? e.message : '打开页面失败')
    }
  }

  const chromeConnected = Boolean(status?.chrome)
  const hasAnyError = Boolean(status?.errors?.length)

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <section className="rounded-3xl border border-card-border bg-white p-6 shadow-sm">
        <div className="text-xs font-black tracking-[0.18em] text-primary">LOGIN MANAGER</div>
        <div className="mt-1 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-3xl font-black tracking-tight text-foreground">登录管理</h2>
          <Button variant="secondary" size="sm" onClick={fetchStatus} disabled={loading}>
            <RefreshCw className={cn('mr-2 h-3.5 w-3.5', loading && 'animate-spin')} />
            {loading ? '检测中…' : '刷新检测'}
          </Button>
        </div>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
          集中管理招聘平台的登录状态。采集、评分与投递任务开始前，请确保目标平台已登录，否则任务会因登录校验失败而暂停。
        </p>

        <div className={cn('mt-4 flex items-center gap-3 rounded-2xl px-4 py-3', chromeConnected ? 'bg-emerald-50/70' : 'bg-red-50/70')}>
          {chromeConnected ? (
            <>
              <Chrome className="h-5 w-5 shrink-0 text-emerald-600" />
              <div className="text-sm font-bold text-emerald-700">
                Google Chrome 已连接{status?.browser_name ? '（' + status.browser_name + '）' : ''}
              </div>
            </>
          ) : (
            <>
              <XCircle className="h-5 w-5 shrink-0 text-red-600" />
              <div className="text-sm font-bold text-red-600">尚未连接到带远程调试的 Google Chrome</div>
            </>
          )}
          {status?.runtime === false && (
            <span className="ml-auto text-xs font-bold text-amber-600">浏览器运行组件未就绪</span>
          )}
        </div>
      </section>

      {error && (
        <div className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">{error}</div>
      )}

      {hasAnyError && status && (
        <div className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-700">
          {status.errors.map((msg, i) => (
            <div key={i}>· {msg}</div>
          ))}
        </div>
      )}

      {status?.platforms.map(platform => (
        <PlatformCard
          key={platform.key}
          platform={platform}
          chrome={chromeConnected}
          loading={loading}
          onOpen={() => openPlatform(platform.key)}
          onRefresh={fetchStatus}
          actionFeedback={feedback}
        />
      ))}

      <div className="flex items-center justify-center gap-2 pb-4 text-center text-[11px] text-muted">
        <Github className="h-3.5 w-3.5" />
        登录态保存在你本机的 Chrome 中，JobWinner 不会保存任何账号密码
      </div>
    </div>
  )
}
