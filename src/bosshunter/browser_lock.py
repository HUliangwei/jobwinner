
"""Shared serialization lock for browser/CDP access across workbench tasks.

Collect, deliver and monitor all drive the same Chrome instance through the
9222 debug port. When tasks run in parallel (deliver while collecting), a
global lock prevents concurrent tab/page mutations that could confuse page
state or trip anti-bot signals. Task code acquires this lock around its
browser-heavy loops.
"""

from __future__ import annotations

from threading import RLock

BROWSER_LOCK = RLock()
