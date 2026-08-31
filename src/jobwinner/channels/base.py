"""Channel adapter abstract base class.

A :class:`ChannelAdapter` encapsulates everything that is platform-specific
about a recruitment channel:

* identity (``key``, ``domain``, display name)
* search page construction
* DOM extraction scripts for list & detail pages
* chat entry / greeting delivery
* login detection
* platform browser lock name and risk-control (throttle) policy

The core pipeline (scoring, greeting generation, state machine, DB layer)
never touches channel specifics and is shared by every channel.
"""

from __future__ import annotations

import abc
from typing import Any


class ChannelAdapter(abc.ABC):
    """Abstract base class for job platform channel adapters."""

    # ------------------------------------------------------------------
    # Identity (must be overridden by every concrete channel)
    # ------------------------------------------------------------------

    #: Unique machine key, e.g. "bosszp", "liepin".
    key: str = ""
    #: Display name shown in logs / web UI.
    label: str = ""
    #: Primary domain used for login-state detection, e.g. "zhipin.com".
    domain: str = ""
    #: Base origin for absolute URL joins, e.g. "https://www.zhipin.com".
    #: Defaults to ``https://{domain}``; channels that need a www prefix override.
    base_url: str = ""
    #: Browser-lock platform name (shared with browser_lock.py).
    lock_key: str = ""
    #: Search URL template. ``{keyword}`` and ``{city_code}`` are substituted.
    search_url_template: str = ""

    #: JS snippet evaluated on a search-list page -> JSON array of card items.
    js_extract_list: str = ""
    #: JS snippet evaluated on a job-detail page -> JSON object of full info.
    js_extract_detail: str = ""
    #: Default monitor chat URL (may be overridden by config ``monitor.chat_url``).
    default_chat_url: str = ""

    #: Optional channel-specific city code map (name -> platform code).
    #: Consulted before ``config.search.city_codes`` so switching channels never
    #: reuses another channel's stale city codes from the global config.
    city_codes: dict[str, str] = {}

    #: Whether this channel can actually deliver greetings today. Channels that
    #: are collect-only (search/scrape/score work, sending not yet wired) set
    #: this to False so the sender skips them instead of clicking blind.
    supports_send: bool = True

    #: Hard cap on how many search-result pages the scraper will open for this
    #: channel (raised against config ``search.max_pages``). Platforms whose new
    #: SPA ignores ``?page=N`` set this to 1 to avoid burning tabs on no-op page
    #: opens (job ids are still deduped, so extra pages are merely wasteful).
    pages_cap: int = 10

    #: Whether scoring needs a separate detail-page fetch. Channels whose list
    #: page payload already carries full details (JD / real salary / company)
    #: set this to False so collection skips opening one detail tab per job.
    detail_required: bool = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    # ------------------------------------------------------------------
    # URL helpers (concrete channels may override)
    # ------------------------------------------------------------------

    def build_search_url(self, keyword: str, city_code: str, page: int = 1, sort: str = "") -> str:
        """Build a search page URL for this channel."""
        from urllib.parse import quote

        url = self.search_url_template.format(
            keyword=quote(keyword), city_code=city_code
        )
        if sort == "newest":
            url = self._append_query(url, "sortType=2")
        if page > 1:
            url = self._append_query(url, f"page={page}")
        return url

    def build_job_url(self, job: dict[str, Any]) -> str:
        """Build the full job-detail URL from a list-card item.

        The default joins ``job["url"]`` (a path like ``/job_detail/xxx.html``)
        against :attr:`base_url` (falls back to ``https://{domain}``).
        Channels with absolute URLs override this.
        """
        url = str(job.get("url") or "")
        if url.startswith(("http://", "https://")):
            return url
        base = self.base_url or f"https://{self.domain}"
        return f"{base}{url}"

    def build_chat_url(self, job: dict[str, Any] | None = None) -> str:
        """Return the chat page URL for this channel."""
        monitor_cfg = self.config.get("monitor", {})
        if isinstance(monitor_cfg, dict) and monitor_cfg.get("chat_url"):
            return str(monitor_cfg["chat_url"])
        return self.default_chat_url

    def resolve_city_code(self, city: str, custom_codes: dict[str, str] | None = None) -> str | None:
        """Resolve a city name to this channel's platform code.

        Channel-specific :attr:`city_codes` win over ``custom_codes`` (the
        legacy global ``search.city_codes`` from BOSS直聘) so switching the
        active channel never breaks city resolution with stale codes.
        """
        if self.city_codes and self.city_codes.get(city):
            return str(self.city_codes[city])
        if custom_codes and isinstance(custom_codes, dict) and custom_codes.get(city):
            return str(custom_codes[city])
        return None

    def generate_job_id(self, url: str) -> str:
        """Generate a stable, unique id for a job URL.

        Default extracts the BOSS直聘 position id from ``/job_detail/xxx.html``
        URLs and falls back to an md5 of the URL; channels with other URL
        layouts override the path pattern.
        """
        import hashlib
        import re as _re

        match = _re.search(r'/job_detail/([^.]+)', url or "")
        if match:
            return match.group(1)
        return hashlib.md5((url or "").encode()).hexdigest()[:16]

    # ------------------------------------------------------------------
    # Login / diagnostics
    # ------------------------------------------------------------------

    def is_own_page(self, url: str) -> bool:
        """Return True when ``url`` belongs to this channel's platform."""
        url = (url or "").lower()
        return self.domain.lower() in url

    def detect_login(self, page_state: dict[str, Any]) -> bool:
        """Return True when the page indicates a logged-in session.

        The abstract default is conservative (True) so channels that do not
        implement state-based detection never block delivery accidentally.
        Concrete channels inspect cookie names / DOM markers.
        """
        return True

    # ------------------------------------------------------------------
    # Risk control
    # ------------------------------------------------------------------

    def throttle_policy(self) -> dict[str, Any]:
        """Return platform-specific throttle tweaks (empty = use defaults)."""
        return {}

    # ------------------------------------------------------------------
    # Optional lifecycle hooks (default no-ops)
    # ------------------------------------------------------------------

    def on_collect(self, target_id: str) -> None:
        """Hook called after a search page is opened (may wait / dismiss popups)."""

    def on_detail_open(self, target_id: str) -> None:
        """Hook called after a detail page is opened."""

    def on_send(self, job: dict[str, Any]) -> None:
        """Hook called before delivering a greeting for a job."""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _append_query(url: str, query: str) -> str:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{query}"
