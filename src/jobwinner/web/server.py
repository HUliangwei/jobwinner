"""JobWinner Web Server - Bottle HTTP service + API routes.

Serves:
- /api/* → JSON data endpoints
- /* → Frontend static files (dist/)
"""

import json
import math
import mimetypes
import os
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from socketserver import ThreadingMixIn
from threading import Event, Lock
from uuid import uuid4
from wsgiref.simple_server import WSGIServer

import yaml
from bottle import Bottle, request, response, static_file, abort

from jobwinner import __version__
from jobwinner.ai.credentials import get_ai_api_key
from jobwinner.cities import CityRefreshError, get_city_map, load_city_snapshot, refresh_city_cache
from jobwinner.config import AI_SERVICE_PRESETS, load_config
from jobwinner.db import (
	JobDeletionConflictError,
	add_history,
	count_unresolved_monitor_items,
	get_daily_activity,
	get_db,
	get_funnel_stats,
	get_jobs_needing_resume,
	get_jobs_pending_confirmation,
	get_jobs_ready_to_send,
	get_jobs_resume_pending,
	get_jobs_with_send_errors,
	get_recent_history,
	get_unresolved_resume_failures,
	get_stats,
	get_top_companies,
	permanent_delete_jobs,
	query_jobs,
	restore_jobs,
	soft_delete_jobs,
	update_job_greeting,
	update_job_status,
)
from jobwinner.job_filters import parse_monthly_salary_k
from jobwinner.job_export import InvalidJobSelectionError, export_jobs, export_row_count
from jobwinner.scoring_run_store import (
	create_scoring_run,
	get_scoring_run,
	list_scoring_runs,
	mark_orphaned_scoring_runs_paused,
	update_scoring_run,
)
from jobwinner.scoring_selection import preview_scoring, select_scoring_jobs, validate_options
from jobwinner.web.preflight import check_ai_connection, collect_preflight_checks, error_messages
from jobwinner.web.resume_upload import ResumeUploadError, prepare_resume_content
from jobwinner.web.city_lookup import CityLookupError, lookup_city
from jobwinner.web.tasks import TaskAlreadyRunningError, WorkbenchTask, WorkbenchTaskRunner

mimetypes.add_type("application/javascript", ".js", strict=True)
mimetypes.add_type("application/javascript", ".mjs", strict=True)
mimetypes.add_type("text/javascript", ".cjs", strict=True)
mimetypes.add_type("text/css", ".css", strict=True)

app = Bottle()
task_runner = WorkbenchTaskRunner()
job_mutation_lock = Lock()

# ── 空闲自动退出（关掉网页即停服）─────────────────────────────
# 任何 HTTP 请求都会刷新心跳；面板被浏览器访问过（_idle_armed）且
# 超过 IDLE_TIMEOUT 秒无请求时，视为用户已关闭页面，自动停服退出。
IDLE_TIMEOUT = int(os.environ.get("JOBWINNER_IDLE_TIMEOUT", "180"))  # 秒：连续 3 分钟无请求即退出（避免误杀，可环境变量覆盖）
_HAS_FRONTEND_ACCESS = False
_LAST_ACTIVITY = time.monotonic()


def _touch_activity() -> None:
	global _HAS_FRONTEND_ACCESS, _LAST_ACTIVITY
	_HAS_FRONTEND_ACCESS = True
	_LAST_ACTIVITY = time.monotonic()


if not hasattr(app, "_idle_shutdown_installed"):
	app.add_hook("before_request", _touch_activity)
	app._idle_shutdown_installed = True


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
	"""Handle Chrome preconnect sockets without blocking other requests."""

	daemon_threads = True
	allow_reuse_address = True

# Paths
FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"
SCHEMA_PATH = Path(__file__).parent / "config_schema.json"


def _default_base_dir() -> Path:
	"""Resolve the runtime project directory even when launched outside repo root."""
	source_root = Path(__file__).resolve().parents[3]
	if (source_root / "config.yaml").exists():
		return source_root

	cwd = Path.cwd()
	if (cwd / "config.yaml").exists():
		return cwd

	return cwd


BASE_DIR = _default_base_dir()
DATA_DIR = BASE_DIR / "data"
RESUME_DIR = DATA_DIR / "resumes"
CONFIG_PATH = BASE_DIR / "config.yaml"


def _config_rel_path(path: Path) -> str:
	"""Return a BASE_DIR-relative path string for portable config.yaml.

	Keeps machine-independent paths (e.g. ``./data/resumes/xxx.md``) so the
	project can be freely moved/migrated without rewriting absolute paths.
	Falls back to the absolute path when ``path`` lies outside BASE_DIR.
	"""
	try:
		rel = path.resolve().relative_to(BASE_DIR.resolve()).as_posix()
		return f"./{rel}"
	except ValueError:
		return str(path)


def set_base_dir(base_dir: Path | str) -> None:
	"""Set the runtime directory used for config.yaml, data, and uploads."""
	global BASE_DIR, DATA_DIR, RESUME_DIR, CONFIG_PATH
	BASE_DIR = Path(base_dir).resolve()
	DATA_DIR = BASE_DIR / "data"
	RESUME_DIR = DATA_DIR / "resumes"
	CONFIG_PATH = BASE_DIR / "config.yaml"
	# 一次性迁移：旧版数据文件 bosshunter.db → jobwinner.db（重命名，SQLite 无损）
	_legacy = DATA_DIR / "bosshunter.db"
	_new = DATA_DIR / "jobwinner.db"
	try:
		if not _new.exists() and _legacy.exists():
			_legacy.rename(_new)
	except Exception:
		pass  # 迁移失败不阻塞；读库时仍有回退
	mark_orphaned_scoring_runs_paused(_new)


def _get_web_db():
	"""Open the dashboard database from the resolved runtime data directory."""
	return get_db(DATA_DIR / "jobwinner.db")


def _json_response(data, status_code=200):
	"""Return JSON response with proper headers."""
	response.content_type = "application/json; charset=utf-8"
	response.status = status_code
	return json.dumps(data, ensure_ascii=False, default=str)


def _serialize_history_items(items):
	"""Expose structured history details while retaining the legacy detail field."""
	serialized = []
	for item in items:
		record = dict(item)
		detail = record.get("detail")
		if isinstance(detail, str) and detail.lstrip().startswith("{"):
			try:
				payload = json.loads(detail)
			except (json.JSONDecodeError, TypeError):
				payload = None
			if isinstance(payload, dict):
				record["detail_payload"] = payload
		record["resolved"] = bool(record.get("resolved"))
		serialized.append(record)
	return serialized


def _mask_api_key(key):
	"""Return a display-safe API key marker."""
	if not key:
		return ""
	if len(key) > 8:
		return key[:4] + "***" + key[-4:]
	return "***"


def _redact_config_for_response(config):
	"""Hide secrets before returning config to the browser."""
	redacted = deepcopy(config)
	ai_cfg = redacted.get("ai")
	if isinstance(ai_cfg, dict):
		key = ai_cfg.pop("api_key", None)
		if key:
			ai_cfg["api_key_masked"] = _mask_api_key(str(key))
		auth_token = ai_cfg.pop("auth_token", None)
		if auth_token:
			ai_cfg["auth_token_masked"] = _mask_api_key(str(auth_token))
	return redacted


def _config_download_payload(config: dict) -> str:
	"""Serialize a shareable config backup without credentials."""
	redacted = _redact_config_for_response(config)
	ai_cfg = redacted.get("ai")
	if isinstance(ai_cfg, dict):
		ai_cfg.pop("api_key_masked", None)
		ai_cfg.pop("auth_token_masked", None)
	return yaml.dump(redacted, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _write_config(config: dict) -> None:
	"""Atomically replace config.yaml so an interrupted write cannot corrupt it."""
	CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
	temporary_path = None
	try:
		with tempfile.NamedTemporaryFile(
			"w",
			encoding="utf-8",
			dir=CONFIG_PATH.parent,
			prefix=f".{CONFIG_PATH.name}.",
			suffix=".tmp",
			delete=False,
		) as temporary:
			temporary_path = Path(temporary.name)
			yaml.dump(config, temporary, allow_unicode=True, default_flow_style=False, sort_keys=False)
			temporary.flush()
			os.fsync(temporary.fileno())
		os.replace(temporary_path, CONFIG_PATH)
		temporary_path = None
	finally:
		if temporary_path is not None:
			try:
				temporary_path.unlink()
			except FileNotFoundError:
				pass


def _sanitize_config_for_write(data):
	"""Remove browser-only fields and preserve existing secrets on blank posts."""
	cleaned = deepcopy(data)
	ai_cfg = cleaned.get("ai")
	if not isinstance(ai_cfg, dict):
		return cleaned

	ai_cfg.pop("api_key_masked", None)
	ai_cfg.pop("has_api_key", None)
	ai_cfg.pop("auth_token_masked", None)
	ai_cfg.pop("has_auth_token", None)

	existing_ai = load_config(CONFIG_PATH).get("ai", {})
	service = ai_cfg.get("service") or existing_ai.get("service")
	if service not in AI_SERVICE_PRESETS:
		provider = ai_cfg.get("provider") or existing_ai.get("provider") or "anthropic"
		service = "custom" if provider == "openai_compatible" else "anthropic"
	ai_cfg["service"] = service
	ai_cfg["provider"] = AI_SERVICE_PRESETS[service]["provider"]

	clear_credentials = bool(ai_cfg.pop("clear_credentials", False))

	for field in ("api_key", "auth_token"):
		if clear_credentials:
			posted_value = ai_cfg.get(field)
			if posted_value is None or str(posted_value).strip() == "":
				ai_cfg.pop(field, None)
			continue
		posted_value = ai_cfg.get(field)
		existing_value = existing_ai.get(field)
		existing_mask = _mask_api_key(str(existing_value)) if existing_value else ""
		should_preserve = (
			posted_value is None
			or str(posted_value).strip() == ""
			or (existing_mask and posted_value == existing_mask)
		)

		if should_preserve:
			if existing_value:
				ai_cfg[field] = existing_value
			else:
				ai_cfg.pop(field, None)

	return cleaned


def _preflight_messages(mode: str, config: dict) -> list[str]:
	"""Return user-actionable blockers before starting a dashboard task."""
	messages: list[str] = []
	if mode not in {"full", "collect", "rescore", "score", "monitor", "deliver"}:
		messages.append(f"不支持的任务模式：{mode}")

	from jobwinner.ai.resume_picker import resume_paths
	strategy = resume_paths(config)
	if not strategy or not any(p.exists() for p in strategy):
		messages.append("请先在配置页上传 .md、.docx 或 .pdf 简历。")

	if mode in {"full", "collect"} and not config.get("search", {}).get("keywords"):
		messages.append("请先在配置页填写搜索关键词。")

	if mode in {"full", "collect", "rescore", "score"} and not get_ai_api_key(config):
		messages.append("请先在配置页填写当前 AI 服务的 API Key，或设置对应的标准环境变量。")

	return messages


def _task_config(extra: dict | None = None) -> dict:
	config = load_config(CONFIG_PATH)
	if extra:
		config.update(extra)
	return config


def _log(task: WorkbenchTask, message: str) -> None:
	task.logs.append(message)


def _record_collect_progress(task: WorkbenchTask, state: dict) -> None:
	task.metrics.update({
		"collect_seen": int(state.get("seen") or 0),
		"collect_new": int(state.get("new") or 0),
		"collect_duplicate": int(state.get("duplicate") or 0),
	})



def _record_score_progress(task: WorkbenchTask, state: dict) -> None:
	task.metrics.update({
		"ai_completed": int(state.get("completed") or 0),
		"ai_total": int(state.get("total") or 0),
		"ai_passed": int(state.get("scored") or 0),
		"ai_filtered": int(state.get("filtered") or 0),
		"ai_failed": int(state.get("failed") or 0),
	})
	_log(
		task,
		f"AI 评分进度 {state['completed']}/{state['total']}：通过 {state['scored']}，过滤 {state['filtered']}，失败 {state['failed']}",
	)



def _execute_collect(task: WorkbenchTask, config: dict) -> None:
	from jobwinner.ai.scorer import score_jobs
	from jobwinner.scraper.jobs import scrape_jobs

	keywords = config.get("search", {}).get("keywords", [])
	search_cfg = config.get("search", {}) if isinstance(config.get("search"), dict) else {}
	batch_size = int(config.get("_batch_size") or search_cfg.get("batch_size") or 20)
	max_batches = config.get("_max_batches")  # None = until no new jobs
	batch_no = 0
	total_collected = 0
	total_scored = 0

	_log(task, f"开始流水线采集（每批 {batch_size} 个新岗位，采集一批评分一批）")
	while True:
		if task.stop_requested.is_set():
			return
		if max_batches is not None and batch_no >= max_batches:
			_log(task, f"已达批次上限 {max_batches}，流水线采集结束")
			break
		batch_no += 1

		collect_config = dict(config)
		collect_config["_workbench_stop_event"] = task.stop_requested
		collect_config["_workbench_collect_progress"] = lambda state: _record_collect_progress(task, state)
		collected_job_ids: list[str] = []
		_log(task, f"第 {batch_no} 批：开始采集（上限 {batch_size} 个新岗位）")
		scrape_jobs(collect_config, keywords, limit=batch_size, collected_job_ids=collected_job_ids)
		if task.stop_requested.is_set():
			return
		new_count = len(collected_job_ids)
		_log(
			task,
			f"第 {batch_no} 批采集完成：扫描 {task.metrics.get('collect_seen', 0)}，新增 {new_count}",
		)
		if new_count == 0:
			_log(task, "没有采到新岗位，采集结束")
			break
		total_collected += new_count

		_log(task, f"第 {batch_no} 批：立即 AI 评分 {new_count} 个新岗位")
		score_config = dict(config)
		score_config["_workbench_stop_event"] = task.stop_requested
		score_config["_workbench_log"] = lambda message: _log(task, message)
		score_config["_workbench_score_progress"] = lambda state: _record_score_progress(task, state)
		score_jobs(score_config, job_ids=collected_job_ids)
		total_scored += new_count
		if task.stop_requested.is_set():
			return

	_log(task, f"流水线采集-评分结束：共采 {total_collected} 个新岗位、评 {total_scored} 个")


def _execute_rescore(task: WorkbenchTask, config: dict) -> None:
	from jobwinner.ai.scorer import score_jobs

	score_config = dict(config)
	score_config["_workbench_stop_event"] = task.stop_requested
	score_config["_workbench_log"] = lambda message: _log(task, message)
	score_config["_workbench_score_progress"] = lambda state: _record_score_progress(task, state)
	_log(task, "开始重新评分")
	score_jobs(score_config, rescore_filtered=True)


def _execute_score(task: WorkbenchTask, config: dict) -> None:
	from jobwinner.ai.scorer import score_jobs

	run_id = str(config.get("_score_run_id") or "")
	options = config.get("_score_options", {}) if isinstance(config.get("_score_options"), dict) else {}
	db_path = DATA_DIR / "jobwinner.db"
	pool_loop = bool(config.get("_pool_loop"))

	def checkpoint(state: dict) -> None:
		if not run_id:
			return
		remaining = [str(job_id) for job_id in state.get("remaining_job_ids", []) if str(job_id)]
		status = str(state.get("status") or "running")
		update_scoring_run(
			db_path,
			run_id,
			status=status,
			remaining_job_ids=remaining,
			progress={**task.metrics, "remaining": len(remaining)},
			pause_reason=str(state.get("pause_reason") or "") if status == "paused" else None,
		)
		if status == "paused":
			task.stop_reason = str(state.get("pause_reason") or "评分任务已暂停")
			task.stop_requested.set()

	score_config = dict(config)
	score_config["_workbench_stop_event"] = task.stop_requested
	score_config["_workbench_log"] = lambda message: _log(task, message)
	score_config["_workbench_score_progress"] = lambda state: _record_score_progress(task, state)
	score_config["_workbench_score_checkpoint"] = checkpoint

	if pool_loop:
		# FIFO pool-consumer mode: continuously score pending jobs as they arrive.
		# Idles (polls) when the pending pool is empty, resumes when new jobs land.
		from jobwinner.db import get_db, get_jobs_by_status
		from jobwinner.scoring_selection import select_scoring_jobs
		import time

		idle_seconds = float(config.get("_pool_poll_seconds") or 8)
		round_no = 0
		_log(task, "评分流水线已启动：持续消费待评分池（有岗位即评，无岗位空闲等待）")
		while not task.stop_requested.is_set():
			db = get_db()
			try:
				batch = select_scoring_jobs(db, scope="pending", limit=8)
				job_ids = [str(job["id"]) for job in batch]
			finally:
				db.close()
			if not job_ids:
				round_no += 1
				_log(task, f"待评分池为空，{int(idle_seconds)} 秒后再检查（第 {round_no} 次空闲）")
				# Idle-wait with stop-awareness
				deadline = time.monotonic() + idle_seconds
				while not task.stop_requested.is_set() and time.monotonic() < deadline:
					time.sleep(0.5)
				continue
			_log(task, f"评分池队列消费：本次取 {len(job_ids)} 个待评分岗位")
			try:
				score_jobs(
					score_config,
					scope="selected",
					limit=None,
					job_ids=job_ids,
					force_rescore=False,
				)
				# Auto-generate greetings for jobs that just became ready (scored above threshold).
				# This decouples greeting generation from manual confirm: by the time the user
				# confirms a job, its greeting already exists, so delivery never stalls.
				try:
					_db2 = get_db()
					try:
						ready_ids = [
							str(row["id"])
							for row in _db2.execute(
								"SELECT id FROM jobs WHERE id IN ({}) AND status='ready' AND (greeting IS NULL OR TRIM(greeting)='')".format(
									",".join("?" for _ in job_ids)
								),
								job_ids,
							).fetchall()
						]
					finally:
						_db2.close()
					if ready_ids:
						_log(task, f"评分完成，自动为 {len(ready_ids)} 个新通过岗位生成招呼语")
						from jobwinner.ai.greeter import generate_greetings
						greet_config = dict(score_config)
						greet_config.pop("_workbench_stop_event", None)
						greet_config["_workbench_job_ids"] = ready_ids
						generate_greetings(greet_config, include_ready=True)
						_log(task, f"自动生成招呼语完成：{len(ready_ids)} 个岗位")
				except Exception as greet_exc:
					_log(task, f"自动生成招呼语异常（不中断评分流水线）：{greet_exc}")
			except Exception as exc:
				_log(task, f"评分批次异常（不中断流水线）：{exc}")
				# Prevent a poison batch from spinning forever
				deadline = time.monotonic() + 3
				while not task.stop_requested.is_set() and time.monotonic() < deadline:
					time.sleep(0.5)
		_log(task, "评分流水线已停止")
		return

	_log(task, f"开始 AI 评分：{len(options.get('job_ids', []))} 个岗位")
	try:
		score_jobs(
			score_config,
			scope="selected",
			limit=None,
			job_ids=list(options.get("job_ids", [])),
			force_rescore=bool(options.get("force_rescore")),
		)
	except Exception as exc:
		update_scoring_run(db_path, run_id, status="failed", error=str(exc)[:1000])
		raise


def _queue_monitor_delivery(
	task: WorkbenchTask,
	job_ids: list[str],
	*,
	direct_send: bool = False,
) -> dict:
	"""Queue confirmed jobs on a task that is already in its monitor loop."""
	queue_lock = task.context.get("monitor_queue_lock")
	if queue_lock is None:
		queue_lock = Lock()
		task.context["monitor_queue_lock"] = queue_lock
	with queue_lock:
		pending = task.context.setdefault("pending_deliveries", [])
		queued_ids = {
			str(job_id)
			for batch in pending
			for job_id in batch.get("job_ids", [])
		}
		new_ids = [job_id for job_id in job_ids if job_id not in queued_ids]
		if new_ids:
			pending.append({"job_ids": new_ids, "direct_send": direct_send})
			_log(task, f"监测期间新增 {len(new_ids)} 个确认投递岗位，已加入发送队列")
	# 记录进入发送环节的岗位 id（前端“确认投递”区显示锁定·正在发送）
	sending_ids = set(task.context.get("sending_job_ids", []))
	sending_ids.update(str(job_id) for job_id in job_ids)
	task.context["sending_job_ids"] = sorted(sending_ids)
	# 前端需要即时“发送进行中”反馈：即使监测线程还没真正开始发，
	# 先按队列总量给出初始进度（本轮已发送 0 / 待发送 N）
	total_queued = sum(len(b.get("job_ids", [])) for b in pending)
	already_done = int(task.metrics.get("send_sent", 0) or 0) + int(task.metrics.get("send_failed", 0) or 0)
	task.metrics["send_total"] = total_queued + already_done
	task.metrics["send_remaining"] = total_queued
	task.metrics.setdefault("send_sent", 0)
	task.metrics.setdefault("send_failed", 0)
	wakeup_event = task.context.get("monitor_wakeup_event")
	if isinstance(wakeup_event, Event):
		wakeup_event.set()
	return task.snapshot()


def _take_monitor_deliveries(task: WorkbenchTask) -> list[dict]:
	queue_lock = task.context.get("monitor_queue_lock")
	if queue_lock is None:
		return []
	with queue_lock:
		pending = list(task.context.get("pending_deliveries", []))
		task.context["pending_deliveries"] = []
	return pending


def _execute_monitor(task: WorkbenchTask, config: dict) -> None:
	from jobwinner.executor.monitor import monitor_and_send_resumes

	monitor_config = dict(config)
	monitor_config["_workbench_stop_event"] = task.stop_requested
	interval_min = int(config.get("monitor", {}).get("interval", 30) or 30)
	interval_sec = max(interval_min * 60, 1)
	queue_lock = task.context.setdefault("monitor_queue_lock", Lock())
	wakeup_event = task.context.setdefault("monitor_wakeup_event", Event())
	task.context["monitoring"] = True
	try:
		while not task.stop_requested.is_set():
			for batch in _take_monitor_deliveries(task):
				deliver_config = dict(config)
				deliver_config["_workbench_job_ids"] = batch.get("job_ids", [])
				if batch.get("direct_send"):
					deliver_config["_workbench_skip_greeting"] = True
				_log(task, f"处理监测期间新增的 {len(deliver_config['_workbench_job_ids'])} 个投递岗位")
				_execute_deliver(task, deliver_config)
				if task.stop_requested.is_set():
					return
			_log(task, "执行一轮监测")
			monitor_summary = monitor_and_send_resumes(monitor_config)
			if isinstance(monitor_summary, dict):
				task.metrics.update({
					"monitor_replied": int(monitor_summary.get("replied") or 0),
					"monitor_pending": int(monitor_summary.get("needs_resume") or 0),
					"monitor_rejected": int(monitor_summary.get("rejected") or 0),
					"monitor_checks": int(task.metrics.get("monitor_checks") or 0) + 1,
				})
			if task.stop_requested.is_set():
				return
			# 官网投递进度巡检：顺带检查各官网投递记录页，写回 stage
			from jobwinner.executor.portal_tracker import check_portal_progress
			try:
				check_portal_progress(stop_event=task.stop_requested, log=lambda m: _log(task, m))
			except Exception as exc:
				_log(task, f"官网进度巡检异常：{exc}")
			if task.stop_requested.is_set():
				return
			_log(task, f"本轮监测完成，{interval_min} 分钟后再次检查")
			wakeup_event.wait(interval_sec)
			wakeup_event.clear()
	finally:
		task.context["monitoring"] = False
		task.context.pop("monitor_wakeup_event", None)
		task.context.pop("monitor_queue_lock", None)


def _execute_conveyor(task: WorkbenchTask, config: dict) -> None:
	"""Resident conveyor: collector + scorer + monitor threads run in parallel
	inside ONE task. Scorer FIFO-consumes the pending pool; collector loops over
	search combos and re-scans periodically; monitor watches for HR replies.
	All three keep running until the task is stopped (挂机模式).

	    collector(常驻重扫) ──→ DB(pending池) ←─ scorer(轮询消费)
	          └────────── monitor(监听HR回复) ────────┘
	"""
	import time
	from threading import Thread

	thread_errors: list[Exception] = []
	collect_interval_sec = int(float(config.get("search", {}).get("collect_rescan_minutes") or 10) * 60)

	# ── Collector thread: loop over all combos, then idle and rescan ──
	def collector_run() -> None:
		try:
			from jobwinner.scraper.jobs import scrape_jobs
			keywords = config.get("search", {}).get("keywords", [])
			collect_config = dict(config)
			collect_config["_workbench_stop_event"] = task.stop_requested
			collect_config["_workbench_collect_progress"] = lambda state: _record_collect_progress(task, state)
			_log(task, f"采集线程启动：常驻循环（关键词 {len(keywords)} 个，扫完一轮休息 {int(collect_interval_sec // 60)} 分钟再扫）")
			while not task.stop_requested.is_set():
				_log(task, "采集线程：开始一轮全量扫描")
				scrape_jobs(collect_config, keywords, limit=None)
				if task.stop_requested.is_set():
					break
				_log(task, f"采集线程：本轮扫描完成，{int(collect_interval_sec // 60)} 分钟后重扫（持续找新岗位）")
				# Idle-wait with stop-awareness
				deadline = time.monotonic() + collect_interval_sec
				while not task.stop_requested.is_set() and time.monotonic() < deadline:
					time.sleep(1)
		except Exception as exc:
			thread_errors.append(exc)
			_log(task, f"采集线程异常：{exc}")

	# ── Scorer thread: reuse the pool_loop mode of _execute_score ────
	def scorer_run() -> None:
		try:
			score_config = dict(config)
			score_config["_pool_loop"] = True
			score_config["_pool_poll_seconds"] = float(config.get("search", {}).get("pool_poll_seconds") or 8)
			_execute_score(task, score_config)
		except Exception as exc:
			thread_errors.append(exc)
			_log(task, f"评分线程异常：{exc}")

	# ── Monitor thread ──────────────────────────────────────────────
	def monitor_run() -> None:
		try:
			_execute_monitor(task, config)
		except Exception as exc:
			thread_errors.append(exc)
			_log(task, f"监测线程异常：{exc}")

	t1 = Thread(target=collector_run, name="resident-collect", daemon=True)
	t2 = Thread(target=scorer_run, name="resident-score", daemon=True)
	t3 = Thread(target=monitor_run, name="resident-monitor", daemon=True)
	task.context["conveyor_threads"] = [t1, t2, t3]
	t1.start()
	t2.start()
	t3.start()

	_log(task, "全流程常驻已启动：采集 + 评分 + 监测 三线程并行（可挂机）")
	t1.join()
	if task.stop_requested.is_set():
		_log(task, "任务停止请求，等待评分/监测线程退出...")
	t2.join()
	t3.join()
	if thread_errors:
		_log(task, f"常驻流程结束（有异常 {len(thread_errors)} 个）")
	else:
		_log(task, "常驻流程结束")


def _execute_full(task: WorkbenchTask, config: dict) -> None:
	db = _get_web_db()
	try:
		deferred_job_ids = [str(job["id"]) for job in get_jobs_ready_to_send(db)]
	finally:
		db.close()
	if deferred_job_ids:
		_log(task, f"优先续发上次已确认但未完成的 {len(deferred_job_ids)} 个岗位")
		deferred_config = load_config(CONFIG_PATH)
		deferred_config["_workbench_job_ids"] = deferred_job_ids
		deferred_config["_workbench_skip_greeting"] = True
		_execute_deliver(task, deferred_config)
		if task.stop_requested.is_set():
			return

	_execute_collect(task, config)
	if task.stop_requested.is_set():
		return

	db = _get_web_db()
	try:
		threshold = int(config.get("scoring", {}).get("threshold", 60) or 60)
		pending_confirmation = [
			job for job in get_jobs_pending_confirmation(db)
			if int(job.get("score") or 0) >= threshold
		]
	finally:
		db.close()
	if not pending_confirmation:
		task.context["waiting_confirmation"] = False
		task.context["confirmation_complete"] = True
		_log(task, "没有待确认岗位，流程结束")
		return

	confirmation_event = Event()
	task.context["confirmation_event"] = confirmation_event
	task.context["waiting_confirmation"] = True
	if task.context.get("delivery_requested"):
		confirmation_event.set()
	_log(task, "等待前端确认投递")
	while not task.stop_requested.is_set() and not confirmation_event.wait(0.5):
		pass
	if task.stop_requested.is_set():
		return

	job_ids = [str(job_id) for job_id in task.context.get("confirmed_job_ids", []) if str(job_id)]
	task.context["waiting_confirmation"] = False
	task.context["confirmation_complete"] = True
	task.context["delivery_requested"] = False
	if not job_ids:
		_log(task, "未收到前端确认岗位，流程结束")
		return

	_log(task, f"前端已确认 {len(job_ids)} 个岗位，继续投递")
	# The user may adjust the daily limit or other send settings while reviewing
	# jobs. Reload immediately before delivery instead of using the task-start snapshot.
	deliver_config = load_config(CONFIG_PATH)
	deliver_config["_workbench_job_ids"] = job_ids
	_execute_deliver(task, deliver_config)
	if task.stop_requested.is_set():
		return
	_execute_monitor(task, load_config(CONFIG_PATH))


def _queue_active_delivery(
	task: WorkbenchTask,
	job_ids: list[str],
	*,
	direct_send: bool,
) -> tuple[dict, list[str]]:
	"""Append jobs to the currently running single delivery worker."""
	queue_lock = task.context.setdefault("delivery_queue_lock", Lock())
	with queue_lock:
		if not task.context.get("delivering"):
			raise TaskAlreadyRunningError("当前发送任务即将结束，请稍后重试")
		scheduled_ids = task.context.setdefault("delivery_scheduled_ids", set())
		new_ids = [job_id for job_id in job_ids if job_id not in scheduled_ids]
		already_queued_count = len(job_ids) - len(new_ids)
		if new_ids:
			task.context.setdefault("pending_deliveries", []).append({
				"job_ids": new_ids,
				"direct_send": direct_send,
			})
			scheduled_ids.update(new_ids)
			_log(task, f"新增 {len(new_ids)} 个岗位，已加入当前发送队列")
		elif already_queued_count:
			_log(task, f"所选 {already_queued_count} 个岗位已在当前发送队列中")
	payload = task.snapshot()
	payload["queued_count"] = len(new_ids)
	payload["already_queued_count"] = already_queued_count
	return payload, new_ids


def _take_active_delivery(task: WorkbenchTask) -> dict | None:
	queue_lock = task.context.get("delivery_queue_lock")
	if queue_lock is None:
		return None
	with queue_lock:
		pending = task.context.setdefault("pending_deliveries", [])
		if pending:
			return pending.pop(0)
		task.context["delivering"] = False
		return None


def _execute_deliver(task: WorkbenchTask, config: dict) -> None:
	"""Run one delivery worker and drain batches queued while it is active."""
	queue_lock = task.context.setdefault("delivery_queue_lock", Lock())
	with queue_lock:
		task.context["delivering"] = True
		task.context.setdefault("pending_deliveries", [])
		task.context.setdefault("delivery_scheduled_ids", set()).update(
			str(job_id) for job_id in config.get("_workbench_job_ids", []) if str(job_id)
		)

	current_config = config
	try:
		while not task.stop_requested.is_set():
			_execute_deliver_batch(task, current_config)
			if task.stop_requested.is_set():
				return
			batch = _take_active_delivery(task)
			if not batch:
				return
			current_config = dict(config)
			current_config["_workbench_job_ids"] = batch.get("job_ids", [])
			if batch.get("direct_send"):
				current_config["_workbench_skip_greeting"] = True
			else:
				current_config.pop("_workbench_skip_greeting", None)
			_log(task, f"继续处理队列中的 {len(current_config['_workbench_job_ids'])} 个岗位")
	finally:
		with queue_lock:
			task.context["delivering"] = False
			task.context.pop("delivery_scheduled_ids", None)
			task.context.pop("pending_deliveries", None)


def _execute_deliver_batch(task: WorkbenchTask, config: dict) -> None:
	from jobwinner.ai.greeter import generate_greetings
	from jobwinner.executor.sender import send_greetings

	config = dict(config)
	config["_workbench_stop_event"] = task.stop_requested
	config["_workbench_log"] = lambda message: _log(task, message)

	def _send_progress(state: dict) -> None:
		"""Live per-job send progress → task metrics + real-time log (吃进度条)."""
		done = int(state.get("done") or 0)
		total = int(state.get("total") or 0)
		sent = int(state.get("sent") or 0)
		failed = int(state.get("failed") or 0)
		phase = str(state.get("phase") or "sending")
		company = str(state.get("current_company") or "")
		title = str(state.get("current_title") or "")
		task.metrics.update({
			"send_sent": sent,
			"send_failed": failed,
			"send_total": total,
			"send_remaining": max(total - done, 0),
			"send_phase": phase,
		})
		phase_label = {"browsing": "模拟浏览中", "opening": "打开岗位页中", "sending": "正在发送中"}.get(phase, "发送中")
		if phase == "browsing":
			_log(task, f"{phase_label}：{company}｜{title}（已发 {sent}/{total}，剩余 {max(total - done, 0)}）")
		elif company:
			_log(task, f"{phase_label}：{company}｜{title}（已发 {sent}/{total}，剩余 {max(total - done, 0)}）")
		else:
			_log(task, f"发送进度：已发 {sent}/{total}，待发送 {max(total - done, 0)}")

	config["_workbench_send_progress"] = _send_progress
	selected_job_ids = [str(job_id) for job_id in config.get("_workbench_job_ids", []) if str(job_id)]
	if not config.get("_workbench_skip_greeting"):
		_log(task, "生成招呼语")
		generated_count = generate_greetings(config, include_ready=True)
		_log(task, f"招呼语生成完成：{generated_count}/{len(selected_job_ids) or generated_count}")
		if task.stop_requested.is_set():
			return
		if selected_job_ids and generated_count != len(selected_job_ids):
			raise RuntimeError(
				f"招呼语生成失败：选择 {len(selected_job_ids)} 个岗位，仅成功生成 {generated_count} 条；未发送任何消息"
			)
	_log(task, "发送招呼语")
	sent_count = send_greetings(config, force=True)
	report = config.get("_workbench_send_report", {})
	failed_count = int(report.get("failed_count", 0) or 0)
	deferred_count = int(report.get("deferred_count", 0) or 0)
	quota_deferred_count = min(
		int(report.get("quota_deferred_count", 0) or 0),
		deferred_count,
	)
	paused_count = max(deferred_count - quota_deferred_count, 0)
	total_count = len(selected_job_ids) or sent_count + failed_count + deferred_count
	_log(
		task,
		f"招呼语发送结果：成功 {sent_count}，失败 {failed_count}，待下次发送 {deferred_count}（共 {total_count}）",
	)
	task.metrics.update({
		"send_sent": int(sent_count or 0),
		"send_failed": int(failed_count or 0),
		"send_deferred": int(deferred_count or 0),
		"send_total": int(total_count or 0),
		"send_remaining": 0,
	})
	# 本批岗位处理完毕（成功/失败/顺延回池），解除“锁定·正在发送”标记
	done_ids = set(str(job_id) for job_id in selected_job_ids if str(job_id))
	if done_ids:
		remaining_sending = [job_id for job_id in task.context.get("sending_job_ids", []) if job_id not in done_ids]
		task.context["sending_job_ids"] = remaining_sending
	if failed_count:
		_log(task, f"{failed_count} 个岗位发送失败已单独记录，继续后续流程")
	if quota_deferred_count:
		_log(task, f"{quota_deferred_count} 个岗位因今日发送额度未执行，已保留在“待发送招呼语”")
	if paused_count:
		_log(task, f"{paused_count} 个岗位本轮未执行，已保留在“待发送招呼语”")

	stop_reason = report.get("stop_reason")
	if stop_reason in {"captcha", "rate_limit", "blocked", "consecutive_errors"}:
		reason_labels = {
			"captcha": "验证码",
			"rate_limit": "频率限制",
			"blocked": "账号或请求被拦截",
			"consecutive_errors": "连续错误过多",
		}
		raise RuntimeError(f"发送已安全暂停：检测到{reason_labels[stop_reason]}")


task_runner._executors.update({
	"full": _execute_conveyor,
	"collect": _execute_collect,
	"rescore": _execute_rescore,
	"score": _execute_score,
	"monitor": _execute_monitor,
	"deliver": _execute_deliver,
})


# ─── Health ───────────────────────────────────────────────

@app.route("/api/health")
def health():
	return _json_response({"status": "ok", "version": __version__})


# ─── Dashboard APIs ──────────────────────────────────────

@app.route("/api/funnel")
def api_funnel():
	db = _get_web_db()
	try:
		data = get_funnel_stats(db)
		return _json_response(data)
	finally:
		db.close()


@app.route("/api/stats")
def api_stats():
	db = _get_web_db()
	try:
		data = get_stats(db)
		return _json_response(data)
	finally:
		db.close()


@app.route("/api/activity")
def api_activity():
	days = int(request.params.get("days", 7))
	db = _get_web_db()
	try:
		data = get_daily_activity(db, days)
		return _json_response(data)
	finally:
		db.close()


@app.route("/api/jobs")
def api_jobs():
	try:
		deleted = request.params.get("deleted", "active").strip()
		limit = int(request.params.get("limit", 100))
		offset = int(request.params.get("offset", 0))
		channel = (request.params.get("channel") or "").strip() or None
		if deleted not in {"active", "only", "all"} or not 1 <= limit <= 500 or offset < 0:
			raise ValueError("岗位查询参数无效")
	except (TypeError, ValueError) as exc:
		return _json_response({"error": str(exc)}, 400)

	db = _get_web_db()
	try:
		jobs, total = query_jobs(db, deleted=deleted, limit=limit, offset=offset, channel=channel)
		response.headers["X-Total-Count"] = str(total)
		return _json_response(jobs)
	finally:
		db.close()


@app.route("/api/progress")
def api_progress():
	"""Return jobs with real recruiting progress (sent+) with stage + timeline."""
	from jobwinner.db import get_application_progress
	db = _get_web_db()
	try:
		return _json_response({"jobs": get_application_progress(db)})
	finally:
		db.close()


_PORTAL_HOMEPAGES = {
	"恒玄": "https://www.bestechnic.com",
	"华为": "https://career.huawei.com",
	"南芯": "https://www.southchip.com",
	"联发": "https://mediatek.com",
	"芯动": "https://www.innosilicon.com",
	"兆易": "https://www.gigadevice.com",
	"歌尔": "https://www.goertek.com",
	"Texas": "https://www.ti.com.cn",
	"艾为": "https://www.awinic.com",
	"恩智浦": "https://www.nxp.com",
	"芯原": "https://www.verisilicon.com",
}


def _portal_homepage(company: str) -> str:
	"""公司名 → 官网主页 URL（用于 md 导出的公司官网清单）。"""
	for key, home in _PORTAL_HOMEPAGES.items():
		if key in company:
			return home
	return ""


@app.route("/api/progress/export", method="POST")
def api_progress_export():
	"""Export the progress board to the Markdown tracking file (tmp/胡良玮_2027届秋招投递跟踪.md)."""
	import sqlite3
	import datetime
	db = _get_web_db()
	try:
		portal = db.execute(
			"SELECT * FROM jobs WHERE source='portal' AND deleted_at IS NULL ORDER BY applied_date DESC"
		).fetchall()
		boss_sent = db.execute(
			"SELECT j.* FROM jobs j WHERE j.source!='portal' AND j.deleted_at IS NULL "
			"AND j.status IN ('sent','replied','resume_sent','needs_resume','follow_up_sent','rejected','error')"
		).fetchall()
		portal = [dict(r) for r in portal]
		boss_sent = [dict(r) for r in boss_sent]
	finally:
		db.close()

	today = datetime.date.today().isoformat()
	lines = [
		"# 胡良玮｜2027届秋招投递跟踪", "",
		f"> 更新时间：{today}", "> 数据源：JobWinner 进度看板（BOSS直聘 + 官网投递）自动生成", "",
		"## 当前概览", "",
		f"- 官网已投公司：**{len({r['company'] for r in portal})}**，岗位：**{len(portal)}**",
		f"- BOSS 已投岗位：**{len(boss_sent)}**",
		f"- 合计已投岗位：**{len(portal) + len(boss_sent)}**", "",
		"## 官网投递（企业官网）", "",
		"| 公司 | 岗位 | 编号 | 城市 | 状态 | 优先级 | 投递日期 | 官网链接 |",
		"|---|---|---|---|---|---|---|---|",
	]
	for r in portal:
		url = str(r.get("url") or "")
		link = f"[投递页]({url})" if url and not url.startswith("portal://") else ""
		src = r.get("stage_source") or ""
		src_label = {"auto": "官网同步", "manual": "手动标注", "unknown": "查不到"}.get(src, src or "—")
		lines.append(
			f"| {r['company']} | {r['title']} | {r.get('jd') or ''} | {r.get('city') or ''} | {r.get('stage') or '已投递'}({src_label}) | {r.get('priority') or ''} | {r.get('applied_date') or (r.get('created_at') or '')[:10]} | {link} |"
		)
	lines += ["", "## 公司官网（投递与进度来源）", "", "| 公司 | 官网主页 | 投递/记录页 |", "|---|---|---|"]
	# 公司官网主页映射（官方域名）
	company_homepages = {
		r["company"]: _portal_homepage(r["company"])
		for r in portal
	}
	seen = set()
	for r in portal:
		if r["company"] in seen:
			continue
		seen.add(r["company"])
		u = str(r.get("url") or "")
		home = company_homepages[r["company"]]
		home_link = f"[官网]({home})" if home else ""
		rec_link = f"[记录页]({u})" if u and not u.startswith("portal://") else ""
		lines.append(f"| {r['company']} | {home_link} | {rec_link} |")
	lines += ["", "## BOSS 直聘已投递", "", "| 公司 | 岗位 | 城市 | 分数 | 状态 |", "|---|---|---|---|---|"]
	for r in boss_sent:
		lines.append(
			f"| {r['company']} | {r['title']} | {r.get('city') or ''} | {r.get('score') or ''} | {r.get('stage') or '已投递'} |"
		)
	lines.append("")

	export_dir = BASE_DIR / "tmp"
	export_dir.mkdir(parents=True, exist_ok=True)
	export_path = export_dir / "胡良玮_2027届秋招投递跟踪.md"
	export_path.write_text("\n".join(lines), encoding="utf-8")
	return _json_response({"ok": True, "path": str(export_path), "count": len(portal) + len(boss_sent)})


@app.route("/api/jobs/<job_id>/stage", method="POST")
def api_job_stage(job_id: str):
	"""Update a job's recruiting stage (筛选/笔试/面试/谈薪/Offer/Rejected...).

	stage_source: 'manual'(手动标注, 默认) / 'unknown'(官网查不到) / 'auto'(官网巡检, 仅后台用)。
	Empty stage clears the stage (user went back to '已投递').
	"""
	from jobwinner.db import set_job_stage
	body = request.json or {}
	if not isinstance(body, dict):
		return _json_response({"error": "请求体必须是对象"}, 400)
	stage = str(body.get("stage") or "").strip()
	if len(stage) > 50:
		return _json_response({"error": "stage 必须 ≤50字符"}, 400)
	note = str(body.get("note") or "").strip() or None
	stage_source = str(body.get("stage_source") or "manual").strip() or "manual"
	if stage_source not in {"manual", "unknown", "auto"}:
		return _json_response({"error": "stage_source 必须是 manual/unknown/auto"}, 400)
	db = _get_web_db()
	try:
		ok = set_job_stage(db, job_id, stage, note, stage_source=stage_source)
		if not ok:
			return _json_response({"error": "岗位不存在"}, 404)
		return _json_response({"ok": True, "job_id": job_id, "stage": stage, "stage_source": stage_source})
	finally:
		db.close()


@app.route("/api/jobs/portal", method="POST")
def api_job_portal_add():
	"""Add a company-portal job application (官网投递) to the progress board."""
	from jobwinner.db import add_portal_application
	body = request.json or {}
	if not isinstance(body, dict):
		return _json_response({"error": "请求体必须是对象"}, 400)
	company = str(body.get("company") or "").strip()
	title = str(body.get("title") or "").strip()
	if not company or not title:
		return _json_response({"error": "公司名和岗位名不能为空"}, 400)
	db = _get_web_db()
	try:
		job_id = add_portal_application(
			db,
			company=company,
			title=title,
			url=str(body.get("url") or "").strip(),
			city=str(body.get("city") or "").strip(),
			priority=str(body.get("priority") or "").strip(),
			stage=str(body.get("stage") or "").strip(),
			notes=str(body.get("notes") or "").strip(),
			applied_date=str(body.get("applied_date") or "").strip(),
		)
		return _json_response({"ok": True, "job_id": job_id})
	finally:
		db.close()


def _optional_float_param(name: str, *, minimum: float = 0, maximum: float | None = None):
	raw_value = request.params.get(name)
	if raw_value in (None, ""):
		return None
	try:
		value = float(raw_value)
	except (TypeError, ValueError) as exc:
		raise ValueError(f"{name} 必须是数字") from exc
	if not math.isfinite(value):
		raise ValueError(f"{name} 必须是有限数字")
	if value < minimum or (maximum is not None and value > maximum):
		raise ValueError(f"{name} 超出允许范围")
	return value


def _integer_param(name: str, default: int, *, minimum: int, maximum: int | None = None):
	raw_value = request.params.get(name, str(default))
	try:
		value = int(raw_value)
	except (TypeError, ValueError) as exc:
		raise ValueError(f"{name} 必须是整数") from exc
	if value < minimum or (maximum is not None and value > maximum):
		raise ValueError(f"{name} 超出允许范围")
	return value


@app.route("/api/jobs/search")
def api_job_search():
	try:
		minimum_score = _optional_float_param("min_score", maximum=100)
		salary_min = _optional_float_param("salary_min")
		salary_max = _optional_float_param("salary_max")
		limit = _integer_param("limit", 15, minimum=1, maximum=100)
		offset = _integer_param("offset", 0, minimum=0)
		if salary_min is not None and salary_max is not None and salary_min > salary_max:
			raise ValueError("最低薪资不能高于最高薪资")
		created_within = request.params.get("created_within", "").strip()
		if created_within and created_within not in {"today", "3d", "7d"}:
			raise ValueError("created_within 参数无效")
	except ValueError as exc:
		return _json_response({"error": str(exc)}, 400)

	query = "SELECT * FROM jobs"
	conditions = ["deleted_at IS NULL"]
	params = []
	keyword = (request.query.getunicode("q") or "").strip()
	status_filter = request.params.get("status", "").strip()
	if keyword:
		conditions.append("(title LIKE ? OR company LIKE ? OR jd LIKE ? OR score_reason LIKE ?)")
		keyword_param = f"%{keyword}%"
		params.extend([keyword_param] * 4)
	if minimum_score is not None:
		conditions.append("score >= ?")
		params.append(minimum_score)
	if status_filter:
		conditions.append("status = ?")
		params.append(status_filter)
	if created_within == "today":
		conditions.append("created_at >= datetime('now', 'localtime', 'start of day', 'utc')")
	elif created_within == "3d":
		conditions.append("created_at >= datetime('now', '-3 days')")
	elif created_within == "7d":
		conditions.append("created_at >= datetime('now', '-7 days')")
	if conditions:
		query += " WHERE " + " AND ".join(conditions)
	query += " ORDER BY created_at DESC, score DESC"

	db = _get_web_db()
	try:
		all_total = db.execute("SELECT COUNT(*) FROM jobs WHERE deleted_at IS NULL").fetchone()[0]
		rows = [dict(row) for row in db.execute(query, params).fetchall()]
		if salary_min is not None or salary_max is not None:
			filtered_rows = []
			for row in rows:
				salary_range = parse_monthly_salary_k(row.get("salary", ""))
				if salary_range is None:
					continue
				job_min, job_max = salary_range
				if salary_min is not None and job_max < salary_min:
					continue
				if salary_max is not None and job_min > salary_max:
					continue
				filtered_rows.append(row)
			rows = filtered_rows
		total = len(rows)
		return _json_response({
			"items": rows[offset:offset + limit],
			"total": total,
			"all_total": all_total,
			"limit": limit,
			"offset": offset,
		})
	finally:
		db.close()


@app.route("/api/top-companies")
def api_top_companies():
	limit = int(request.params.get("limit", 5))
	db = _get_web_db()
	try:
		data = get_top_companies(db, limit)
		return _json_response(data)
	finally:
		db.close()


@app.route("/api/history")
def api_history():
	limit = int(request.params.get("limit", 15))
	include_unresolved = request.params.get("include_unresolved", "").lower() in ("1", "true", "yes")
	db = _get_web_db()
	try:
		data = get_recent_history(db, limit)
		if include_unresolved:
			seen_ids = {item["id"] for item in data}
			data.extend(
				item
				for item in get_unresolved_resume_failures(db)
				if item["id"] not in seen_ids
			)
			data.sort(
				key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)),
				reverse=True,
			)
		return _json_response(_serialize_history_items(data))
	finally:
		db.close()


@app.route("/api/history/unresolved-replies/count")
def api_history_unresolved_replies_count():
	db = _get_web_db()
	try:
		return _json_response({"count": count_unresolved_monitor_items(db)})
	finally:
		db.close()


def _send_window_info(config):
	from jobwinner.throttle import SendWindowChecker
	windows = config.get("throttle", {}).get("send_windows", ["09:00-16:00"])
	checker = SendWindowChecker(windows)
	return {
		"windows": list(windows),
		"active": bool(checker.is_active()),
		"next": checker.next_window_info(),
	}


def _merge_pending_greetings(db) -> list[dict]:
	"""待发送列表 = 正常待发岗位（ready/approved）＋ 发送失败岗位（error）。
	发送失败岗位保留在待发送列表并附 last_error（最近一次失败原因），
	前端据此显示「发送失败」标记；它们不会自动重发（get_jobs_ready_to_send
	仍只选 ready/approved），需要用户手动勾选重试。"""
	jobs = list(get_jobs_ready_to_send(db))
	failed = get_jobs_with_send_errors(db)
	if failed:
		for job in failed:
			row = db.execute(
				"SELECT detail FROM history WHERE job_id=? AND action='error' ORDER BY created_at DESC LIMIT 1",
				(job["id"],),
			).fetchone()
			job["last_error"] = row["detail"] if row else ("发送失败(%s)" % job.get("status", "error"))
		jobs.extend(failed)
	return jobs


@app.route("/api/workbench")
def api_workbench():
	db = _get_web_db()
	try:
		threshold = load_config(CONFIG_PATH).get("scoring", {}).get("threshold", 60)
		status = task_runner.status()
		return _json_response({
			"funnel": get_funnel_stats(db),
			"funnel_today": get_funnel_stats(db, today=True),
			"pending_confirmation": [
				job for job in get_jobs_pending_confirmation(db)
				if int(job.get("score") or 0) >= threshold
			],
			"pending_greetings": _merge_pending_greetings(db),
			"send_errors": get_jobs_with_send_errors(db),
			"pending_scoring_count": len(select_scoring_jobs(db, scope="pending", limit=None)),
			"send_window": _send_window_info(load_config(CONFIG_PATH)),
			"needs_resume": get_jobs_needing_resume(db),
			"resume_pending": get_jobs_resume_pending(db),
			"task": status["active"],
			"active_tasks": status.get("active_tasks", []),
			"last_task": status["last_task"],
		})
	finally:
		db.close()


@app.route("/api/workbench/preflight")
def api_workbench_preflight():
	mode = request.params.get("mode", "")
	try:
		config = load_config(CONFIG_PATH)
		checks = collect_preflight_checks(mode, config)
		messages = error_messages(checks)
		return _json_response({"ok": not messages, "messages": messages, "checks": checks})
	except Exception as e:
		return _json_response({"ok": False, "messages": [str(e)]}, 500)


@app.route("/api/diagnostics/ai")
def api_ai_diagnostics():
	try:
		checks = check_ai_connection(load_config(CONFIG_PATH), required=True)
		messages = error_messages(checks)
		return _json_response({"ok": not messages, "messages": messages, "checks": checks})
	except Exception as e:
		return _json_response({"ok": False, "messages": [str(e)]}, 500)


# ─── 平台登录管理 ──────────────────────────────────────────
# 各平台预设：后续增加猎聘/智联等只需在此追加条目。
PLATFORM_PRESETS = [
	{
		"key": "boss",
		"name": "BOSS 直聘",
		"url": "https://www.zhipin.com/",
		"login_markers": ("/login", "/signin", "/user/", "passport", "user/login"),
	},
]


@app.route("/api/platforms/login")
def api_platforms_login():
	"""返回各平台登录状态：浏览器连接情况 + BOSS 直聘页面是否打开/是否登录。"""
	from jobwinner.browser.diagnostics import run_browser_diagnostics
	try:
		config = load_config(CONFIG_PATH)
		diag = run_browser_diagnostics(config)
		boss_tab = diag.get("boss_tab")
		platforms = []
		for preset in PLATFORM_PRESETS:
			tab = boss_tab if preset["key"] == "boss" else None
			url = str((tab or {}).get("url") or "")
			lower_url = url.lower()
			logged_in = bool(tab) and not any(
				marker in lower_url for marker in preset["login_markers"]
			)
			login_hint = ""
			if tab and not logged_in:
				login_hint = f"已打开 {preset['name']}，但当前页面疑似登录页，请在 Chrome 中完成登录后再刷新检测。"
			elif not tab and diag.get("chrome"):
				login_hint = f"Chrome 已连接，但未发现已打开的 {preset['name']} 页面，可点击“打开页面”。"
			platforms.append({
				"key": preset["key"],
				"name": preset["name"],
				"url": preset["url"],
				"opened": bool(tab),
				"logged_in": logged_in,
				"tab_url": url or None,
				"tab_title": str((tab or {}).get("title") or "") or None,
				"login_hint": login_hint,
			})
		return _json_response({
			"ok": True,
			"runtime": bool(diag.get("runtime")),
			"chrome": bool(diag.get("chrome")),
			"browser_name": (diag.get("browser_name") or diag.get("browser_product") or ""),
			"errors": diag.get("errors") or [],
			"platforms": platforms,
		})
	except Exception as e:
		return _json_response({"ok": False, "error": str(e)}, 500)


@app.route("/api/platforms/login/open", method="POST")
def api_platforms_login_open():
	"""在已连接的 Chrome 中打开指定平台首页，便于手动登录。"""
	from jobwinner.browser import new_tab
	try:
		body = request.json or {}
		key = str(body.get("platform") or "boss")
		preset = next((p for p in PLATFORM_PRESETS if p["key"] == key), None)
		if not preset:
			return _json_response({"ok": False, "error": f"未知平台：{key}"}, 400)
		target_id = new_tab(preset["url"], background=False)
		if not target_id:
			return _json_response(
				{"ok": False, "error": "无法打开页面：未连接 Chrome 或浏览器运行组件不可用。请先启动带远程调试的 Google Chrome。"},
				409,
			)
		return _json_response({"ok": True, "platform": key, "url": preset["url"], "target_id": target_id})
	except Exception as e:
		return _json_response({"ok": False, "error": str(e)}, 500)


def _scoring_options_from_body(body: dict) -> dict:
	raw_options = body.get("options", body)
	if not isinstance(raw_options, dict):
		raise ValueError("评分参数必须是对象")
	return validate_options(
		raw_options.get("scope", "pending"),
		raw_options.get("limit"),
		raw_options.get("job_ids", []),
		raw_options.get("force_rescore", False),
	)


@app.route("/api/scoring/preview", method="POST")
def api_scoring_preview():
	try:
		body = request.json or {}
		if not isinstance(body, dict):
			raise ValueError("请求体必须是对象")
		options = _scoring_options_from_body(body)
		config = load_config(CONFIG_PATH)
		max_attempts = config.get("ai", {}).get("scoring_max_attempts", 2)
		db = _get_web_db()
		try:
			return _json_response(preview_scoring(db, **options, max_attempts_per_job=max_attempts))
		finally:
			db.close()
	except ValueError as exc:
		return _json_response({"error": str(exc)}, 400)


@app.route("/api/scoring/start", method="POST")
def api_scoring_start():
	run_id = str(uuid4())
	db_path = DATA_DIR / "jobwinner.db"
	try:
		body = request.json or {}
		if not isinstance(body, dict):
			raise ValueError("请求体必须是对象")
		options = _scoring_options_from_body(body)
		config = load_config(CONFIG_PATH)
		messages = _preflight_messages("rescore", config)
		if messages:
			return _json_response({"error": "请先处理评分启动检查", "messages": messages}, 400)
		active_runs = [run for run in list_scoring_runs(db_path, limit=100) if run.get("status") in {"running", "paused"}]
		if active_runs:
			return _json_response({"error": "已有独立评分任务正在运行或等待恢复，请先继续或结束该任务"}, 409)
		db = _get_web_db()
		try:
			selected = select_scoring_jobs(db, **options)
		finally:
			db.close()
		job_ids = [str(job["id"]) for job in selected]
		if not job_ids:
			return _json_response({"error": "没有符合条件的待评分岗位"}, 400)
		stored_options = {
			"scope": options["scope"],
			"limit": options["limit"],
			"force_rescore": options["force_rescore"],
		}
		create_scoring_run(db_path, run_id=run_id, options=stored_options, job_ids=job_ids)
		runtime_options = {"job_ids": job_ids, "force_rescore": options["force_rescore"]}
		with job_mutation_lock:
			update_scoring_run(db_path, run_id, status="running")
			task = task_runner.start("score", _task_config({
				"_score_run_id": run_id,
				"_score_options": runtime_options,
			}))
		update_scoring_run(db_path, run_id, task_id=str(task["id"]))
		return _json_response({"run": get_scoring_run(db_path, run_id), "task": task})
	except TaskAlreadyRunningError as exc:
		update_scoring_run(db_path, run_id, status="stopped", error=str(exc))
		return _json_response({"error": str(exc)}, 409)
	except ValueError as exc:
		return _json_response({"error": str(exc)}, 400)
	except Exception as exc:
		update_scoring_run(db_path, run_id, status="failed", error=str(exc)[:1000])
		return _json_response({"error": "启动评分失败"}, 500)


@app.route("/api/scoring/runs")
def api_scoring_runs():
	return _json_response(list_scoring_runs(DATA_DIR / "jobwinner.db"))


@app.route("/api/scoring/runs/<run_id>/pause", method="POST")
def api_scoring_pause(run_id):
	db_path = DATA_DIR / "jobwinner.db"
	run = get_scoring_run(db_path, run_id)
	if not run:
		return _json_response({"error": "评分任务不存在"}, 404)
	if run.get("status") != "running":
		return _json_response(run)
	try:
		task = task_runner.stop(str(run.get("task_id") or ""), "用户暂停独立评分")
	except KeyError:
		task = {"status": "stopped"}
	latest = get_scoring_run(db_path, run_id) or run
	if task.get("status") not in {"completed", "failed"} and latest.get("remaining_job_ids"):
		latest = update_scoring_run(db_path, run_id, status="paused", pause_reason="用户暂停独立评分") or latest
	return _json_response(latest)


@app.route("/api/scoring/runs/<run_id>/resume", method="POST")
def api_scoring_resume(run_id):
	db_path = DATA_DIR / "jobwinner.db"
	run = get_scoring_run(db_path, run_id)
	if not run:
		return _json_response({"error": "评分任务不存在"}, 404)
	if run.get("status") != "paused":
		return _json_response({"error": "只有已暂停的评分任务可以恢复"}, 409)
	remaining = [str(job_id) for job_id in run.get("remaining_job_ids", []) if str(job_id)]
	if not remaining:
		return _json_response({"error": "该评分任务没有剩余岗位"}, 400)
	config = load_config(CONFIG_PATH)
	messages = _preflight_messages("rescore", config)
	if messages:
		return _json_response({"error": "请先处理评分启动检查", "messages": messages}, 400)
	force_rescore = bool(run.get("options", {}).get("force_rescore"))
	db = _get_web_db()
	try:
		eligible = select_scoring_jobs(
			db,
			scope="selected",
			limit=None,
			job_ids=remaining,
			force_rescore=force_rescore,
		)
	finally:
		db.close()
	remaining = [str(job["id"]) for job in eligible]
	if not remaining:
		completed = update_scoring_run(db_path, run_id, status="completed", remaining_job_ids=[])
		return _json_response(completed)
	try:
		with job_mutation_lock:
			update_scoring_run(db_path, run_id, status="running", remaining_job_ids=remaining)
			task = task_runner.start("score", _task_config({
				"_score_run_id": run_id,
				"_score_options": {"job_ids": remaining, "force_rescore": force_rescore},
			}))
		update_scoring_run(db_path, run_id, task_id=str(task["id"]))
		return _json_response({"run": get_scoring_run(db_path, run_id), "task": task})
	except TaskAlreadyRunningError as exc:
		update_scoring_run(db_path, run_id, status="paused", pause_reason=str(exc))
		return _json_response({"error": str(exc)}, 409)


@app.route("/api/scoring/runs/<run_id>/end", method="POST")
def api_scoring_end(run_id):
	db_path = DATA_DIR / "jobwinner.db"
	run = get_scoring_run(db_path, run_id)
	if not run:
		return _json_response({"error": "评分任务不存在"}, 404)
	if run.get("status") == "running" and run.get("task_id"):
		try:
			task_runner.stop(str(run["task_id"]), "用户结束独立评分")
		except KeyError:
			pass
	ended = update_scoring_run(db_path, run_id, status="stopped", remaining_job_ids=[])
	return _json_response(ended)


@app.route("/api/workbench/task", method="POST")
def api_workbench_task_start():
	try:
		body = request.json or {}
		mode = body.get("mode", "")
		messages = _preflight_messages(mode, load_config(CONFIG_PATH))
		if messages:
			return _json_response({"error": "请先处理启动前检查", "messages": messages}, 400)
		config = _task_config()
		if mode == "deliver":
			from jobwinner.db import get_jobs_ready_to_send
			db = _get_web_db()
			try:
				ready_jobs = get_jobs_ready_to_send(db)
			finally:
				db.close()
			ready_ids = [str(job["id"]) for job in ready_jobs]
			if not ready_ids:
				return _json_response({"error": "当前没有已生成招呼语的待发送岗位，请先完成评分与招呼语生成。"}, 400)
			config["_workbench_job_ids"] = ready_ids
		elif mode == "score":
			config["_pool_loop"] = True
		with job_mutation_lock:
			task = task_runner.start(mode, config)
		return _json_response(task)
	except TaskAlreadyRunningError as e:
		return _json_response({"error": str(e)}, 409)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/workbench/task/<task_id>/stop", method="POST")
def api_workbench_task_stop(task_id):
	try:
		return _json_response(task_runner.stop(task_id))
	except KeyError:
		return _json_response({"error": "任务不存在"}, 404)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/workbench/deliver", method="POST")
def api_workbench_deliver():
	try:
		body = request.json or {}
		job_ids = [str(job_id) for job_id in body.get("job_ids", []) if str(job_id)]
		if not job_ids:
			return _json_response({"error": "请选择要投递的岗位"}, 400)
		validation_db = _get_web_db()
		try:
			placeholders = ",".join("?" for _ in job_ids)
			active_ids = {
				str(row["id"])
				for row in validation_db.execute(
					f"SELECT id FROM jobs WHERE deleted_at IS NULL AND id IN ({placeholders})",
					job_ids,
				).fetchall()
			}
		finally:
			validation_db.close()
		invalid_ids = [job_id for job_id in job_ids if job_id not in active_ids]
		if invalid_ids:
			return _json_response({"error": "所选岗位不存在或已进入回收站", "invalid_ids": invalid_ids}, 409)

		direct_send = bool(body.get("direct_send"))
		status = task_runner.status()
		active_task = status.get("active") or {}
		active_runtime_task = task_runner._tasks.get(active_task.get("id"))
		monitoring_task = None
		if (
			active_runtime_task
			and active_runtime_task.status == "running"
			and active_runtime_task.context.get("monitoring")
		):
			monitoring_task = active_runtime_task
		delivery_task = None
		if (
			active_runtime_task
			and active_runtime_task.status == "running"
			and active_runtime_task.context.get("delivering")
		):
			delivery_task = active_runtime_task
		waiting_task = None
		if (
			not direct_send
			and active_runtime_task
			and active_runtime_task.mode == "full"
			and active_runtime_task.status == "running"
			and not monitoring_task
			and not delivery_task
			and not active_runtime_task.context.get("confirmation_complete")
		):
			waiting_task = active_runtime_task
		if active_task and not waiting_task and not monitoring_task and not delivery_task:
			# deliver conflicts only with another deliver or with a running full
			# pipeline (which already drives sending); all other tasks
			# (collect/score/monitor) run concurrently with delivery.
			from jobwinner.web.tasks import MODE_GROUPS
			active_mode = active_task.get("mode", "")
			active_group = MODE_GROUPS.get(active_mode, active_mode)
			if active_group == "deliver" or active_mode == "full":
				raise TaskAlreadyRunningError(
					f"当前已有后台任务「{active_task.get('label', '未知任务')}」正在运行或停止中，请等待其完全结束"
				)

		queued_payload = None
		status_job_ids = job_ids
		if delivery_task:
			queued_payload, status_job_ids = _queue_active_delivery(
				delivery_task,
				job_ids,
				direct_send=direct_send,
			)

		db = _get_web_db()
		try:
			for job_id in status_job_ids:
				update_job_status(db, job_id, "approved")
				if not direct_send:
					add_history(db, job_id, "approved", "Web Dashboard 确认投递")
		finally:
			db.close()

		if queued_payload is not None:
			return _json_response(queued_payload)

		if waiting_task:
			waiting_task.context["confirmed_job_ids"] = job_ids
			waiting_task.context["delivery_requested"] = True
			confirmation_event = waiting_task.context.get("confirmation_event")
			if isinstance(confirmation_event, Event):
				confirmation_event.set()
			return _json_response(waiting_task.snapshot())

		if monitoring_task:
			return _json_response(
				_queue_monitor_delivery(
					monitoring_task,
					job_ids,
					direct_send=direct_send,
				)
			)

		deliver_options = {"_workbench_job_ids": job_ids}
		if direct_send:
			deliver_options["_workbench_skip_greeting"] = True
		task = task_runner.start("deliver", _task_config(deliver_options))
		return _json_response(task)
	except TaskAlreadyRunningError as e:
		return _json_response({"error": str(e)}, 409)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/workbench/reject", method="POST")
def api_workbench_reject():
	try:
		body = request.json or {}
		job_ids = [str(job_id) for job_id in body.get("job_ids", []) if str(job_id)]
		if not job_ids:
			return _json_response({"error": "请选择要放弃的岗位"}, 400)

		db = _get_web_db()
		try:
			placeholders = ",".join("?" for _ in job_ids)
			active_ids = {
				str(row["id"])
				for row in db.execute(
					f"SELECT id FROM jobs WHERE deleted_at IS NULL AND id IN ({placeholders})",
					job_ids,
				).fetchall()
			}
			invalid_ids = [job_id for job_id in job_ids if job_id not in active_ids]
			if invalid_ids:
				return _json_response({"error": "所选岗位不存在或已进入回收站", "invalid_ids": invalid_ids}, 409)
			for job_id in job_ids:
				update_job_status(db, job_id, "rejected")
				add_history(db, job_id, "rejected", "Web Dashboard 放弃投递")
		finally:
			db.close()

		return _json_response({"success": True, "count": len(job_ids)})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/jobs/<job_id>")
def api_job_detail(job_id):
	db = _get_web_db()
	try:
		row = db.execute("SELECT * FROM jobs WHERE id = ? AND deleted_at IS NULL", (job_id,)).fetchone()
		if not row:
			return _json_response({"error": "岗位不存在"}, 404)
		return _json_response(dict(row))
	finally:
		db.close()


@app.route("/api/jobs/<job_id>/greeting", method="PATCH")
def api_job_update_greeting(job_id):
	"""Update (edit) the greeting message for a job before sending."""
	try:
		body = request.json or {}
		greeting = body.get("greeting")
		if greeting is None:
			return _json_response({"error": "缺少 greeting 字段"}, 400)
		greeting = str(greeting).strip()
		if not greeting:
			return _json_response({"error": "招呼语不能为空"}, 400)
		if len(greeting) > 2000:
			return _json_response({"error": "招呼语过长（最多 2000 字）"}, 400)
		db = _get_web_db()
		try:
			row = db.execute(
				"SELECT id FROM jobs WHERE id = ? AND deleted_at IS NULL", (job_id,)
			).fetchone()
			if not row:
				return _json_response({"error": "岗位不存在或已进入回收站"}, 404)
			update_job_greeting(db, job_id, greeting)
			add_history(db, job_id, "greeting_edited", "Web Dashboard 手动编辑招呼语")
		finally:
			db.close()
		return _json_response({"success": True, "greeting": greeting})
	except Exception as exc:
		return _json_response({"error": str(exc)}, 500)


@app.route("/api/jobs/<job_id>/mark-resume-sent", method="POST")
@app.route("/api/greeting/polish", method="POST")
def api_greeting_polish():
	"""Polish a user-written greeting via AI without saving it."""
	try:
		body = request.json or {}
		greeting = str(body.get("greeting", "")).strip()
		if not greeting:
			return _json_response({"error": "缺少招呼语内容"}, 400)
		from jobwinner.ai.greeter import polish_greeting
		config = load_config(CONFIG_PATH)
		polished = polish_greeting(greeting, config)
		if not polished:
			return _json_response({"error": "AI 润色失败，请稍后重试"}, 502)
		return _json_response({"polished": polished})
	except Exception as exc:
		return _json_response({"error": str(exc)}, 500)


def api_job_mark_resume_sent(job_id):
	db = _get_web_db()
	try:
		row = db.execute("SELECT 1 FROM jobs WHERE id = ? AND deleted_at IS NULL", (job_id,)).fetchone()
		if not row:
			return _json_response({"error": "岗位不存在或已进入回收站"}, 404)
		update_job_status(db, job_id, "resume_sent")
		add_history(db, job_id, "resume_sent", "Web Dashboard 标记定制简历已发送")
		return _json_response({"success": True})
	finally:
		db.close()


@app.route("/api/jobs/<job_id>/confirm-send-resume", method="POST")
def api_job_confirm_send_resume(job_id):
	"""User-confirmed sending of a tailored PDF resume to the HR chat.

	Opens the BOSS conversation for this job and sends the platform resume
	(delivers the tailored PDF path for manual drag-in if attachment upload
	is unavailable). Marks the job as resume_sent on success.
	"""
	try:
		from jobwinner.executor.monitor import _deliver_resume_to_chat, _open_conversation

		config = _task_config()
		db = _get_web_db()
		try:
			row = db.execute("SELECT * FROM jobs WHERE id = ? AND deleted_at IS NULL", (job_id,)).fetchone()
			if not row:
				return _json_response({"error": "岗位不存在或已进入回收站"}, 404)
			job = dict(row)
		finally:
			db.close()

		if job.get("status") not in {"resume_pending", "needs_resume"}:
			return _json_response({"error": f"该岗位当前状态为 {job.get('status')}，不可确认发送"}, 409)

		# Open the conversation with HR
		target_id = _open_conversation(job, config)
		if not target_id:
			return _json_response({"error": "无法打开与 HR 的对话窗口，请确认已登录 BOSS 直聘"}, 502)

		# Deliver platform resume via BOSS's built-in resume button
		sent = _deliver_resume_to_chat(target_id)
		if not sent:
			return _json_response({"error": "未能自动发送简历（BOSS 界面结构可能变化），可手动打开对话发送"}, 502)

		db = _get_web_db()
		try:
			update_job_status(db, job_id, "resume_sent")
			add_history(db, job_id, "resume_sent", "Web Dashboard 确认发送定制简历")
		finally:
			db.close()
		return _json_response({"success": True, "resume_path": job.get("resume_path", "")})
	except Exception as exc:
		return _json_response({"error": str(exc)[:500]}, 500)


@app.route("/api/jobs/<job_id>/resume/download")
def api_job_resume_download(job_id):
	db = _get_web_db()
	try:
		row = db.execute("SELECT resume_path FROM jobs WHERE id = ? AND deleted_at IS NULL", (job_id,)).fetchone()
		if not row or not row["resume_path"]:
			return _json_response({"error": "定制简历不存在"}, 404)
		resume_path = Path(row["resume_path"])
		if not resume_path.exists():
			return _json_response({"error": "定制简历文件不存在"}, 404)
		return static_file(resume_path.name, root=str(resume_path.parent), download=resume_path.name)
	finally:
		db.close()


@app.route("/api/history/<history_id>/reply", method="POST")
def api_history_reply(history_id):
	db = _get_web_db()
	try:
		body = request.json or {}
		message = str(body.get("message", "")).strip()
		if not message:
			return _json_response({"error": "回复内容不能为空"}, 400)

		row = db.execute(
			"SELECT id, job_id, action, detail FROM history WHERE id = ?",
			(history_id,),
		).fetchone()
		if not row:
			return _json_response({"error": "待回复记录不存在"}, 404)
		if row["action"] != "reply_pending":
			return _json_response({"error": "只能确认待回复记录"}, 400)

		from jobwinner.executor.monitor import _build_reply_resolution_detail

		add_history(
			db,
			row["job_id"],
			"replied",
			_build_reply_resolution_detail(
				"replied.v1",
				"Web Dashboard 确认回复",
				row["detail"],
				message,
				int(row["id"]),
			),
		)
		update_job_status(db, row["job_id"], "replied")
		return _json_response({"success": True, "message": "回复已记录，请在招聘平台手动发送。"})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)
	finally:
		db.close()


@app.route("/api/history/<history_id>/dismiss", method="POST")
def api_history_dismiss(history_id):
	db = _get_web_db()
	try:
		row = db.execute(
			"SELECT id, job_id, action, detail FROM history WHERE id = ?",
			(history_id,),
		).fetchone()
		if not row:
			return _json_response({"error": "待回复记录不存在"}, 404)
		if row["action"] != "reply_pending":
			return _json_response({"error": "只能放弃待回复记录"}, 400)

		from jobwinner.executor.monitor import _build_reply_resolution_detail

		add_history(
			db,
			row["job_id"],
			"reply_dismissed",
			_build_reply_resolution_detail(
				"reply_dismissed.v1",
				"Web Dashboard 放弃回复建议",
				row["detail"],
				pending_history_id=int(row["id"]),
			),
		)
		return _json_response({"success": True})
	finally:
		db.close()


# ─── Config APIs ─────────────────────────────────────────

@app.route("/api/config")
def api_config_get():
	try:
		config = _redact_config_for_response(load_config(CONFIG_PATH))
		return _json_response(config)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/config", method="POST")
def api_config_post():
	try:
		import yaml
		data = request.json
		if not data:
			return _json_response({"error": "Empty body"}, 400)
		if not isinstance(data, dict):
			return _json_response({"error": "Config body must be an object"}, 400)
		data = _sanitize_config_for_write(data)

		# Basic validation
		profile = data.get("profile", {})
		if profile.get("salary_min", 0) > profile.get("salary_max", 0) and profile.get("salary_max", 0) > 0:
			return _json_response({"error": "salary_min must be <= salary_max"}, 400)

		# Write YAML (backend exclusively owns YAML serialization)
		_write_config(data)

		return _json_response({"success": True, "message": "配置已保存"})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/config/schema")
def api_config_schema():
	try:
		with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
			schema = json.load(f)
		return _json_response(schema)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/config/download")
def api_config_download():
	if CONFIG_PATH.exists():
		response.content_type = "application/x-yaml; charset=utf-8"
		response.headers["Content-Disposition"] = "attachment; filename=config.yaml"
		return _config_download_payload(load_config(CONFIG_PATH))
	abort(404, "config.yaml not found")


@app.route("/api/config/cities")
def api_cities():
	return _json_response(get_city_map(BASE_DIR))


@app.route("/api/config/cities/lookup", method="POST")
def api_city_lookup():
	try:
		body = request.json or {}
		city = str(body.get("city") or "").strip()
		city_code = get_city_map(BASE_DIR).get(city)
		if city_code:
			return _json_response({"name": city, "code": city_code})
		return _json_response(lookup_city(city))
	except CityLookupError as exc:
		return _json_response({"error": str(exc)}, 400)


@app.route("/api/cities")
def api_city_snapshot():
	try:
		snapshot = load_city_snapshot(BASE_DIR)
		return _json_response({
			"ok": True,
			"source": snapshot.get("source", "bundled"),
			"count": len(snapshot.get("cities", [])),
			"updated_at": snapshot.get("fetched_at"),
			"cities": snapshot.get("cities", []),
		})
	except Exception:
		return _json_response({
			"ok": False,
			"source": "bundled",
			"count": 0,
			"cities": [],
			"error": "本地城市列表不可用",
		}, 500)


@app.route("/api/cities/refresh", method="POST")
def api_city_refresh():
	try:
		snapshot = refresh_city_cache(DATA_DIR / "cities.cache.json")
		return _json_response({
			"ok": True,
			"source": "cache",
			"count": len(snapshot.get("cities", [])),
			"updated_at": snapshot.get("fetched_at"),
			"cities": snapshot.get("cities", []),
		})
	except CityRefreshError as exc:
		return _json_response({
			"ok": False,
			"source": "local",
			"using_local_data": True,
			"error": str(exc),
		}, 502)


@app.route("/api/jobs/export", method="POST")
def api_jobs_export():
	db = _get_web_db()
	try:
		body = request.json or {}
		if not isinstance(body, dict):
			return _json_response({"error": "请求体必须是对象"}, 400)
		format_value = body.get("format", "xlsx")
		scope = body.get("scope", "all")
		job_ids = body.get("job_ids", [])
		filters = body.get("filters", {})
		if not isinstance(job_ids, list):
			return _json_response({"error": "岗位 ID 必须是数组"}, 400)
		if not isinstance(filters, dict):
			return _json_response({"error": "筛选条件必须是对象"}, 400)
		content, content_type, filename = export_jobs(
			db,
			format=format_value,
			scope=scope,
			job_ids=job_ids,
			filters=filters,
		)
		exported_count = export_row_count(db, scope=scope, job_ids=job_ids, filters=filters)
		response.content_type = content_type
		response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
		response.headers["Content-Length"] = str(len(content))
		response.headers["X-Exported-Count"] = str(exported_count)
		return content
	except InvalidJobSelectionError as exc:
		return _json_response({
			"error": str(exc),
			"code": "invalid_job_ids",
			"invalid_ids": exc.invalid_ids,
		}, 400)
	except ValueError as exc:
		return _json_response({"error": str(exc)}, 400)
	except Exception:
		return _json_response({"error": "岗位导出失败"}, 500)
	finally:
		db.close()


def _job_action_payload():
	body = request.json or {}
	if not isinstance(body, dict):
		raise ValueError("请求体必须是对象")
	job_ids = body.get("job_ids")
	if not isinstance(job_ids, list):
		raise ValueError("岗位 ID 必须是数组")
	return body, job_ids


def _job_action_error(exc: ValueError):
	payload = {"error": str(exc), "code": getattr(exc, "code", "invalid_request")}
	if isinstance(exc, JobDeletionConflictError):
		payload["blocked"] = exc.blocked
		payload["not_found"] = exc.not_found
	return _json_response(payload, 409 if isinstance(exc, JobDeletionConflictError) else 400)


def _active_task_mutation_error():
	active = task_runner.status().get("active")
	if not active:
		return None
	return _json_response({
		"error": f"当前后台任务「{active.get('label', '未知任务')}」仍在运行，请停止或等待结束后再操作回收站",
		"code": "active_task_conflict",
		"task_id": active.get("id"),
	}, 409)


@app.route("/api/jobs/soft-delete", method="POST")
def api_jobs_soft_delete():
	db = _get_web_db()
	try:
		body, job_ids = _job_action_payload()
		with job_mutation_lock:
			conflict = _active_task_mutation_error()
			if conflict is not None:
				return conflict
			result = soft_delete_jobs(
				db,
				job_ids,
				confirmed=body.get("confirmed") is True,
				reason=str(body.get("reason") or "用户移入回收站"),
			)
		return _json_response(result)
	except (ValueError, JobDeletionConflictError) as exc:
		return _job_action_error(exc)
	finally:
		db.close()


@app.route("/api/jobs/restore", method="POST")
def api_jobs_restore():
	db = _get_web_db()
	try:
		body, job_ids = _job_action_payload()
		with job_mutation_lock:
			conflict = _active_task_mutation_error()
			if conflict is not None:
				return conflict
			result = restore_jobs(db, job_ids, confirmed=body.get("confirmed") is True)
		return _json_response(result)
	except (ValueError, JobDeletionConflictError) as exc:
		return _job_action_error(exc)
	finally:
		db.close()


@app.route("/api/jobs/permanent-delete", method="POST")
def api_jobs_permanent_delete():
	db = _get_web_db()
	try:
		body, job_ids = _job_action_payload()
		with job_mutation_lock:
			conflict = _active_task_mutation_error()
			if conflict is not None:
				return conflict
			result = permanent_delete_jobs(
				db,
				job_ids,
				confirmed=body.get("confirmed") is True,
				confirmation=body.get("confirmation", ""),
			)
		return _json_response(result)
	except (ValueError, JobDeletionConflictError) as exc:
		return _job_action_error(exc)
	finally:
		db.close()


# ─── Resume APIs ─────────────────────────────────────────

@app.route("/api/resume")
def api_resume_get():
	try:
		config = load_config(CONFIG_PATH)
		from jobwinner.ai.resume_picker import resume_paths
		items = []
		for resume_path in resume_paths(config):
			if not resume_path.exists():
				continue
			p = Path(str(resume_path))
			stat = p.stat()
			items.append({
				"filename": p.name,
				"size": stat.st_size,
				"uploaded_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
				"path": str(p),
			})
		return _json_response(items)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/resume/upload", method="POST")
def api_resume_upload():
	try:
		import yaml
		upload = request.files.get("file")
		if not upload:
			return _json_response({"error": "No file uploaded"}, 400)

		# Validate size (10MB max)
		content = upload.file.read()
		if len(content) > 10 * 1024 * 1024:
			return _json_response({"error": "文件大小超过 10MB 限制"}, 400)

		# Bottle's normalized `filename` strips non-ASCII characters. Use the
		# raw browser filename and apply our own Unicode-safe sanitization.
		raw_name = upload.raw_filename or upload.filename
		safe_name, stored_content = prepare_resume_content(raw_name, content)
		RESUME_DIR.mkdir(parents=True, exist_ok=True)
		dest = RESUME_DIR / safe_name
		dest.write_bytes(stored_content)

		# Update config: append to the multi-resume list and keep the legacy
		# resume_path pointing at the first entry for backward compatibility.
		config = load_config(CONFIG_PATH)
		profile = config.setdefault("profile", {})
		existing_paths = profile.get("resume_paths")
		if not isinstance(existing_paths, list):
			existing_paths = []
		legacy_path = profile.get("resume_path")
		# Keep the legacy path only when it actually exists; a phantom default
		# (./resume.md from DEFAULTS) should not occupy the primary slot.
		if legacy_path and str(legacy_path).strip() and not existing_paths:
			legacy = Path(str(legacy_path))
			if legacy.exists():
				existing_paths.append(str(legacy_path))
		dest_str = _config_rel_path(dest)
		if dest_str not in existing_paths:
			existing_paths.append(dest_str)
		profile["resume_paths"] = existing_paths
		profile["resume_path"] = existing_paths[0] if existing_paths else ""
		_write_config(config)

		from jobwinner.ai.resume_picker import resume_paths as _resolve_resume_paths
		return _json_response({
			"success": True,
			"filename": safe_name,
			"size": len(stored_content),
			"path": str(dest),
			"resume_paths": [str(p) for p in _resolve_resume_paths(config)],
		})
	except ResumeUploadError as e:
		return _json_response({"error": str(e)}, 400)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/resume", method="DELETE")
def api_resume_delete():
	try:
		import yaml
		config = load_config(CONFIG_PATH)
		profile = config.setdefault("profile", {})
		body = request.json or {}
		target = str(body.get("path") or "").strip() if isinstance(body, dict) else ""

		if target:
			# Remove the specific resume from the multi-resume list.
			existing_paths = profile.get("resume_paths")
			if isinstance(existing_paths, list):
				profile["resume_paths"] = [p for p in existing_paths if str(p) != target]
			if str(profile.get("resume_path") or "") == target:
				profile["resume_path"] = ""
		else:
			# Legacy behavior: detach all resumes from config.
			profile["resume_path"] = ""
			profile["resume_paths"] = []

		# Keep resume_path pointing at the first remaining resume (backward compat).
		remaining = [p for p in (profile.get("resume_paths") or []) if str(p).strip()]
		if not profile.get("resume_path") and remaining:
			profile["resume_path"] = remaining[0]
		_write_config(config)

		return _json_response({"success": True})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


# ─── Static Files + SPA Fallback ─────────────────────────

_STATIC_MIME_TYPES = {
	".css": "text/css; charset=utf-8",
	".cjs": "text/javascript; charset=utf-8",
	".html": "text/html; charset=utf-8",
	".js": "application/javascript; charset=utf-8",
	".json": "application/json; charset=utf-8",
	".mjs": "application/javascript; charset=utf-8",
	".svg": "image/svg+xml",
}


def _serve_static(filename: str, root: Path):
	"""Serve static assets with stable MIME types while retaining range/cache support."""
	mimetype = _STATIC_MIME_TYPES.get(Path(filename).suffix.lower(), "auto")
	return static_file(filename, root=str(root), mimetype=mimetype)


@app.route("/assets/<filepath:path>")
def serve_assets(filepath):
	return _serve_static(filepath, FRONTEND_DIR / "assets")


@app.route("/")
@app.route("/<filepath:path>")
def serve_spa(filepath="index.html"):
	if str(filepath).startswith("api/"):
		return _json_response({"error": "Not found"}, 404)

	# Try serving the exact file first
	file_path = FRONTEND_DIR / filepath
	if file_path.is_file():
		return _serve_static(filepath, FRONTEND_DIR)
	# SPA fallback: return index.html for all non-API routes
	return _serve_static("index.html", FRONTEND_DIR)


# ─── Error Handlers ──────────────────────────────────────

@app.error(404)
def error404(error):
	if request.path.startswith("/api/"):
		response.content_type = "application/json; charset=utf-8"
		return json.dumps({"error": "Not found"}, ensure_ascii=False)
	# SPA fallback for non-API 404s
	return _serve_static("index.html", FRONTEND_DIR)


@app.error(500)
def error500(error):
	response.content_type = "application/json; charset=utf-8"
	return json.dumps({"error": str(error.body)}, ensure_ascii=False)


# ─── Run ─────────────────────────────────────────────────

@app.route("/api/shutdown", method="POST")
def api_shutdown():
	"""完全退出服务：停 CDP 代理 + 停 web 进程（网页点『退出服务』触发）。

	等效于关闭终端：停止后台任务后进程退出，cmd 启动窗口随之关闭。
	"""
	try:
		# 1) 尽量停止所有工作台任务（采集/评分/监测/发送）
		try:
			from jobwinner.web.tasks import task_runner
			status = task_runner.status()
			active = status.get("active_tasks") or []
			for task_id in active:
				try:
					task_runner.stop(str(task_id), reason="用户请求退出服务")
				except Exception:
					pass
		except Exception:
			pass
		# 2) 停 CDP 浏览器代理（node 3456）
		_stop_cdp_proxy()
		# 3) 停 web 服务（进程退出，终端窗口随之关闭）
		print("[jobwinner] 收到退出请求，服务已停止。窗口可关闭。")
		import threading as _th
		_th.Timer(0.8, lambda: os._exit(0)).start()
		return _json_response({"ok": True, "message": "服务已停止"})
	except Exception as exc:
		return _json_response({"error": f"退出失败: {exc}"}, 500)


def _stop_cdp_proxy() -> None:
	"""Kill the CDP browser-runtime proxy (node process on proxy_port 3456)."""
	try:
		from jobwinner.config import load_config as _lc
		cfg = _lc(CONFIG_PATH)
		proxy_port = int(cfg.get("browser", {}).get("proxy_port", 3456))
	except Exception:
		proxy_port = 3456
	try:
		import socket
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		sock.settimeout(1)
		if sock.connect_ex(("127.0.0.1", proxy_port)) == 0:
			import subprocess
			subprocess.run(
				["powershell", "-NoProfile", "-Command",
				 f"$c = Get-NetTCPConnection -LocalPort {proxy_port} -State Listen -ErrorAction SilentlyContinue; if ($c) {{ Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }}"],
				creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
			)
		sock.close()
	except Exception:
		pass


def run_server(host: str = "127.0.0.1", port: int = 8686, open_browser: bool = True):
	"""Start the web server."""
	if open_browser:
		import webbrowser
		import threading
		def _open():
			time.sleep(1)
			webbrowser.open(f"http://{host}:{port}")
		threading.Thread(target=_open, daemon=True).start()

	# 官网投递进度巡检：面板启动后跑一轮（用户"启动这个网站"即触发），后台异步不阻塞
	import threading as _threading
	from jobwinner.executor.portal_tracker import check_portal_progress

	def _initial_portal_check() -> None:
		try:
			time.sleep(2)
			check_portal_progress()
		except Exception:
			pass

	_threading.Thread(target=_initial_portal_check, daemon=True).start()

	# 空闲自动退出：关掉网页后，连续 IDLE_TIMEOUT 秒无请求 → 停服（含 CDP 代理）。
	# 保护：存在活跃任务（采集/评分/监测/发送）时永不自动退出，任务跑完才恢复空闲计时。
	def _idle_shutdown_watcher() -> None:
		global _HAS_FRONTEND_ACCESS, _LAST_ACTIVITY
		while True:
			try:
				time.sleep(10)
				if not _HAS_FRONTEND_ACCESS:
					continue  # 从未被浏览器访问过，不自动退出
				try:
					has_active = bool(task_runner.status().get("active_tasks"))
				except Exception:
					has_active = False
				if has_active:
					# 有任务在跑：视为持续心跳，任务跑完后再开始空闲计时
					_LAST_ACTIVITY = time.monotonic()
					continue
				if time.monotonic() - _LAST_ACTIVITY > IDLE_TIMEOUT:
					print("[jobwinner] 网页已关闭且无活跃任务，空闲超过阈值，自动停止服务...")
					_stop_cdp_proxy()
					os._exit(0)
			except Exception:
				time.sleep(10)

	_threading.Thread(target=_idle_shutdown_watcher, daemon=True).start()

	app.run(
		host=host,
		port=port,
		quiet=False,
		reloader=False,
		server_class=ThreadingWSGIServer,
	)
