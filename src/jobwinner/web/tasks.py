"""Workbench background task runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import Event, Lock, Thread, Timer
from typing import Any, Callable
from uuid import uuid4
import traceback

from jobwinner.throttle import SendWindowChecker

# Parallelism model: every mode is its own group so collect/score/monitor/
# deliver can all run concurrently and independently. Same-mode is mutually
# exclusive; full bundles everything and stays exclusive with all modes.
MODE_GROUPS = {
    "deliver": "deliver",
    "collect": "collect",
    "score": "score",
    "rescore": "rescore",
    "monitor": "monitor",
    "full": "full",
}


MODE_LABELS = {
    "full": "运行全流程",
    "collect": "采集",
    "score": "AI 评分",
    "rescore": "重新评分",
    "monitor": "监测",
    "deliver": "发送",
}

TERMINAL_STATUSES = {"completed", "failed", "stopped"}
ACTIVE_STATUSES = {"running", "stopping"}
DEADLINE_MODES = {"full", "monitor", "deliver"}


class TaskAlreadyRunningError(RuntimeError):
    """Raised when a mutually exclusive workbench task is already active."""


@dataclass
class WorkbenchTask:
    id: str
    mode: str
    label: str
    status: str = "running"
    logs: list[str] = field(default_factory=list)
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    deadline_at: str | None = None
    stop_reason: str | None = None
    stop_requested: Event = field(default_factory=Event, repr=False)
    metrics: dict[str, int] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict, repr=False)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "mode": self.mode,
            "label": self.label,
            "status": self.status,
            "logs": list(self.logs),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deadline_at": self.deadline_at,
            "stop_reason": self.stop_reason,
            "stop_requested": self.stop_requested.is_set(),
            "metrics": dict(self.metrics),
            "traceback": self.context.get("traceback"),
            "sending_job_ids": list(self.context.get("sending_job_ids") or []),
        }


Executor = Callable[[WorkbenchTask, dict], None]


class WorkbenchTaskRunner:
    def __init__(self, executors: dict[str, Executor] | None = None):
        self._executors = executors or {}
        self._tasks: dict[str, WorkbenchTask] = {}
        self._threads: dict[str, Thread] = {}
        self._deadline_timers: dict[str, Timer] = {}
        self._lock = Lock()

    def start(self, mode: str, config: dict) -> dict:
        if mode not in MODE_LABELS:
            raise ValueError(f"Unsupported workbench mode: {mode}")

        with self._lock:
            group = MODE_GROUPS.get(mode, mode)
            conflicting = self._active_in_group_locked(group)
            if conflicting:
                raise TaskAlreadyRunningError(
                    f"任务「{conflicting.label}」正在运行，同类型的任务不能同时启动；"
                    f"但可以启动其他环节的任务（如发送/采集/评分/监测并行）"
                )
            active_any = self._active_task_locked()
            if mode == "full" and active_any:
                raise TaskAlreadyRunningError(
                    f"任务「{active_any.label}」正在运行，全流程常驻占用所有环节，请先停止它再启动。"
                )
            if active_any is not None and active_any.mode == "full":
                raise TaskAlreadyRunningError(
                    f"全流程常驻「{active_any.label}」正在运行并占用采集/评分/监测，请先停止它再启动其他任务。"
                )

            task = WorkbenchTask(id=str(uuid4()), mode=mode, label=MODE_LABELS[mode])
            deadline = _deadline_from_config(mode, config)
            if deadline:
                task.deadline_at = deadline.isoformat(timespec="seconds")
            self._tasks[task.id] = task

            force_deliver = bool(
                mode == "deliver"
                and isinstance(config, dict)
                and config.get("_workbench_skip_greeting")
            )
            if deadline and deadline <= datetime.now() and not force_deliver:
                task.stop_requested.set()
                task.status = "stopped"
                task.stop_reason = "今日发送时间窗口已截止，后台未启动"
                task.logs.append(task.stop_reason)
                task.updated_at = datetime.now().isoformat(timespec="seconds")
                return task.snapshot()

            thread = Thread(target=self._run, args=(task, config), daemon=True)
            self._threads[task.id] = thread
            if deadline and not force_deliver:
                delay_seconds = max((deadline - datetime.now()).total_seconds(), 0)
                timer = Timer(delay_seconds, self._stop_at_deadline, args=(task.id,))
                timer.daemon = True
                self._deadline_timers[task.id] = timer
                timer.start()
            thread.start()
            return task.snapshot()

    def status(self) -> dict:
        with self._lock:
            active = self._active_task_locked()
            active_tasks = [
                task.snapshot()
                for task in self._tasks.values()
                if task.status in ACTIVE_STATUSES
            ]
            tasks = [task.snapshot() for task in self._tasks.values()]
            return {
                "active": active.snapshot() if active else None,
                "active_tasks": active_tasks,
                "last_task": tasks[-1] if tasks else None,
                "tasks": tasks,
            }

    def stop(self, task_id: str, reason: str = "用户已请求停止") -> dict:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            if task.status in TERMINAL_STATUSES:
                return task.snapshot()
            task.stop_requested.set()
            task.status = "stopping"
            task.stop_reason = reason
            if reason and (not task.logs or task.logs[-1] != reason):
                task.logs.append(reason)
            task.updated_at = datetime.now().isoformat(timespec="seconds")
            confirmation_event = task.context.get("confirmation_event")
            if isinstance(confirmation_event, Event):
                confirmation_event.set()
            monitor_wakeup_event = task.context.get("monitor_wakeup_event")
            if isinstance(monitor_wakeup_event, Event):
                monitor_wakeup_event.set()
            return task.snapshot()

    def wait(self, timeout: float | None = None) -> None:
        threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=timeout)

    def _run(self, task: WorkbenchTask, config: dict) -> None:
        try:
            executor = self._executors.get(task.mode)
            if executor:
                executor(task, config)
            with self._lock:
                if task.stop_requested.is_set():
                    task.status = "stopped"
                else:
                    task.status = "completed"
                task.updated_at = datetime.now().isoformat(timespec="seconds")
        except Exception as exc:
            with self._lock:
                if task.stop_requested.is_set():
                    task.status = "stopped"
                    task.error = None
                else:
                    task.status = "failed"
                    task.error = str(exc)
                    task.context = dict(task.context)
                    task.context["traceback"] = traceback.format_exc()
                task.updated_at = datetime.now().isoformat(timespec="seconds")
        finally:
            with self._lock:
                timer = self._deadline_timers.pop(task.id, None)
            if timer:
                timer.cancel()

    def _stop_at_deadline(self, task_id: str) -> None:
        try:
            self.stop(task_id, "已到发送时间窗口截止时间，后台自动停止")
        except KeyError:
            return

    def _active_task_locked(self) -> WorkbenchTask | None:
        for task in self._tasks.values():
            if task.status in ACTIVE_STATUSES:
                return task
        return None

    def _active_in_group_locked(self, group: str) -> WorkbenchTask | None:
        """Return an active task whose mode shares the given parallelism group."""
        for task in self._tasks.values():
            if task.status in ACTIVE_STATUSES and MODE_GROUPS.get(task.mode, task.mode) == group:
                return task
        return None


def _deadline_from_config(mode: str, config: dict) -> datetime | None:
    """Resolve the automatic stop deadline for long-running/send tasks."""
    if mode not in DEADLINE_MODES:
        return None
    throttle = config.get("throttle", {}) if isinstance(config, dict) else {}
    windows = throttle.get("send_windows", [])
    if not isinstance(windows, list):
        return None
    return SendWindowChecker(windows).latest_end_datetime()
