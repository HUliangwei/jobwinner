"""Job scraping module - Extract jobs from the active channel's search results."""

import json
import random
import time
from typing import Callable
from jobwinner.browser_lock import BrowserPriority, platform_browser_lock

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from jobwinner.browser import (
    new_tab, close_tab, evaluate, scroll, wait_for_load
)
from jobwinner.channels import get_active_channel
from jobwinner.config import CITY_CODES
from jobwinner.cancellation import get_stop_event
from jobwinner.db import get_db, job_exists, insert_job
from jobwinner.job_filters import matching_blocked_company, matching_deal_breaker
from jobwinner.throttle import PageThrottle

console = Console()


def _resolve_city_code(city: str, config: dict, channel) -> str | None:
    """Resolve a city name to a platform code.

    Order: active channel's own city code map ``city_codes`` -> config ``search.city_codes``
    (legacy BOSS直聘 codes) -> the global legacy ``CITY_CODES`` table.
    Channel codes win so switching the active channel never lets stale BOSS
    codes silently map to wrong cities on other platforms.
    """
    custom_codes = config.get("search", {}).get("city_codes", {})
    code = channel.resolve_city_code(city, custom_codes=custom_codes)
    if code:
        return code
    return CITY_CODES.get(city)


def _wait_or_stop(stop_event, seconds: float) -> bool:
    if stop_event is not None:
        return stop_event.wait(seconds)
    time.sleep(seconds)
    return False


def scrape_jobs(
    config: dict,
    keywords: list[str],
    limit: int | None = None,
    *,
    collected_job_ids: list[str] | None = None,
    on_new_job: Callable | None = None,
) -> int:
    """Scrape jobs from the active channel and store in database.

    Supports multi-keyword × multi-city combinations with pagination.
    When limit is None, collection is bounded only by city × keyword × max_pages.
    Returns the number of new jobs added.

    on_new_job: optional callback(job_id, job_record) invoked immediately after
    a new job is inserted, enabling conveyor-belt (real-time streaming) scoring.
    """
    db = get_db()
    stop_event = get_stop_event(config)
    channel = get_active_channel(config)
    throttle = PageThrottle(delay_min=2.0, delay_max=5.0)
    deal_breakers = config.get("profile", {}).get("deal_breakers", [])
    jd_deal_breakers = config.get("profile", {}).get("jd_deal_breakers", [])
    progress_callback = config.get("_workbench_collect_progress")
    seen_count = 0
    blocked_companies = config.get("profile", {}).get("blocked_companies", [])
    new_count = 0
    duplicate_count = 0

    def report_progress() -> None:
        if callable(progress_callback):
            progress_callback({"seen": seen_count, "new": new_count, "duplicate": duplicate_count})

    # Pagination config
    search_config = config.get("search", {})
    max_pages = min(search_config.get("max_pages", 3), channel.pages_cap or 10)  # 渠道上限

    # Resolve cities: search.cities > profile.target_cities > ["北京"]
    cities = search_config.get("cities", [])
    if not cities:
        cities = config.get("profile", {}).get("target_cities", ["北京"])

    # Build search combinations: city × keyword
    search_combos = []
    for city in cities:
        city_code = _resolve_city_code(city, config, channel)
        if not city_code:
            console.print(f"[yellow]⚠ 未识别的城市: {city}，已跳过[/yellow]")
            continue
        for keyword in keywords:
            search_combos.append((city, city_code, keyword))

    if not search_combos:
        console.print("[red]没有有效的搜索组合（检查城市配置）[/red]")
        db.close()
        return 0

    if stop_event is not None and stop_event.is_set():
        db.close()
        return 0

    console.print(f"[dim]搜索组合: {len(search_combos)} 个 ({len(cities)}城市 × {len(keywords)}关键词 × {max_pages}页)[/dim]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        for city, city_code, keyword in search_combos:
            if stop_event is not None and stop_event.is_set():
                break
            if limit is not None and new_count >= limit:
                break

            label = f"{city}/{keyword}" if len(cities) > 1 else keyword
            task = progress.add_task(f"搜索: {label}", total=None)
            keyword_new = 0

            for page in range(1, max_pages + 1):
                if stop_event is not None and stop_event.is_set():
                    break
                if limit is not None and new_count >= limit:
                    break

                # Build paginated URL
                search_url = channel.build_search_url(
                    keyword, city_code, page=page, sort=search_config.get("sort", "")
                )

                with platform_browser_lock(channel.lock_key).context(BrowserPriority.COLLECT):
                    # Open the first search page in the current window foreground
                    # so the user can see collect starting in Chrome; subsequent
                    # pages open in the background to avoid stealing focus.
                    target_id = new_tab(search_url, background=(page > 1))
                    if not target_id:
                        if page == 1:
                            progress.update(task, description=f"[red]✗ 无法打开搜索页: {label}[/red]")
                        break

                    if _wait_or_stop(stop_event, 3):
                        close_tab(target_id)
                        break
                    wait_for_load(target_id, timeout=10)
                    if stop_event is not None and stop_event.is_set():
                        close_tab(target_id)
                        break

                    # Scroll to load all results on this page
                    scroll(target_id, y=2000)
                    if _wait_or_stop(stop_event, 1.5):
                        close_tab(target_id)
                        break
                    scroll(target_id, y=4000)
                    if _wait_or_stop(stop_event, 1.5):
                        close_tab(target_id)
                        break

                    # Extract job list
                    result = evaluate(target_id, channel.js_extract_list)
                    if not result:
                        close_tab(target_id)
                        break

                    try:
                        jobs_list = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        close_tab(target_id)
                        break

                close_tab(target_id)

                # No results on this page, stop pagination
                if not jobs_list:
                    break

                progress.update(task, description=f"搜索: {label} 第{page}页 ({len(jobs_list)}条)")

                # Process each job
                for job_data in jobs_list:
                    if stop_event is not None and stop_event.is_set():
                        break
                    if limit is not None and new_count >= limit:
                        break

                    seen_count += 1
                    report_progress()
                    job_url = job_data.get("url", "")
                    job_id = channel.generate_job_id(job_url)

                    # Skip if already exists
                    if job_exists(db, job_id):
                        duplicate_count += 1
                        report_progress()
                        continue

                    # Skip deal breakers
                    if matching_deal_breaker(job_data.get("title", ""), deal_breakers):
                        continue
                    if matching_blocked_company(job_data.get("company", ""), blocked_companies):
                        continue

                    # Open detail page for full JD. 详情页的浏览器操作（开 tab →
                    # 等待加载 → 提取 → 关闭）也要走平台锁，否则它会和发送/监测
                    # 并发操作同一个 Chrome，破坏页面状态。锁粒度为单条详情。
                    detail_url = channel.build_job_url(job_data)
                    detail: dict = {}
                    if channel.detail_required:
                        # 打开详情页抽取完整信息（默认行为，如 BOSS）。
                        # 详情页操作也在平台锁内，避免与发送/监测并发破坏页面状态。
                        if throttle.wait(stop_event):
                            break
                        with platform_browser_lock(channel.lock_key).context(BrowserPriority.COLLECT):
                            detail_target = new_tab(detail_url, background=True)
                            if not detail_target:
                                continue

                            if _wait_or_stop(stop_event, 2):
                                close_tab(detail_target)
                                break
                            wait_for_load(detail_target, timeout=10)
                            if stop_event is not None and stop_event.is_set():
                                close_tab(detail_target)
                                break

                            # Extract detail
                            detail_result = evaluate(detail_target, channel.js_extract_detail)
                            close_tab(detail_target)

                        if not detail_result:
                            continue

                        try:
                            detail = json.loads(detail_result)
                        except (json.JSONDecodeError, TypeError):
                            continue
                    else:
                        # 渠道列表页已携带完整详情（智联 __INITIAL_STATE__），
                        # 直接使用列表数据，不再逐条开详情页。
                        detail = dict(job_data)

                    # Build job record
                    job_record = {
                        "id": job_id,
                        "title": detail.get("title", job_data.get("title", "")),
                        "company": detail.get("company", job_data.get("company", "")),
                        "salary": detail.get("salary", job_data.get("salary", "")),
                        "city": city,
                        "experience": detail.get("experience", job_data.get("experience", "")),
                        "jd": detail.get("jd", ""),
                        "hr_name": detail.get("hr_name", ""),
                        "hr_title": detail.get("hr_title", ""),
                        "hr_active": detail.get("hr_active", ""),
                        "company_size": detail.get("company_size", ""),
                        "company_industry": detail.get("company_industry", ""),
                        "url": detail_url,
                        "channel": channel.key,
                    }

                    if matching_deal_breaker(job_record["jd"], jd_deal_breakers):
                        continue

                    insert_job(db, job_record)
                    if collected_job_ids is not None:
                        collected_job_ids.append(job_id)
                    if on_new_job is not None:
                        try:
                            on_new_job(job_id, job_record)
                        except Exception:
                            # A callback failure must not abort scraping.
                            pass
                    new_count += 1
                    keyword_new += 1
                    report_progress()
                    progress.update(task, description=f"搜索: {label} 第{page}页 (新增 {keyword_new})")

                # Anti-scraping: pause between pages
                if page < max_pages:
                    if _wait_or_stop(stop_event, random.uniform(3.0, 6.0)):
                        break

            progress.update(task, description=f"搜索: {label} (新增 {keyword_new})")

    report_progress()
    db.close()
    return new_count
