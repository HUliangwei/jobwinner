"""Sender module - Auto-send greetings with throttle control."""

import time
import json
from threading import Event
from urllib.parse import urljoin
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from jobwinner.browser import (
    new_tab,
    close_tab,
    evaluate,
    click_at,
    get_page_targets,
    navigate,
    press_key,
    type_text,
    wait_for_load,
)
from jobwinner.db import get_db, get_jobs_ready_to_send, update_job_status, add_history, add_risk_event
from jobwinner.throttle import RequestThrottle, SendWindowChecker, ProgressiveBackoff, should_take_day_off
from jobwinner.browser_lock import BrowserPriority, platform_browser_lock
from jobwinner.channels import get_active_channel, set_active_channel, current_channel

console = Console()

CHAT_BUTTON_SELECTOR = (
    'a[redirect-url*="/web/geek/chat"], '
    'a[data-url*="/friend/add"], '
    'a.btn-startchat, '
    '[ka="job_detail_chat"], '
    '[ka^="go_chat"], '
    '[ka*="gochat"], '
    '.op-btn-chat, '
    '.btn-startchat-wrap'
)

CHAT_BUTTON_SCRIPT_FOR_TESTS = """
(() => {
    const selectors = [
        'a[redirect-url*="/web/geek/chat"]',
        'a[data-url*="/friend/add"]',
        'a.btn-startchat',
        '[ka="job_detail_chat"]',
        '[ka^="go_chat"]',
        '[ka*="gochat"]',
        '.op-btn-chat',
        '.btn-startchat-wrap'
    ];
    const candidates = selectors.flatMap((selector, priority) =>
        Array.from(document.querySelectorAll(selector)).map((el) => ({el, selector, priority}))
    );
    const elementState = (el) => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        const visible = !!(
            rect.width && rect.height &&
            style.display !== 'none' &&
            style.visibility !== 'hidden' &&
            style.pointerEvents !== 'none'
        );
        const inViewport = visible && rect.bottom > 0 && rect.right > 0 &&
            rect.top < innerHeight && rect.left < innerWidth;
        const x = Math.min(Math.max(rect.x + rect.width / 2, 0), innerWidth - 1);
        const y = Math.min(Math.max(rect.y + rect.height / 2, 0), innerHeight - 1);
        const top = inViewport ? document.elementFromPoint(x, y) : null;
        const topmost = !!(top && (top === el || el.contains(top)));
        return {rect, visible, inViewport, topmost};
    };
    const isVisible = (el) => elementState(el).visible;
    const score = (item) => {
        const el = item.el;
        const text = (el.innerText || el.textContent || '').trim();
        const ka = el.getAttribute('ka') || '';
        const redirectUrl = el.getAttribute('redirect-url') || '';
        const dataUrl = el.getAttribute('data-url') || '';
        const tagName = String(el.tagName || '').toLowerCase();
        let value = 0;
        if (isVisible(el)) value += 1000;
        const state = elementState(el);
        if (state.inViewport) value += 500;
        if (state.topmost) value += 300;
        if (tagName === 'a') value += 200;
        if (redirectUrl.includes('/web/geek/chat')) value += 300;
        if (dataUrl.includes('/friend/add')) value += 250;
        if (el.classList && el.classList.contains('btn-startchat')) value += 120;
        if (text.includes('沟通')) value += 80;
        if (ka === 'job_detail_chat' || ka.includes('go_chat') || ka.includes('gochat')) value += 60;
        if (el.classList && el.classList.contains('btn-startchat-wrap')) value -= 100;
        return value - item.priority;
    };
    const matches = candidates
        .filter((item) => {
            const el = item.el;
            const text = (el.innerText || el.textContent || '').trim();
            const ka = el.getAttribute('ka') || '';
            const redirectUrl = el.getAttribute('redirect-url') || '';
            const dataUrl = el.getAttribute('data-url') || '';
            return (
                text.includes('沟通') ||
                redirectUrl.includes('/web/geek/chat') ||
                dataUrl.includes('/friend/add') ||
                ka === 'job_detail_chat' ||
                ka.includes('go_chat') ||
                ka.includes('gochat')
            );
        })
        .sort((a, b) => score(b) - score(a));
    const btn = matches[0] && matches[0].el;
    if (!btn) return JSON.stringify({
        success: false,
        error: 'no_chat_button',
        candidates: candidates.map((item) => {
            const el = item.el;
            const text = (el.innerText || el.textContent || '').trim();
            return {
                text,
                ka: el.getAttribute('ka'),
                className: String(el.className || ''),
                tagName: el.tagName,
                redirectUrl: el.getAttribute('redirect-url'),
                dataUrl: el.getAttribute('data-url'),
                visible: isVisible(el)
            };
        })
    });
    btn.scrollIntoView({block: 'center', inline: 'center'});
    const rect = btn.getBoundingClientRect();
    btn.click();
    return JSON.stringify({
        success: true,
        interaction: 'dom_click',
        x: rect.x + rect.width / 2,
        y: rect.y + rect.height / 2,
        button_text: (btn.innerText || btn.textContent || '').trim(),
        ka: btn.getAttribute('ka'),
        className: String(btn.className || ''),
        tagName: btn.tagName,
        redirectUrl: btn.getAttribute('redirect-url'),
        dataUrl: btn.getAttribute('data-url'),
        visible: isVisible(btn)
    });
})()
"""


def _parse_js_result(result) -> dict:
    if not result:
        return {"success": False, "error": "no_response"}
    if isinstance(result, dict):
        return result
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return {"success": False, "error": "parse_error"}


def _stop_requested(stop_event) -> bool:
    return bool(stop_event and stop_event.is_set())


def _sleep_or_stop(seconds: float, stop_event) -> bool:
    if stop_event:
        return bool(stop_event.wait(seconds))
    time.sleep(seconds)
    return False


# ────────────────────────────────────────────────────────────────
# 智联招聘：投递 + 招呼语发送（适配器 supports_send=True）
# 实测定稿（2026-08-31 登录态）：
#   岗位页「立即投递」→ 弹窗选简历(.a-attachment-select__item)
#   →「投递简历」(.a-attachment-select__action-btn__delivery) → 平台自动带默认
#   打招呼语投递（无自定义输入框）→ 按钮变「继续沟通」
#   → 进入 i.zhaopin.com/im 会话 → textarea.im-sender__input
#   → type_text 输入自定义招呼语 → Enter 发送（按Enter键发送）
# ────────────────────────────────────────────────────────────────


def _zhaopin_page_state(target_id: str) -> dict:
    """Detect the zhaopin job detail page apply state."""
    js = """
    (() => {
        const norm = (s) => (s || '').trim();
        const findBtn = (text) => Array.from(document.querySelectorAll('button, a, div[role="button"], [class*="button"]'))
            .find((e) => norm(e.innerText) === text);
        const apply = !!findBtn('立即投递');
        const cont = !!findBtn('继续沟通');
        return JSON.stringify({
            state: apply ? 'apply' : (cont ? 'continue' : 'unknown')
        });
    })()
    """
    return _parse_js_result(evaluate(target_id, js))


def _zhaopin_click_button(target_id: str, text: str) -> bool:
    """Click the zhaopin action whose visible text matches exactly."""
    text_json = json.dumps(text, ensure_ascii=False)
    js = """
    (() => {
        const text = __TEXT__;
        const el = Array.from(document.querySelectorAll('button, a, div[role="button"], [class*="button"]'))
            .find((e) => (e.innerText || '').trim() === text);
        if (!el) return JSON.stringify({ok: false});
        el.click();
        return JSON.stringify({ok: true});
    })()
    """
    result = _parse_js_result(evaluate(target_id, js.replace("__TEXT__", text_json)))
    return bool(result.get("ok"))


_ZHAOPIN_RESUME_PICK_JS = """
(() => {
    const kw = '__RESUME_KW__';
    const items = Array.from(document.querySelectorAll('.a-attachment-select__item, [class*="modal"] [class*="item"], [class*="modal"] li'));
    const hit = items.find((e) => {
        const txt = (e.innerText || '').trim();
        return txt.includes(kw) && (txt.includes('简历') || txt.includes('在线'));
    }) || items[0];
    if (!hit) return JSON.stringify({ok: false});
    hit.click();
    return JSON.stringify({ok: true});
})()
"""

_ZHAOPIN_DELIVER_SUBMIT_JS = """
(() => {
    const el = Array.from(document.querySelectorAll('.a-attachment-select__action-btn__delivery, [class*="modal"] a, [class*="modal"] button'))
        .find((e) => (e.innerText || '').trim() === '投递简历');
    if (!el) return JSON.stringify({ok: false});
    el.click();
    return JSON.stringify({ok: true});
})()
"""


def _zhaopin_apply(target_id: str, resume_keyword: str, stop_event) -> dict:
    """Click 立即投递 → pick resume → submit delivery (real apply)."""
    if not _zhaopin_click_button(target_id, "立即投递"):
        return {"success": False, "error": "no_apply_button", "history_detail": "未找到「立即投递」按钮"}
    if _sleep_or_stop(2.5, stop_event):
        return {"success": False, "error": "stopped", "skip_backoff": True}

    kw = json.dumps(resume_keyword or "在线", ensure_ascii=False)
    picked = _parse_js_result(evaluate(target_id, _ZHAOPIN_RESUME_PICK_JS.replace("__RESUME_KW__", kw)))
    if not picked.get("ok"):
        return {"success": False, "error": "resume_pick_failed", "history_detail": "投递弹窗中未找到简历选项"}
    if _sleep_or_stop(1.2, stop_event):
        return {"success": False, "error": "stopped", "skip_backoff": True}

    submitted = _parse_js_result(evaluate(target_id, _ZHAOPIN_DELIVER_SUBMIT_JS))
    if not submitted.get("ok"):
        return {"success": False, "error": "deliver_submit_failed", "history_detail": "未找到「投递简历」提交按钮"}
    # 等投递完成（弹窗出现「已向对方发送简历和打招呼语」/按钮切换）
    for _ in range(4):
        if _stop_requested(stop_event):
            return {"success": False, "error": "stopped", "skip_backoff": True}
        if _sleep_or_stop(1.0, stop_event):
            return {"success": False, "error": "stopped", "skip_backoff": True}
        state = _zhaopin_page_state(target_id)
        if state.get("state") != "apply":
            return {"success": True}
    return {"success": False, "error": "deliver_timeout", "history_detail": "投递未确认成功（弹窗未切换状态）", "skip_backoff": True}


def _zhaopin_open_chat(target_id: str, stop_event) -> dict:
    """Click 继续沟通 (post-deliver) and wait for the IM session input."""
    if not _zhaopin_click_button(target_id, "继续沟通"):
        return {"success": False, "error": "no_chat_entry", "history_detail": "未找到「继续沟通」入口"}
    for _ in range(15):
        if _stop_requested(stop_event):
            return {"success": False, "error": "stopped", "skip_backoff": True}
        info = _parse_js_result(evaluate(target_id, """
        (() => {
            const hasInput = !!document.querySelector('textarea.im-sender__input, [contenteditable="true"].im-sender__input');
            return JSON.stringify({ hasInput });
        })()
        """))
        if info.get("hasInput"):
            return {"success": True}
        if _sleep_or_stop(1, stop_event):
            return {"success": False, "error": "stopped", "skip_backoff": True}
    return {"success": False, "error": "chat_not_ready", "history_detail": "会话页未就绪（找不到消息输入框）"}


_ZHAOPIN_SEND_VERIFY_JS = """
(() => {
    const input = document.querySelector('textarea.im-sender__input, [contenteditable="true"].im-sender__input');
    const norm = (s) => (s || '').replace(/\s+/g, '').trim();
    const expected = norm('__GREETING__');
    const cur = norm(input ? (input.value || input.innerText || '') : '');
    const sent = Array.from(document.querySelectorAll('[class*="message"], [class*="msg"], [class*="bubble"], [class*="dialog"]'))
        .some((e) => {
            const t = norm(e.innerText);
            return t.length > 0 && t.includes(expected.slice(0, 18));
        });
    return JSON.stringify({ cleared: cur.length === 0, sent });
})()
"""


def _send_zhaopin_greeting_once_locked(
    job: dict,
    greeting: str,
    throttle_config: dict,
    resume_keyword: str = "在线",
    phase_callback=None,
) -> tuple[dict, str | None]:
    """Deliver the resume on zhaopin, then send the custom greeting in IM."""
    stop_event = throttle_config.get("_workbench_stop_event")
    target_id = new_tab(job["url"], background=True)
    if not target_id:
        return {"success": False, "error": "open_page_failed", "history_detail": "无法打开页面", "skip_backoff": True}, None

    if _stop_requested(stop_event):
        close_tab(target_id)
        return {"success": False, "error": "stopped", "skip_backoff": True}, None

    # 等页面加载完成（SPA 首屏可能较慢）
    wait_for_load(target_id, timeout=12)
    if _stop_requested(stop_event):
        close_tab(target_id)
        return {"success": False, "error": "stopped", "skip_backoff": True}, None

    # 模拟浏览（防检测，与 BOSS 发送一致）
    browse_min = throttle_config.get("browse_duration_min", 15)
    browse_max = throttle_config.get("browse_duration_max", 30)
    if throttle_config.get("browse_before_greet", True):
        import random
        browse_time = random.uniform(browse_min, browse_max)
        if phase_callback:
            try:
                phase_callback(job, "browsing", {"browse_seconds": browse_time})
            except Exception:
                pass
        if _sleep_or_stop(browse_time, stop_event):
            close_tab(target_id)
            return {"success": False, "error": "stopped", "skip_backoff": True}, None
        if phase_callback:
            try:
                phase_callback(job, "browsed", {})
            except Exception:
                pass

    # 等投递/沟通按钮出现（页面 SPA 渲染完成后才有）
    state = _zhaopin_page_state(target_id)
    for _ in range(6):
        if state.get("state") != "unknown":
            break
        if _sleep_or_stop(1, stop_event):
            close_tab(target_id)
            return {"success": False, "error": "stopped", "skip_backoff": True}, None
        state = _zhaopin_page_state(target_id)

    if state.get("state") == "apply":
        apply_result = _zhaopin_apply(target_id, resume_keyword, stop_event)
        if not apply_result.get("success"):
            close_tab(target_id)
            return apply_result, None
        if _sleep_or_stop(1.5, stop_event):
            close_tab(target_id)
            return {"success": False, "error": "stopped", "skip_backoff": True}, None
    elif state.get("state") not in {"continue"}:
        close_tab(target_id)
        return {"success": False, "error": "no_apply_button", "history_detail": "未找到投递/沟通入口，岗位可能已下架", "skip_backoff": True}, None

    # 进入会话发送自定义招呼语（best-effort；投递本身已成功）
    chat_result = _zhaopin_open_chat(target_id, stop_event)
    if chat_result.get("error") == "stopped":
        close_tab(target_id)
        return chat_result, None
    greeting_sent = False
    chat_detail = ""
    if chat_result.get("success"):
        if type_text(target_id, greeting, human=False):
            if _sleep_or_stop(0.8, stop_event):
                close_tab(target_id)
                return {"success": False, "error": "stopped", "skip_backoff": True}, None
            if press_key(target_id, "Enter"):
                if _sleep_or_stop(1.6, stop_event):
                    close_tab(target_id)
                    return {"success": False, "error": "stopped", "skip_backoff": True}, None
                g_json = json.dumps(greeting, ensure_ascii=False)
                check = _parse_js_result(evaluate(target_id, _ZHAOPIN_SEND_VERIFY_JS.replace("__GREETING__", g_json)))
                greeting_sent = bool(check.get("cleared") or check.get("sent"))
            else:
                chat_detail = "发送键触发失败"
        else:
            chat_detail = "招呼语输入失败"
    else:
        chat_detail = chat_result.get("history_detail", "会话未就绪")
    close_tab(target_id)

    if greeting_sent:
        return {"success": True, "greeting_sent": True, "history_detail": "已投递简历并发送招呼语"}, None
    return {"success": True, "greeting_sent": False, "history_detail": "已投递简历（招呼语补充未确认：%s）" % chat_detail}, None


def _send_zhaopin_greeting_once(
    job: dict,
    greeting: str,
    throttle_config: dict,
    resume_keyword: str = "在线",
    phase_callback=None,
) -> tuple[dict, str | None]:
    """Serialize zhaopin browser ops under the zhaopin platform lock."""
    from jobwinner.channels import get_channel

    lock_key = get_channel("zhaopin").lock_key
    with platform_browser_lock(lock_key).context(BrowserPriority.DELIVER):
        return _send_zhaopin_greeting_once_locked(
            job, greeting, throttle_config,
            resume_keyword=resume_keyword,
            phase_callback=phase_callback,
        )


def _detect_greet_popup(target_id: str) -> dict:
    detect_popup_js = """
    (() => {
        const visible = (element) => element && !!(
            element.offsetWidth || element.offsetHeight || element.getClientRects().length
        );
        const explicitPreset = Array.from(document.querySelectorAll('.greet-boss-pop, .greet-pop'))
            .find((element) => visible(element));
        if (explicitPreset) {
            return JSON.stringify({success: true, popup: true, kind: 'preset_greeting'});
        }

        const startChat = Array.from(document.querySelectorAll('.dialog-wrap.startchat-dialog'))
            .find((element) => visible(element) && element.querySelector('textarea.input-area'));
        if (startChat) {
            return JSON.stringify({success: true, popup: true, kind: 'startchat_dialog'});
        }

        const preset = Array.from(document.querySelectorAll('.dialog-wrap'))
            .find((element) => {
                if (!visible(element)) return false;
                const text = (element.innerText || element.textContent || '').trim();
                const hasEditableGreeting = !!element.querySelector('textarea.input-area');
                return !hasEditableGreeting && /预设招呼语|默认招呼语|自动招呼语|打招呼语/.test(text);
            });
        if (preset) {
            return JSON.stringify({success: true, popup: true, kind: 'preset_greeting'});
        }
        return JSON.stringify({success: true, popup: false, kind: null});
    })()
    """
    return _parse_js_result(evaluate(target_id, detect_popup_js))


def _is_preset_greeting_popup(state: dict) -> bool:
    if state.get("action") == "no_popup":
        return False
    if not state.get("popup"):
        return False
    return state.get("kind") in {None, "preset_greeting"}


def _confirm_preset_greeting(target_id: str) -> dict:
    result = _parse_js_result(evaluate(target_id, """
    (() => {
        const visible = (element) => {
            if (!element) return false;
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return !!(rect.width && rect.height && style.display !== 'none'
                && style.visibility !== 'hidden' && style.pointerEvents !== 'none');
        };
        const dialogs = Array.from(document.querySelectorAll('.greet-boss-pop, .greet-pop, .dialog-wrap'))
            .filter(visible);
        const popup = dialogs.find((element) =>
            element.matches('.greet-boss-pop, .greet-pop')
            || /预设招呼语|默认招呼语|自动招呼语|打招呼语/.test(
                (element.innerText || element.textContent || '').trim()
            )
        );
        if (!popup) return JSON.stringify({success: false, error: 'preset_popup_missing'});
        const buttons = Array.from(popup.querySelectorAll(
            '[ka="dialog_confirm"], .btn-sure, button, [role="button"]'
        )).filter((element) => {
            if (!visible(element) || element.disabled || element.classList.contains('disabled')) return false;
            const text = (element.innerText || element.textContent || '').trim();
            return element.matches('[ka="dialog_confirm"], .btn-sure')
                || /确定|确认|继续|开始沟通|立即沟通/.test(text);
        });
        const button = buttons[0];
        if (!button) return JSON.stringify({success: false, error: 'preset_confirm_missing'});
        button.scrollIntoView({block: 'center', inline: 'center'});
        button.click();
        return JSON.stringify({success: true, action: 'preset_confirmed'});
    })()
    """))
    if not result.get("success"):
        return {
            **result,
            "history_detail": "检测到平台招呼语，但无法确认招呼语弹窗",
            "skip_backoff": True,
        }
    return {"success": True, "action": "preset_confirmed"}


def _submit_startchat_greeting(target_id: str, greeting: str) -> dict:
    greeting_escaped = json.dumps(greeting, ensure_ascii=False)
    input_state = _parse_js_result(evaluate(target_id, """
    (() => {
        const visible = (element) => {
            if (!element) return false;
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return !!(rect.width && rect.height && style.display !== 'none'
                && style.visibility !== 'hidden' && style.pointerEvents !== 'none');
        };
        const dialog = Array.from(document.querySelectorAll('.dialog-wrap.startchat-dialog'))
            .find(visible);
        const input = dialog && Array.from(dialog.querySelectorAll('textarea.input-area, textarea'))
            .find(visible);
        if (!input) return JSON.stringify({success: false, error: 'startchat_input_missing'});
        input.scrollIntoView({block: 'center', inline: 'center'});
        const rect = input.getBoundingClientRect();
        return JSON.stringify({
            success: true,
            x: rect.x + rect.width / 2,
            y: rect.y + rect.height / 2
        });
    })()
    """))
    if not input_state.get("success"):
        return {
            **input_state,
            "history_detail": "首次沟通弹窗中未找到招呼语输入框",
            "skip_backoff": True,
        }
    if not click_at(target_id, f"{input_state['x']},{input_state['y']}"):
        return {"success": False, "error": "startchat_input_focus_failed", "skip_backoff": True}
    if not press_key(target_id, "SelectAll") or not press_key(target_id, "Backspace"):
        return {"success": False, "error": "startchat_input_clear_failed", "skip_backoff": True}
    if not type_text(target_id, greeting, human=True):
        return {"success": False, "error": "startchat_trusted_input_failed", "skip_backoff": True}

    submit_state = _parse_js_result(evaluate(target_id, f"""
    (() => {{
        const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
        const visible = (element) => {{
            if (!element) return false;
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return !!(rect.width && rect.height && style.display !== 'none'
                && style.visibility !== 'hidden' && style.pointerEvents !== 'none');
        }};
        const dialog = Array.from(document.querySelectorAll('.dialog-wrap.startchat-dialog'))
            .find(visible);
        const input = dialog && Array.from(dialog.querySelectorAll('textarea.input-area, textarea'))
            .find(visible);
        if (!input || normalize(input.value) !== normalize({greeting_escaped})) {{
            return JSON.stringify({{success: false, error: 'startchat_input_not_filled'}});
        }}
        const buttons = Array.from(dialog.querySelectorAll(
            '.send-message, [ka="dialog_confirm"], .btn-sure, .btn-send, '
            + '.send-btn, button, [role="button"]'
        )).filter((element) => {{
            if (!visible(element) || element.disabled || element.classList.contains('disabled')) return false;
            const text = (element.innerText || element.textContent || '').trim();
            return element.matches(
                '.send-message, [ka="dialog_confirm"], .btn-sure, .btn-send, .send-btn'
            ) || /发送|确定|开始沟通|立即沟通/.test(text);
        }});
        const button = buttons[0];
        if (!button) return JSON.stringify({{success: false, error: 'startchat_submit_missing'}});
        button.scrollIntoView({{block: 'center', inline: 'center'}});
        const rect = button.getBoundingClientRect();
        return JSON.stringify({{
            success: true,
            x: rect.x + rect.width / 2,
            y: rect.y + rect.height / 2
        }});
    }})()
    """))
    if not submit_state.get("success"):
        return {
            **submit_state,
            "history_detail": "首次沟通招呼语未被 BOSS 输入组件接受",
            "skip_backoff": True,
        }
    if not click_at(target_id, f"{submit_state['x']},{submit_state['y']}"):
        return {
            "success": False,
            "error": "startchat_submit_click_failed",
            "history_detail": "首次沟通招呼语已填写，但真实提交点击失败",
            "skip_backoff": True,
        }
    return {"success": True, "action": "first_contact_submitted"}


def _navigate_to_chat_redirect(target_id: str, click_result: dict) -> bool:
    """Reuse the platform's own chat destination without foregrounding the job tab."""
    redirect_url = str(click_result.get("redirectUrl") or "").strip()
    channel = current_channel()
    # Platform-specific: BOSS redirects via /web/geek/chat; other channels may
    # use their own chat paths. Adapters that need stricter matching override
    # ``is_chat_path`` if added later.
    if not redirect_url.startswith("/web/geek/chat"):
        return False
    base = channel.base_url or f"https://{channel.domain}"
    return navigate(target_id, urljoin(base, redirect_url))


def _handle_greet_popup(target_id: str, greeting: str, click_result: dict | None = None) -> dict:
    state = _detect_greet_popup(target_id)
    if not state.get("success"):
        return {
            "success": False,
            "error": state.get("error", "popup_detection_failed"),
            "history_detail": "无法识别首次沟通页面状态",
            "skip_backoff": True,
        }
    if _is_preset_greeting_popup(state):
        return _confirm_preset_greeting(target_id)
    if state.get("kind") == "startchat_dialog":
        if click_result and _navigate_to_chat_redirect(target_id, click_result):
            return {"success": True, "action": "startchat_redirected"}
        return {
            "success": False,
            "error": "startchat_redirect_unavailable",
            "history_detail": "首次沟通弹窗缺少可验证的聊天地址，已停止发送且未切换前台",
            "skip_backoff": True,
        }
    return {"success": True, "action": "no_popup"}


def _chat_target_matches_job(target_id: str, job: dict) -> bool:
    job_id = json.dumps(str(job.get("id") or ""), ensure_ascii=False)
    company = json.dumps(str(job.get("company") or ""), ensure_ascii=False)
    title = json.dumps(str(job.get("title") or ""), ensure_ascii=False)
    result = _parse_js_result(evaluate(target_id, f"""
    (() => {{
        const normalize = (value) => String(value || '').replace(/\\s+/g, '').toLowerCase();
        const expectedId = normalize({job_id});
        const expectedCompany = normalize({company});
        const expectedTitle = normalize({title});
        const activeRoots = Array.from(new Set([
            document.querySelector('.chat-conversation'),
            document.querySelector('.friend-content.selected')
        ].filter(Boolean)));
        const activeText = normalize(
            activeRoots.map((element) => element.innerText || element.textContent || '').join(' ')
        );
        const activeHtml = activeRoots.map((element) => element.outerHTML || '').join(' ');
        const idMatch = !!expectedId && (
            location.href.includes(expectedId) ||
            activeHtml.includes(expectedId)
        );
        const identityMatch = !!expectedCompany && activeText.includes(expectedCompany) &&
            (!expectedTitle || activeText.includes(expectedTitle));
        return JSON.stringify({{success: true, matches: idMatch || identityMatch}});
    }})()
    """))
    return bool(result.get("success") and result.get("matches"))


def _wait_for_chat_page(
    target_id: str,
    stop_event,
    attempts: int = 20,
    job: dict | None = None,
    excluded_target_ids: set[str] | None = None,
) -> dict:
    for _ in range(attempts):
        if _sleep_or_stop(0.5, stop_event):
            close_tab(target_id)
            return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}
        url_now = evaluate(target_id, "location.pathname")
        if url_now and "/web/geek/chat" in url_now:
            if job is None or _chat_target_matches_job(target_id, job):
                return {"success": True, "target_id": target_id}
        if job is not None:
            for candidate in get_page_targets():
                candidate_id = str(candidate.get("targetId") or "")
                candidate_url = str(candidate.get("url") or "")
                if (
                    candidate_id
                    and candidate_id != target_id
                    and "/web/geek/chat" in candidate_url
                    and _chat_target_matches_job(candidate_id, job)
                ):
                    return {"success": True, "target_id": candidate_id, "opened_new_tab": True}
    return {"success": False, "error": "chat_navigation_timeout"}


def _click_chat_button(target_id: str, stop_event, attempts: int = 30) -> dict:
    click_chat_js = CHAT_BUTTON_SCRIPT_FOR_TESTS

    last_result: dict = {"success": False, "error": "no_chat_button"}
    for attempt in range(max(1, attempts)):
        if _stop_requested(stop_event):
            return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}
        last_result = _parse_js_result(evaluate(target_id, click_chat_js))
        if last_result.get("success"):
            return last_result
        if attempt < attempts - 1 and _sleep_or_stop(1, stop_event):
            return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}

    return last_result


def _adopt_chat_target(current_target_id: str, chat_ready: dict) -> str:
    """Switch to a matching chat tab and close the superseded job tab."""
    next_target_id = str(chat_ready.get("target_id") or current_target_id)
    if next_target_id != current_target_id:
        close_tab(current_target_id)
    return next_target_id


def _message_delivery_state(target_id: str, greeting: str) -> str:
    greeting_escaped = json.dumps(greeting, ensure_ascii=False)
    result = _parse_js_result(evaluate(target_id, f"""
    (() => {{
        const normalize = (value) => String(value || '')
            .replace(/[\\u200b-\\u200f\\ufeff]/g, '')
            .replace(/\\s+/g, ' ')
            .trim();
        const removeDeliveryLabels = (value) => normalize(value)
            .replace(/(发送中|已读|未读|送达|发送成功|重试|重新发送)$/g, '')
            .trim();
        const textMatchesExpected = (value, expectedText) => {{
            const text = removeDeliveryLabels(value);
            return text === expectedText || text.includes(expectedText);
        }};
        const messageText = (node) => {{
            const contentNode = node.querySelector(
                '.message-content, .text, .content, .message-text, '
                + '[class*="message-content"], [class*="msg-content"], [class*="text"]'
            );
            return normalize(contentNode ? contentNode.innerText || contentNode.textContent : node.innerText || node.textContent);
        }};
        const expected = normalize({greeting_escaped});
        const ownMessages = Array.from(document.querySelectorAll(
            '.chat-record .message-item.item-myself, .chat-record .item-myself, '
            + '.chat-record .message-item.item-self, .chat-record [class*="item-my"]'
        ));
        const matching = ownMessages.filter((node) => textMatchesExpected(messageText(node), expected));
        const messageList = document.querySelector('.chat-record');
        const vue = messageList && messageList.__vue__;
        const records = vue && Array.isArray(vue.list$) ? vue.list$ : [];
        const matchingRecords = records.filter((message) => {{
            if (!message || !message.isSelf) return false;
            const text = message.text || message.lastText || message.content || message.message || message.body || '';
            return textMatchesExpected(text, expected);
        }});
        if (!matching.length && !matchingRecords.length) {{
            return JSON.stringify({{success: true, state: 'missing'}});
        }}
        const domStates = matching.map((node) => {{
            const statusNode = node.querySelector('.message-status');
            const statusClass = statusNode ? String(statusNode.className || '') : '';
            if (statusClass.includes('status-error')) return 'failed';
            if (statusClass.includes('status-loading')) return 'pending';
            return 'delivered';
        }});
        const recordStates = matchingRecords.map((record) => {{
            const status = Number(record.status);
            if (status === 4) return 'failed';
            if (status === 0) return 'pending';
            return 'delivered';
        }});
        const states = [...domStates, ...recordStates];
        const state = states.includes('delivered') ? 'delivered'
            : states.includes('pending') ? 'pending' : 'failed';
        return JSON.stringify({{success: true, state}});
    }})()
    """))
    if not result.get("success"):
        return "missing"
    return str(result.get("state") or "missing")


def _submitted_message_looks_accepted(target_id: str, greeting: str) -> bool:
    """Return true when Boss appears to have accepted a send despite missing echo."""
    greeting_escaped = json.dumps(greeting, ensure_ascii=False)
    result = _parse_js_result(evaluate(target_id, f"""
    (() => {{
        const normalize = (value) => String(value || '')
            .replace(/[\\u200b-\\u200f\\ufeff]/g, '')
            .replace(/\\s+/g, ' ')
            .trim();
        const expected = normalize({greeting_escaped});
        const input = document.querySelector('#chat-input');
        const inputText = normalize(input ? input.innerText || input.textContent || input.value : '');
        const inputCleared = !!input && inputText.length === 0;
        const ownMessages = Array.from(document.querySelectorAll(
            '.chat-record .message-item.item-myself, .chat-record .item-myself, '
            + '.chat-record .message-item.item-self, .chat-record [class*="item-my"]'
        ));
        const hasFailedOwnMessage = ownMessages.some((node) => {{
            const statusNode = node.querySelector('.message-status');
            const statusClass = statusNode ? String(statusNode.className || '') : '';
            const text = normalize(node.innerText || node.textContent);
            return statusClass.includes('status-error')
                || (text.includes('发送失败') && (text.includes(expected) || expected.includes(text)));
        }});
        return JSON.stringify({{
            success: true,
            accepted: inputCleared && !hasFailedOwnMessage,
            inputCleared,
            hasFailedOwnMessage
        }});
    }})()
    """))
    return bool(result.get("success") and result.get("accepted"))


def _submit_chat_message_background(target_id: str, greeting: str) -> dict:
    """Use JobWinner's original Vue submit path without foregrounding Chrome."""
    greeting_escaped = json.dumps(greeting, ensure_ascii=False)
    result = _parse_js_result(evaluate(target_id, f"""
    (() => {{
        const input = document.querySelector('#chat-input');
        if (!input) return JSON.stringify({{success: false, error: 'no_chat_input'}});

        let vue = null;
        let element = input;
        for (let index = 0; index < 15 && element; index += 1) {{
            if (element.__vue__) {{
                vue = element.__vue__;
                break;
            }}
            element = element.parentElement;
        }}
        if (!vue || typeof vue.handleSubmit !== 'function') {{
            return JSON.stringify({{success: false, error: 'legacy_submit_unavailable'}});
        }}

        input.innerText = {greeting_escaped};
        input.dispatchEvent(new InputEvent('input', {{
            bubbles: true,
            inputType: 'insertText',
            data: {greeting_escaped}
        }}));
        if (vue._data) vue._data.enableSubmit = true;
        vue.handleSubmit();
        return JSON.stringify({{success: true, action: 'chat_submitted_background'}});
    }})()
    """))
    if result.get("success"):
        return {"success": True, "action": "chat_submitted_background"}
    return result


def _fill_chat_input(target_id: str, greeting: str) -> dict:
    greeting_escaped = json.dumps(greeting, ensure_ascii=False)
    input_state = _parse_js_result(evaluate(target_id, """
    (() => {
        const input = document.querySelector('#chat-input');
        if (!input) return JSON.stringify({success: false, error: 'no_chat_input'});
        return JSON.stringify({success: true});
    })()
    """))
    if not input_state.get("success"):
        return input_state
    if not click_at(target_id, "#chat-input"):
        return {"success": False, "error": "chat_input_focus_failed"}
    if not press_key(target_id, "SelectAll") or not press_key(target_id, "Backspace"):
        return {"success": False, "error": "chat_input_clear_failed"}
    if not type_text(target_id, greeting, human=True):
        return {"success": False, "error": "trusted_input_failed"}

    result = _parse_js_result(evaluate(target_id, f"""
    (() => {{
        const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
        const input = document.querySelector('#chat-input');
        if (!input) return JSON.stringify({{success: false, error: 'no_chat_input'}});
        const sendButton = document.querySelector('.btn-send');
        const disabled = !sendButton || sendButton.disabled || sendButton.classList.contains('disabled');
        const matches = normalize(input.innerText || input.textContent) === normalize({greeting_escaped});
        return JSON.stringify({{
            success: matches,
            error: matches ? null : 'input_not_filled',
            send_button: !!sendButton,
            disabled
        }});
    }})()
    """))
    if result.get("success") and result.get("disabled"):
        if type_text(target_id, " ") and press_key(target_id, "Backspace"):
            result = _parse_js_result(evaluate(target_id, f"""
            (() => {{
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                const input = document.querySelector('#chat-input');
                const sendButton = document.querySelector('.btn-send');
                const disabled = !sendButton || sendButton.disabled || sendButton.classList.contains('disabled');
                const matches = !!input && normalize(input.innerText || input.textContent) === normalize({greeting_escaped});
                return JSON.stringify({{success: matches, error: matches ? null : 'input_not_filled', disabled}});
            }})()
            """))
    return result


# JS: 在岗位详情页点击"立即沟通"并发送招呼语
JS_SEND_GREETING = """
(async (greeting) => {
    // 找到"立即沟通"按钮
    const btn = document.querySelector('.btn-startchat, .op-btn-chat, [ka="job_detail_chat"]');
    if (!btn) return JSON.stringify({success: false, error: 'no_chat_button'});

    btn.click();
    await new Promise(r => setTimeout(r, 2000));

    // 等待聊天输入框出现
    const input = document.querySelector('.chat-input textarea, .chat-input [contenteditable], .input-area textarea');
    if (!input) return JSON.stringify({success: false, error: 'no_input_box'});

    // 输入招呼语
    if (input.tagName === 'TEXTAREA') {
        input.value = greeting;
        input.dispatchEvent(new Event('input', {bubbles: true}));
    } else {
        input.innerHTML = greeting;
        input.dispatchEvent(new Event('input', {bubbles: true}));
    }

    await new Promise(r => setTimeout(r, 500));

    // 点击发送
    const sendBtn = document.querySelector('.btn-send, .send-btn, [class*="send"]');
    if (sendBtn) {
        sendBtn.click();
        await new Promise(r => setTimeout(r, 1000));
        return JSON.stringify({success: true});
    }

    return JSON.stringify({success: false, error: 'no_send_button'});
})(arguments[0])
"""


def _send_greeting_once(
    job: dict,
    greeting: str,
    throttle_config: dict,
    phase_callback=None,
) -> tuple[dict, str | None]:
    # Serialize browser-level operations across parallel workbench tasks.
    # 发送是最高优先级浏览器操作（写岗位页/发消息最敏感），
    # 平台锁会让它在采集/监测之前插队。
    with platform_browser_lock(current_channel().lock_key).context(BrowserPriority.DELIVER):
        return _send_greeting_once_locked(job, greeting, throttle_config, phase_callback)


def _send_greeting_once_locked(
    job: dict,
    greeting: str,
    throttle_config: dict,
    phase_callback=None,
) -> tuple[dict, str | None]:
    stop_event = throttle_config.get("_workbench_stop_event")
    existing_target_ids = {
        str(target.get("targetId") or "")
        for target in get_page_targets()
        if target.get("targetId")
    }
    target_id = new_tab(job["url"], background=True)
    if not target_id:
        return {"success": False, "error": "open_page_failed", "history_detail": "无法打开页面", "skip_backoff": True}, None

    if _stop_requested(stop_event):
        close_tab(target_id)
        return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None

    browse_min = throttle_config.get("browse_duration_min", 15)
    browse_max = throttle_config.get("browse_duration_max", 30)
    if throttle_config.get("browse_before_greet", True):
        import random
        browse_time = random.uniform(browse_min, browse_max)
        if phase_callback:
            try:
                phase_callback(job, "browsing", {"browse_seconds": browse_time})
            except Exception:
                pass
        if _sleep_or_stop(browse_time, stop_event):
            close_tab(target_id)
            return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None
        if phase_callback:
            try:
                phase_callback(job, "browsed", {})
            except Exception:
                pass

    page_check_js = """
    (() => {
        const text = document.body ? document.body.innerText : '';
        const title = document.title || '';
        if (
            title.includes('访问的页面不存在') ||
            text.includes('您访问的页面不存在') ||
            text.includes('Oops!')
        ) {
            return JSON.stringify({
                success: false,
                error: 'job_page_unavailable',
                history_detail: '岗位页面不存在或已下架',
                skip_backoff: true
            });
        }
        return JSON.stringify({success: true});
    })()
    """
    page_check = _parse_js_result(evaluate(target_id, page_check_js))
    if not page_check.get("success"):
        close_tab(target_id)
        return page_check, None

    chat_button_attempts = int(throttle_config.get("_chat_button_attempts", 30))
    result1a = _click_chat_button(target_id, stop_event, chat_button_attempts)
    if not result1a.get("success"):
        close_tab(target_id)
        return {"success": False, "error": "no_chat_button", "history_detail": "无法找到沟通按钮", "skip_backoff": True}, None

    if _sleep_or_stop(4, stop_event):
        close_tab(target_id)
        return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None

    popup_result = _handle_greet_popup(target_id, greeting, result1a)
    if not popup_result.get("success"):
        return popup_result, target_id
    contact_action = str(popup_result.get("action") or "no_popup")

    navigation_attempts = int(throttle_config.get("_chat_navigation_attempts", 20))
    chat_ready = _wait_for_chat_page(
        target_id,
        stop_event,
        navigation_attempts,
        job,
        excluded_target_ids=existing_target_ids,
    )
    if chat_ready.get("success"):
        target_id = _adopt_chat_target(target_id, chat_ready)
    if chat_ready.get("error") == "stopped":
        return chat_ready, None
    if not chat_ready.get("success") and contact_action not in {
        "first_contact_submitted",
        "startchat_redirected",
    }:
        console.print("[yellow]    ! 沟通按钮未跳转聊天页，尝试真实点击兜底[/yellow]")
        if click_at(target_id, CHAT_BUTTON_SELECTOR):
            if _sleep_or_stop(1, stop_event):
                close_tab(target_id)
                return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None
            popup_result = _handle_greet_popup(target_id, greeting, result1a)
            if not popup_result.get("success"):
                return popup_result, target_id
            contact_action = str(popup_result.get("action") or "no_popup")
            chat_ready = _wait_for_chat_page(
                target_id,
                stop_event,
                navigation_attempts,
                job,
                excluded_target_ids=existing_target_ids,
            )
            if chat_ready.get("success"):
                target_id = _adopt_chat_target(target_id, chat_ready)
            if chat_ready.get("error") == "stopped":
                return chat_ready, None

    if not chat_ready.get("success"):
        if contact_action == "first_contact_submitted":
            return {
                "success": False,
                "error": "first_contact_navigation_unverified",
                "history_detail": "首次招呼语已经提交，但未能进入对应会话验证结果；为避免重复发送，请人工检查",
                "skip_backoff": True,
            }, target_id
        return {
            "success": False,
            "error": "no_chat_input",
            "history_detail": "发送失败: 未进入具体聊天会话，可能是BOSS继续沟通跳转失败",
            "skip_backoff": True,
        }, target_id

    verification_attempts = int(throttle_config.get("_send_verification_attempts", 20))
    if contact_action == "first_contact_submitted":
        for _ in range(max(1, verification_attempts)):
            if _sleep_or_stop(0.5, stop_event):
                close_tab(target_id)
                return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None
            delivery_state = _message_delivery_state(target_id, greeting)
            if delivery_state == "failed":
                return {
                    "success": False,
                    "error": "first_contact_send_rejected",
                    "history_detail": "首次沟通招呼语被标记为发送失败",
                    "skip_backoff": True,
                }, target_id
            if delivery_state == "delivered":
                if _sleep_or_stop(2, stop_event):
                    close_tab(target_id)
                    return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None
                if _message_delivery_state(target_id, greeting) == "delivered":
                    close_tab(target_id)
                    return {
                        "success": True,
                        "verified": True,
                        "first_contact": True,
                    }, None
                return {
                    "success": False,
                    "error": "first_contact_send_not_stable",
                    "history_detail": "首次招呼语曾出现但未稳定保留在会话中",
                    "skip_backoff": True,
                }, target_id
        if _submitted_message_looks_accepted(target_id, greeting):
            close_tab(target_id)
            return {
                "success": True,
                "accepted_without_echo": True,
                "first_contact": True,
            }, None
        return {
            "success": False,
            "error": "first_contact_delivery_unverified",
            "history_detail": "首次招呼语已提交，但会话中未确认对应消息；为避免重复发送，请人工检查",
            "skip_backoff": True,
        }, target_id

    existing_state = _message_delivery_state(target_id, greeting)
    if existing_state == "delivered":
        close_tab(target_id)
        return {"success": True, "already_present": True, "verified": True}, None
    if existing_state in {"pending", "failed"}:
        return {
            "success": False,
            "error": f"existing_message_{existing_state}",
            "history_detail": f"会话中已有状态为 {existing_state} 的相同消息，请人工检查",
            "skip_backoff": True,
        }, target_id

    submit_result = _submit_chat_message_background(target_id, greeting)
    if not submit_result.get("success"):
        input_result = _fill_chat_input(target_id, greeting)
        if not input_result.get("success"):
            return input_result, target_id
        if input_result.get("disabled"):
            return {
                "success": False,
                "error": "send_button_unavailable",
                "history_detail": "招呼语已填入，但发送按钮不可用，未标记成功",
                "skip_backoff": True,
            }, target_id
        if not click_at(target_id, ".btn-send:not(.disabled)"):
            return {
                "success": False,
                "error": "send_button_click_failed",
                "history_detail": "招呼语已填入，但发送按钮点击失败，未标记成功",
                "skip_backoff": True,
            }, target_id

    for _ in range(max(1, verification_attempts)):
        if _sleep_or_stop(0.5, stop_event):
            close_tab(target_id)
            return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None
        delivery_state = _message_delivery_state(target_id, greeting)
        if delivery_state == "failed":
            return {
                "success": False,
                "error": "send_rejected_after_click",
                "history_detail": "BOSS 将消息标记为发送失败，未记录为已发送",
                "skip_backoff": True,
            }, target_id
        if delivery_state == "delivered":
            if _sleep_or_stop(2, stop_event):
                close_tab(target_id)
                return {"success": False, "error": "stopped", "history_detail": "用户已请求停止", "skip_backoff": True}, None
            if _message_delivery_state(target_id, greeting) == "delivered":
                close_tab(target_id)
                return {"success": True, "verified": True}, None
            return {
                "success": False,
                "error": "send_not_stable",
                "history_detail": "消息曾出现但未稳定保留在会话中，未记录为已发送",
                "skip_backoff": True,
            }, target_id

    if _submitted_message_looks_accepted(target_id, greeting):
        close_tab(target_id)
        return {"success": True, "accepted_without_echo": True}, None

    return {
        "success": False,
        "error": "send_not_confirmed",
        "history_detail": "已点击发送，但会话中未确认对应招呼语，未记录为已发送",
        "skip_backoff": True,
    }, target_id




def _merge_channel_throttle(channel_key: str, throttle_config: dict) -> dict:
    """Global throttle + per-channel overrides (throttle.channel_overrides.<key>).

    Keys present in the channel override (except channel_overrides itself) replace
    the global values, so each platform can keep its own quota / interval / browsing
    profile while sharing the rest of the anti-ban policy.
    """
    merged = dict(throttle_config)
    merged.pop("channel_overrides", None)
    override = (throttle_config.get("channel_overrides") or {}).get(channel_key) or {}
    if override:
        for key, value in override.items():
            merged[key] = value
    return merged


def send_greetings(config: dict, force: bool = False) -> int:
    """Send generated greetings. Returns count of successfully sent."""
    set_active_channel(get_active_channel(config))
    db = get_db()
    throttle_config = config.get("throttle", {})
    stop_event = config.get("_workbench_stop_event")
    workbench_job_ids = {str(job_id) for job_id in config.get("_workbench_job_ids", [])}
    send_report = {
        "requested_count": len(workbench_job_ids),
        "eligible_count": 0,
        "scheduled_count": 0,
        "attempted_count": 0,
        "sent_count": 0,
        "failed_count": 0,
        "deferred_count": len(workbench_job_ids),
        "quota_deferred_count": 0,
        "stop_reason": None,
    }
    # Keep the integer return value for CLI/backward compatibility while giving
    # the web workflow enough detail to distinguish failures from quota deferrals.
    config["_workbench_send_report"] = send_report
    if isinstance(stop_event, Event):
        throttle_config = dict(throttle_config)
        throttle_config["_workbench_stop_event"] = stop_event

    # Anti-ban: random day off (可通过 --force 跳过)
    day_off_prob = throttle_config.get("day_off_probability", 0.05)
    if not force and should_take_day_off(day_off_prob):
        console.print("[yellow]🎲 今日随机休息（防检测），跳过发送[/yellow]")
        add_risk_event(db, "day_off", "随机休息日")
        send_report["stop_reason"] = "day_off"
        db.close()
        return 0

    # Anti-ban: send window check (可通过 --force 跳过)
    send_windows = throttle_config.get("send_windows", [])
    window_checker = SendWindowChecker(send_windows)
    if not force and not window_checker.is_active():
        info = window_checker.next_window_info()
        console.print("[yellow]⏰ 当前不在发送时间窗口内，暂不发送[/yellow]")
        console.print(f"[dim]  {info}[/dim]")
        add_risk_event(db, "outside_window", info)
        send_report["stop_reason"] = "outside_window"
        db.close()
        return 0

    jobs = get_jobs_ready_to_send(db)
    if workbench_job_ids:
        jobs = [job for job in jobs if str(job["id"]) in workbench_job_ids]
    send_report["eligible_count"] = len(jobs)
    send_report["deferred_count"] = len(workbench_job_ids) if workbench_job_ids else len(jobs)

    if not jobs:
        console.print("[yellow]没有已生成招呼语的待发送岗位，请先运行 jobwinner greet[/yellow]")
        send_report["stop_reason"] = "no_ready_jobs"
        db.close()
        return 0

    # Per-channel anti-ban split: bosszp / zhaopin get INDEPENDENT daily quota,
    # send interval and progressive backoff, so one platform being rate-limited
    # or busy never throttles the other. Config: throttle.channel_overrides.<key>.
    # daily_limit <= 0 (or None/"unlimited") means no daily cap — throttling is
    # then driven purely by the randomized interval + extra pauses below.
    daily_limit = throttle_config.get("daily_limit", 30)
    interval_min = throttle_config.get("interval_min", 60)
    interval_max = throttle_config.get("interval_max", 180)
    extra_pause_probability = float(throttle_config.get("extra_pause_probability", 0.05))
    extra_pause_min = float(throttle_config.get("extra_pause_min", 2.0))
    extra_pause_max = float(throttle_config.get("extra_pause_max", 30.0))

    def _today_sent_for_channel(channel_key: str) -> int:
        # 历史岗位的 channel 可能为空/旧数据，一律计入默认主渠道（bosszp）。
        # 注意：数据库存 UTC，本地 UTC+8 的凌晨 0-8 点会落在 UTC 的"昨天"，
        # 用 date('now') 会把当天凌晨发的算成昨天，导致配额/今日已发虚低。
        # 这里统一用 localtime 口径（与 get_funnel_stats 的今日口径一致）。
        if channel_key in ("", "bosszp"):
            row = db.execute(
                "SELECT COUNT(*) as cnt FROM history h JOIN jobs j ON h.job_id=j.id "
                "WHERE h.action='sent' AND date(h.created_at,'localtime')=date('now','localtime') "
                "AND (j.channel IS NULL OR j.channel='' OR j.channel='bosszp')"
            ).fetchone()
        else:
            row = db.execute(
                "SELECT COUNT(*) as cnt FROM history h JOIN jobs j ON h.job_id=j.id "
                "WHERE h.action='sent' AND date(h.created_at,'localtime')=date('now','localtime') "
                "AND j.channel=?", (channel_key,)
            ).fetchone()
        return row["cnt"] if row else 0


    channel_states: dict[str, dict] = {}
    jobs_to_send = []
    quota_deferred = 0
    for job in jobs:
        ch = str(job.get("channel") or "bosszp")
        st = channel_states.get(ch)
        if st is None:
            tc = _merge_channel_throttle(ch, throttle_config)
            raw_limit = tc.get("daily_limit", daily_limit)
            # 0 / None / "unlimited" → 无每日上限
            limit = None if (raw_limit is None or str(raw_limit).strip().lower() in ("0", "", "unlimited", "none")) else int(raw_limit)
            st = {
                "key": ch,
                "limit": limit,
                "sent_today": _today_sent_for_channel(ch),
                "interval_min": float(tc.get("interval_min", interval_min)),
                "interval_max": float(tc.get("interval_max", interval_max)),
                "extra_pause_probability": float(tc.get("extra_pause_probability", extra_pause_probability)),
                "extra_pause_min": float(tc.get("extra_pause_min", extra_pause_min)),
                "extra_pause_max": float(tc.get("extra_pause_max", extra_pause_max)),
                "throttle": None,
                "backoff": None,
                "interval_used": False,
                "risk_paused": False,
            }
            channel_states[ch] = st
        if st["limit"] is None:
            jobs_to_send.append(job)
            st.setdefault("scheduled", []).append(job)
            continue
        remaining = st["limit"] - st["sent_today"] - len(st.setdefault("scheduled", []))
        if remaining <= 0:
            quota_deferred += 1
            continue
        st["scheduled"].append(job)
        jobs_to_send.append(job)

    if not jobs_to_send:
        console.print("[yellow]今日已达发送上限（按渠道分别计算）[/yellow]")
        send_report["quota_deferred_count"] = max(quota_deferred, len(jobs))
        send_report["stop_reason"] = "daily_limit"
        db.close()
        return 0

    send_report["scheduled_count"] = len(jobs_to_send)
    send_report["quota_deferred_count"] = quota_deferred
    if quota_deferred:
        send_report["stop_reason"] = "daily_limit"

    def _state_throttle(st: dict) -> RequestThrottle:
        if st["throttle"] is None:
            st["throttle"] = RequestThrottle(
                delay_min=st["interval_min"],
                delay_max=st["interval_max"],
                extra_pause_probability=st["extra_pause_probability"],
                extra_pause_min=st["extra_pause_min"],
                extra_pause_max=st["extra_pause_max"],
            )
        return st["throttle"]

    def _state_backoff(st: dict) -> ProgressiveBackoff:
        if st["backoff"] is None:
            st["backoff"] = ProgressiveBackoff()
        return st["backoff"]

    sent_count = 0
    progress_callback = config.get("_workbench_send_progress")

    def report_send_progress(job: dict | None = None, phase: str = "sending") -> None:
        """Push live send progress to the workbench task (frontend status card)."""
        if not callable(progress_callback):
            return
        try:
            progress_callback({
                "done": sent_count + send_report.get("failed_count", 0),
                "sent": sent_count,
                "failed": send_report.get("failed_count", 0),
                "total": len(jobs_to_send),
                "phase": phase,
                "current_company": (job or {}).get("company", ""),
                "current_title": (job or {}).get("title", ""),
            })
        except Exception:
            # Progress reporting must never break the send loop
            pass

    quota_note = " / ".join(
        f"{v['key']} {v['sent_today']}/{'不限' if v['limit'] is None else v['limit']}"
        for v in channel_states.values()
    )
    console.print(f"[bold]准备发送 {len(jobs_to_send)} 条招呼语[/bold] (今日已发 {quota_note})")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console
    ) as progress:
        task = progress.add_task("发送中", total=len(jobs_to_send))

        for job in jobs_to_send:
            if _stop_requested(stop_event):
                console.print("[yellow]已请求停止，结束发送[/yellow]")
                send_report["stop_reason"] = "stopped"
                break

            greeting = job.get("greeting", "")
            if not greeting:
                update_job_status(db, job["id"], "error")
                send_report["attempted_count"] += 1
                send_report["failed_count"] += 1
                progress.update(task, advance=1)
                continue

            ch = str(job.get("channel") or "bosszp")
            st = channel_states[ch]
            if st["risk_paused"]:
                # 该渠道前方触发风控/连续错误暂停：跳过本岗位，其它渠道继续。
                continue

            # Wait between sends per channel (except the first send of this channel)
            if st["interval_used"]:
                progress.update(task, description="等待间隔...")
                if _state_throttle(st).wait(stop_event):
                    console.print("[yellow]已请求停止，结束发送[/yellow]")
                    send_report["stop_reason"] = "stopped"
                    break
            st["interval_used"] = True

            progress.update(task, description=f"发送: {job['company'][:10]} - {job['title'][:15]}")
            report_send_progress(job, "opening")

            def job_phase_cb(job_info: dict, phase: str, extra: dict | None = None) -> None:
                # 浏览中 → 前端横幅显示“模拟浏览中”；浏览完进入输入发送
                if phase == "browsing":
                    report_send_progress(job_info, "browsing")
                elif phase == "browsed":
                    report_send_progress(job_info, "sending")

            if (job.get("channel") or "bosszp") == "zhaopin":
                result_data, failed_target_id = _send_zhaopin_greeting_once(
                    job, greeting, throttle_config,
                    resume_keyword=config.get("channels", {}).get("zhaopin", {}).get("resume", "在线"),
                    phase_callback=job_phase_cb,
                )
            else:
                result_data, failed_target_id = _send_greeting_once(
                    job, greeting, throttle_config,
                    phase_callback=job_phase_cb,
                )
            if result_data.get("error") == "stopped":
                send_report["stop_reason"] = "stopped"
                break
            if result_data.get("error") == "no_chat_input" and failed_target_id:
                console.print("[yellow]    ! 未进入具体聊天会话，重新打开岗位页再试一次[/yellow]")
                close_tab(failed_target_id)
                result_data, failed_target_id = _send_greeting_once(
                    job, greeting, throttle_config,
                    phase_callback=job_phase_cb,
                )
                if result_data.get("error") == "stopped":
                    send_report["stop_reason"] = "stopped"
                    break

            send_report["attempted_count"] += 1

            if not result_data.get("success"):
                console.print(f"[yellow]    ! 发送失败，已记录并关闭任务页面: {result_data.get('error', 'unknown')}[/yellow]")
                if failed_target_id:
                    close_tab(failed_target_id)
                    failed_target_id = None

            if result_data.get("success"):
                _state_throttle(st).mark()
                update_job_status(db, job["id"], "sent")
                add_history(db, job["id"], "sent", greeting[:50])
                sent_count += 1
                send_report["sent_count"] = sent_count
                _state_backoff(st).record_success()
            else:
                error = result_data.get("error", "unknown")
                send_report["failed_count"] += 1
                skip_backoff = bool(result_data.get("skip_backoff"))
                # 自动重试开关（默认关闭）：非风控、非结构性错误时，把岗位
                # 放回待发送队列（status=approved，greeting 保留），下轮发送
                # 循环会再试一次。风控信号与结构性错误绝不自动重发（安全）。
                auto_retry = bool(throttle_config.get("auto_retry_failed", False))
                is_risk_signal = error in ("captcha", "rate_limit", "blocked")
                if auto_retry and not is_risk_signal and not skip_backoff:
                    update_job_status(db, job["id"], "approved")
                    add_history(db, job["id"], "retry", f"发送失败({error})已自动放回待发送队列，下次运行重试")
                else:
                    update_job_status(db, job["id"], "error")
                    add_history(db, job["id"], "error", result_data.get("history_detail", f"发送失败: {error}"))
                if skip_backoff:
                    progress.update(task, advance=1)
                    continue

                _state_throttle(st).mark()

                # 风控信号：只暂停该渠道（后续本渠道岗位跳过），其它渠道继续。
                if is_risk_signal:
                    console.print(f"\n[red]⚠ 检测到风控信号: {error}，暂停 {st['key']} 渠道（其余渠道继续）[/red]")
                    add_risk_event(db, error, f"触发风控: {error} ({st['key']})")
                    st["risk_paused"] = True
                    progress.update(task, advance=1)
                    continue

                # Progressive backoff on errors (per channel)
                backoff = _state_backoff(st)
                pause_duration = backoff.record_error()
                add_risk_event(db, "send_error", f"{error} (连续{backoff._consecutive_errors}次, {st['key']})")

                # Too many consecutive errors: pause this channel only
                if backoff.should_pause_long:
                    console.print(f"\n[red]⚠ {st['key']} 连续错误过多，暂停该渠道 {int(pause_duration/60)} 分钟[/red]")
                    add_risk_event(db, "backoff_pause", f"{st['key']} 暂停{int(pause_duration)}秒")
                    st["risk_paused"] = True
                    progress.update(task, advance=1)
                    continue
                elif pause_duration > 0:
                    console.print(f"\n[yellow]  错误退避: 额外等待 {int(pause_duration)}秒[/yellow]")
                    if _sleep_or_stop(pause_duration, stop_event):
                        send_report["stop_reason"] = "stopped"
                        break

            progress.update(task, advance=1)

    console.print(f"\n[green]✓ 成功发送 {sent_count} 条[/green]")
    report_total = len(workbench_job_ids) if workbench_job_ids else len(jobs)
    send_report["sent_count"] = sent_count
    send_report["deferred_count"] = max(
        report_total - sent_count - send_report["failed_count"],
        0,
    )
    db.close()
    return sent_count
