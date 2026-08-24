"""AI Greeter - Generate personalized greeting messages with self-review."""

import json
import re

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from bosshunter.ai.credentials import AIRequestError, call_anthropic_text
from bosshunter.cancellation import OperationCancelled, run_cancellable
from bosshunter.db import (
    add_history,
    get_db,
    get_jobs_by_status,
    get_jobs_needing_greeting,
    update_job_greeting,
    update_job_status,
)

console = Console()

GREETING_PROMPT = """你是一位求职者，需要在BOSS直聘上给HR发送打招呼消息。请根据以下信息生成一条个性化、自然的招呼语，像真人主动搭话一样.

## 我的背景
{resume_summary}

## 基本事实（可放心提及，不是捏造）
- 双985高校在读/毕业
- 对{title}这个方向有浓厚兴趣和热情，一直在学习和实践

## 目标岗位
- 职位：{title}
- 公司：{company}
- 薪资：{salary}
- 岗位要求摘要：{jd_summary}
- 匹配分析：{match_reason}

## 额外亮点（适时融入，不要生硬罗列）
{extra_highlights}

## 要求
1. 字数控制在50-150字
2. 风格自然，像真人发的IM消息，口语化、有温度，不要太正式、不要像求职模板
3. 突出1-2个最匹配的优势
4. 表达对岗位的兴趣和想进一步了解的意愿（例如“对这个方向挺感兴趣”“想多了解一下咱们这边”），但不要谄媚
5. 不要用“您好，我是xxx”这种模板开头，要有差异化、像真人搭话
6. 适配手机端阅读
7. 不要在一条打招呼里塞太多信息；可以自然提到学历背景和热爱，但保持轻快
8. 【严禁】不得捏造我没有的经历、头衔或身份；只能用“我的背景”和“基本事实”中明确提到的内容
9. 【严禁】不得把岗位JD中的描述（如公司头衔、项目名）当作我的经历来写
10. 学历背景（双985）和兴趣是允许提及的，但不要每句都在强调学历，自然带出即可
11. 对岗位感兴趣时，用“想进一步了解/想聊聊”这类自然口吻，表达诚意但不是套话
{critique_section}
请直接输出招呼语文本，不要加任何标记或解释。
"""

REVIEW_PROMPT = """请评估以下BOSS直聘招呼语的质量。

## 岗位
{title} @ {company}

## 招呼语
{greeting}

## 评估维度（每项1-10分）
1. 自然度：是否像真人发的IM消息，而非模板
2. 相关性：是否针对该岗位突出匹配点
3. 差异化：是否能从众多招呼中脱颖而出

请严格按JSON格式输出，不要输出其他内容：
{{"naturalness": 8, "relevance": 7, "differentiation": 6, "avg": 7.0, "critique": "改进建议（20字内）"}}
"""





POLISH_PROMPT = """你是一位求职沟通专家。请对下面的招呼语进行润色。保持原意、风格和事实完全不变（不得新增或删减关键信息，不得捏造经历），只让表达更自然、口语化、有温度，像真人主动搭话，避免生硬套话。

原招呼语：
{greeting}

要求：
1. 字数控制在50-150字
2. 不要捏造任何经历或身份
3. 语气自然、口语化、有温度，保留本人原有的表达重点
4. 不要用\"您好，我是xxx\"之类的模板开头
5. 只输出润色后的招呼语文本，不要加任何标记或解释
"""


def polish_greeting(greeting: str, config: dict) -> str | None:
    """Polish a user-written greeting via AI with the shared provider."""
    prompt = POLISH_PROMPT.format(greeting=greeting)
    response = _call_claude(prompt, config, purpose="greeting")
    return _normalize_greeting_response(response)


def _get_resume_summary(config: dict, job: dict | None = None) -> str:
    """Get a brief resume summary for greeting generation.

    Picks the best-matching resume for the job when one is given, otherwise
    falls back to the default (first existing) resume.
    """
    from bosshunter.ai.resume_picker import resume_paths, select_resume_path_for_job
    resume_path = select_resume_path_for_job(job, config) if job else None
    if resume_path is None:
        for p in resume_paths(config):
            if p.exists():
                resume_path = p
                break
    if resume_path is None or not resume_path.exists():
        return ""
    content = resume_path.read_text(encoding="utf-8")
    return content[:1500]


def _call_claude(
    prompt: str,
    config: dict,
    max_tokens: int | None = None,
    *,
    purpose: str = "greeting",
) -> str | None:
    """Call Claude API and return response text."""
    ai_cfg = config.get("ai", {}) if isinstance(config.get("ai"), dict) else {}
    default_key = "greeting_review_max_tokens" if purpose == "greeting_review" else "greeting_max_tokens"
    default_tokens = 4096 if purpose == "greeting_review" else 8192
    token_limit = max_tokens if max_tokens is not None else ai_cfg.get(default_key, default_tokens)
    try:
        token_limit = max(128, min(int(token_limit or default_tokens), 65536))
    except (TypeError, ValueError):
        token_limit = default_tokens
    return run_cancellable(
        lambda: call_anthropic_text(
            prompt,
            config,
            token_limit,
            timeout=ai_cfg.get(
                f"{purpose}_timeout_seconds",
                ai_cfg.get("greeting_timeout_seconds", ai_cfg.get("timeout_seconds", 180)),
            ),
            purpose=purpose,
        ),
        config,
    )


def _truncate_prompt_text(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    marker = "\n...[为适配模型上下文已裁剪]...\n"
    available = max(limit - len(marker), 2)
    head = max(int(available * 0.7), 1)
    return f"{text[:head]}{marker}{text[-(available - head):]}"


def _notify(config: dict, message: str, *, error: bool = False) -> None:
    console.print(f"[{'red' if error else 'yellow'}]{message}[/{'red' if error else 'yellow'}]")
    callback = config.get("_workbench_log")
    if callable(callback):
        callback(message)


def _json_greeting_text(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            nested = _json_greeting_text(item)
            if nested:
                return nested
        return None
    if not isinstance(value, dict):
        return None
    for key in ("greeting", "message", "text", "content"):
        nested = _json_greeting_text(value.get(key))
        if nested:
            return nested
    for key in ("data", "result", "output"):
        nested = _json_greeting_text(value.get(key))
        if nested:
            return nested
    return None


def _normalize_greeting_response(response: str | None) -> str | None:
    """Accept common provider wrappers while rejecting non-answer payloads."""
    if not isinstance(response, str):
        return None
    greeting = response.strip()
    if not greeting:
        return None

    fenced = re.fullmatch(
        r"```(?:json|text|markdown|md)?\s*(.*?)\s*```",
        greeting,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        greeting = fenced.group(1).strip()

    parsed_greeting = None
    structured_response = greeting.startswith("{") or greeting.startswith("[")
    if structured_response:
        try:
            parsed = json.loads(greeting)
        except (json.JSONDecodeError, TypeError):
            return None
        parsed_greeting = _json_greeting_text(parsed)
    else:
        decoder = json.JSONDecoder()
        for index, char in enumerate(greeting):
            if char not in "[{":
                continue
            try:
                parsed, _ = decoder.raw_decode(greeting[index:])
            except (json.JSONDecodeError, TypeError):
                continue
            parsed_greeting = _json_greeting_text(parsed)
            if parsed_greeting:
                break
    if structured_response and parsed_greeting is None:
        return None
    if parsed_greeting is not None:
        greeting = parsed_greeting

    greeting = re.sub(
        r"^\s*(?:最终)?(?:打招呼语|招呼语|消息内容|回复)\s*[:：]\s*",
        "",
        greeting,
        count=1,
        flags=re.IGNORECASE,
    )
    greeting = greeting.strip().strip('"\'“”‘’').strip()
    if not greeting:
        return None
    if re.fullmatch(r"(?is)(?:抱歉|无法|不能).{0,80}", greeting):
        return None

    if len(greeting) > 150:
        cut = greeting[:150]
        for sep in ("。", "！", "？", "～", "\n"):
            idx = cut.rfind(sep)
            if idx > 50:
                greeting = cut[:idx + 1]
                break
        else:
            greeting = cut
    return greeting.strip() or None


def _parse_review_response(response: str | None) -> dict | None:
    if not isinstance(response, str) or not response.strip():
        return None
    decoder = json.JSONDecoder()
    for index, char in enumerate(response):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(response[index:])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        try:
            avg = float(parsed.get("avg"))
        except (TypeError, ValueError):
            continue
        if not 1 <= avg <= 10:
            continue
        parsed["avg"] = avg
        parsed["critique"] = str(parsed.get("critique") or "")
        return parsed
    return None


def _review_greeting(
    greeting: str,
    job: dict,
    config: dict,
    max_tokens: int | None = None,
) -> dict | None:
    """Self-evaluate a greeting. Returns scores dict or None on failure."""
    prompt = REVIEW_PROMPT.format(
        title=job["title"],
        company=job["company"],
        greeting=greeting,
    )
    response = _call_claude(prompt, config, max_tokens, purpose="greeting_review")
    return _parse_review_response(response)


def _generate_greeting_once(
    job: dict,
    resume_summary: str,
    config: dict,
    critique: str = "",
    *,
    compact: bool = False,
    max_tokens: int | None = None,
) -> str | None:
    """Generate a single greeting attempt."""
    jd_limit = 250 if compact else 500
    resume_limit = 800 if compact else 1500
    jd_summary = _truncate_prompt_text(job.get("jd", ""), jd_limit) or "无详细描述"
    critique_section = f"\n7. 上次生成的问题: {critique}，请避免此问题\n" if critique else ""

    # Build extra highlights from config (portfolio URL, personal strengths, etc.)
    profile_cfg = config.get("profile", {})
    highlights = profile_cfg.get("extra_highlights", [])
    portfolio_url = profile_cfg.get("portfolio_url", "")
    highlight_lines = [f"- {h}" for h in highlights]
    if portfolio_url:
        highlight_lines.append(f"- 个人作品集网址：{portfolio_url}")
    extra_highlights = "\n".join(highlight_lines) if highlight_lines else "（无额外亮点配置）"

    prompt = GREETING_PROMPT.format(
        resume_summary=_truncate_prompt_text(resume_summary, resume_limit),
        title=job["title"],
        company=job["company"],
        salary=job["salary"] or "面议",
        jd_summary=jd_summary,
        match_reason=_truncate_prompt_text(job.get("score_reason", ""), 240),
        critique_section=critique_section,
        extra_highlights=_truncate_prompt_text(extra_highlights, 500),
    )

    ai_cfg = config.get("ai", {}) if isinstance(config.get("ai"), dict) else {}
    token_limit = max_tokens if max_tokens is not None else ai_cfg.get("greeting_max_tokens", 8192)
    response = _call_claude(prompt, config, token_limit)
    return _normalize_greeting_response(response)


def _generate_with_token_retry(
    job: dict,
    resume_summary: str,
    config: dict,
    critique: str = "",
) -> str | None:
    """Retry only request-size/output-limit failures without changing batch size."""
    try:
        result = _generate_greeting_once(job, resume_summary, config, critique)
        if result:
            return result
        ai_cfg = config.get("ai", {}) if isinstance(config.get("ai"), dict) else {}
        try:
            max_attempts = max(1, min(int(ai_cfg.get("greeting_max_attempts", 2) or 2), 3))
        except (TypeError, ValueError):
            max_attempts = 2
        for attempt in range(2, max_attempts + 1):
            _notify(
                config,
                f"{job['company']}｜{job['title']} 未返回完整招呼语，正在重试（{attempt}/{max_attempts}）。",
            )
            result = _generate_greeting_once(job, resume_summary, config, critique)
            if result:
                return result
        return None
    except AIRequestError as exc:
        if exc.kind == "output_truncated":
            _notify(config, f"{job['company']}｜{job['title']} 的招呼语回答被截断，正在增大输出 Token 上限后重试。")
            compact = False
            ai_cfg = config.get("ai", {}) if isinstance(config.get("ai"), dict) else {}
            try:
                configured_tokens = int(ai_cfg.get("greeting_max_tokens", 8192) or 8192)
            except (TypeError, ValueError):
                configured_tokens = 8192
            retry_max_tokens = min(
                max(configured_tokens * 2, 600),
                65536,
            )
        elif exc.kind == "output_limit":
            _notify(config, f"{job['company']}｜{job['title']} 正在降低单次输出 Token 上限后重试招呼语。")
            compact = False
            retry_max_tokens = 160
        elif exc.kind == "context_limit":
            _notify(config, f"{job['company']}｜{job['title']} 内容较长，正在压缩后重试招呼语。")
            compact = True
            retry_max_tokens = 160
        else:
            raise

    try:
        return _generate_greeting_once(
            job,
            resume_summary,
            config,
            critique,
            compact=compact,
            max_tokens=retry_max_tokens,
        )
    except AIRequestError as retry_exc:
        if retry_exc.kind in {"output_truncated", "output_limit", "context_limit"}:
            _notify(
                config,
                f"已跳过 {job['company']}｜{job['title']}：调整单次 Token 请求后仍失败。",
            )
            return None
        raise


def _review_with_token_retry(greeting: str, job: dict, config: dict) -> dict | None:
    """Keep greeting review from interrupting the usable generated draft."""
    try:
        return _review_greeting(greeting, job, config)
    except AIRequestError as exc:
        if exc.kind == "output_truncated":
            _notify(config, f"{job['company']}｜{job['title']} 的质量检查回答被截断，正在增大输出 Token 上限后重试。")
            ai_cfg = config.get("ai", {}) if isinstance(config.get("ai"), dict) else {}
            try:
                configured_tokens = int(ai_cfg.get("greeting_review_max_tokens", 4096) or 4096)
            except (TypeError, ValueError):
                configured_tokens = 4096
            retry_max_tokens = min(
                max(configured_tokens * 2, 600),
                65536,
            )
        elif exc.kind == "output_limit":
            _notify(config, f"{job['company']}｜{job['title']} 正在降低单次输出 Token 上限后重试质量检查。")
            retry_max_tokens = 128
        elif exc.kind == "context_limit":
            return None
        else:
            raise

    try:
        return _review_greeting(greeting, job, config, retry_max_tokens)
    except AIRequestError as retry_exc:
        if retry_exc.kind in {"output_truncated", "output_limit", "context_limit"}:
            return None
        raise


def generate_greetings(config: dict, *, include_ready: bool = False) -> int:
    """Generate greetings for jobs that still lack one.

    Defaults to ``approved`` jobs (preserving the original CLI semantics). When
    ``include_ready`` is set, also picks up ``ready`` jobs whose greeting is
    empty — used by the scoring pipeline so that jobs passing the AI threshold
    get their greeting generated immediately.
    """
    db = get_db()
    jobs = (
        get_jobs_needing_greeting(db)
        if include_ready
        else get_jobs_by_status(db, "approved")
    )
    _workbench_job_ids = {str(job_id) for job_id in config.get("_workbench_job_ids", [])}
    if _workbench_job_ids:
        jobs = [job for job in jobs if str(job["id"]) in _workbench_job_ids]

    if not jobs:
        hint = "没有已确认的岗位可生成招呼语。请先运行 bosshunter confirm，或使用 bosshunter run 执行完整流程。"
        if include_ready:
            hint = "没有待生成招呼语的岗位（评分通过即自动生成）。"
        console.print(f"[yellow]{hint}[/yellow]")
        db.close()
        return 0

    resume_summary = _get_resume_summary(config)
    if not resume_summary:
        console.print("[red]无法读取简历[/red]")
        db.close()
        return 0

    ai_cfg = config.get("ai", {})
    review_threshold = ai_cfg.get("greeting_review_threshold", 7.0)
    try:
        max_iterations = max(0, int(ai_cfg.get("greeting_max_iterations", 2) or 0))
    except (TypeError, ValueError):
        max_iterations = 2

    count = 0
    failed = 0
    pause_reason = ""
    stop_event = config.get("_workbench_stop_event")
    cancelled = False

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"生成招呼语 (0/{len(jobs)})", total=len(jobs))

        for index, job in enumerate(jobs, start=1):
            if stop_event is not None and stop_event.is_set():
                break
            # Pick the resume that best matches this job for its greeting.
            resume_summary = _get_resume_summary(config, job)
            best_greeting = None
            pause_after_current = ""

            for iteration in range(max_iterations + 1):
                if stop_event is not None and stop_event.is_set():
                    break
                critique = ""
                if iteration > 0 and best_greeting:
                    try:
                        review = _review_with_token_retry(best_greeting, job, config)
                    except OperationCancelled:
                        cancelled = True
                        break
                    except AIRequestError as exc:
                        pause_after_current = exc.user_message
                        break
                    if review is None:
                        _notify(
                            config,
                            f"{job['company']}｜{job['title']} 的质量检查返回格式无法识别，已保留可用招呼语并继续。",
                        )
                        break
                    if review and review.get("avg", 10) >= review_threshold:
                        break
                    critique = review.get("critique", "") if review else ""

                try:
                    greeting = _generate_with_token_retry(job, resume_summary, config, critique)
                except OperationCancelled:
                    cancelled = True
                    break
                except AIRequestError as exc:
                    if best_greeting:
                        pause_after_current = exc.user_message
                    else:
                        pause_reason = exc.user_message
                    break

                if not greeting:
                    if not best_greeting:
                        failed += 1
                    break

                best_greeting = greeting
                if max_iterations == 0:
                    break

            if cancelled or (stop_event is not None and stop_event.is_set()):
                break
            if not best_greeting:
                if not pause_reason and not (stop_event is not None and stop_event.is_set()):
                    add_history(db, job["id"], "greeting_failed", "AI 未返回完整招呼语，岗位保留为待生成")
                progress.update(task, advance=1, description=f"生成招呼语 ({index}/{len(jobs)})")
                if pause_reason:
                    break
                continue

            update_job_greeting(db, job["id"], best_greeting)
            update_job_status(db, job["id"], "ready")
            count += 1
            progress.update(task, advance=1, description=f"生成招呼语 ({index}/{len(jobs)})")

            if pause_after_current:
                pause_reason = pause_after_current
                break

    db.close()
    if pause_reason:
        remaining = max(len(jobs) - count, 0)
        _notify(
            config,
            f"招呼语生成已安全暂停：{pause_reason}。已生成内容已保存，剩余 {remaining} 个岗位下次运行会继续处理。",
            error=True,
        )
    if failed:
        _notify(config, f"本轮有 {failed} 个岗位未生成招呼语并保留为待处理，可稍后重试。")
    return count
