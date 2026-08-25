import { useState } from 'react'
import { useLocation } from 'react-router-dom'
import { Activity, Power } from 'lucide-react'

const pageTitles: Record<string, string> = {
  '/': '工作台',
  '/jobs': '岗位池',
  '/monitor': '监测执行',
  '/login': '登录管理',
  '/config': '配置',
}

export function Header() {
  const location = useLocation()
  const title = pageTitles[location.pathname] || 'JobWinner'
  const [shuttingDown, setShuttingDown] = useState(false)

  const handleShutdown = async () => {
    if (!window.confirm('确定要退出 JobWinner 服务吗？\n将停止采集/评分/监测/发送任务并关闭服务。')) return
    setShuttingDown(true)
    try {
      await fetch('/api/shutdown', { method: 'POST' })
    } catch {
      // 服务可能已在响应前退出，忽略网络错误
    }
    // 服务退出后本页无法再刷新，展示提示并延迟关闭
    setTimeout(() => {
      document.title = 'JobWinner 已退出'
      window.location.href = 'about:blank'
      window.close()
    }, 1000)
  }

  return (
    <header className="h-16 border-b border-card-border bg-[#FFFCFA] flex items-center justify-between px-6">
      <h1 className="text-lg font-black text-foreground">{title}</h1>
      <div className="flex items-center gap-3 text-xs text-muted">
        <span className="flex items-center gap-2">
          <Activity className="w-3 h-3 text-success" />
          <span>本地服务运行中</span>
        </span>
        <button
          type="button"
          onClick={handleShutdown}
          disabled={shuttingDown}
          className="inline-flex items-center gap-1.5 rounded-lg border border-card-border bg-white px-3 py-1.5 text-xs font-bold text-foreground transition hover:border-danger/40 hover:bg-red-50 hover:text-danger disabled:opacity-50"
          aria-label="退出服务"
        >
          <Power className="h-3.5 w-3.5" />
          {shuttingDown ? '正在退出…' : '退出服务'}
        </button>
      </div>
    </header>
  )
}
