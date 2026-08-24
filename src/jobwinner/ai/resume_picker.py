"""Resume selection helper: pick the best-matching base resume for a job.

Supports multiple resumes (profile.resume_paths). Falls back to the single
legacy profile.resume_path when resume_paths is not configured.

Strategy (cost-free, no AI call):
1. Rule-based keyword prefilter: match resume filename stem against the job's
   title/company. E.g. "resume_embodied" matches a 具身智能 job, while
   "resume_ic" matches a 模拟IC/芯片 job.
2. If exactly one resume matched -> use it. If several match -> the first in
   configured order wins.
3. If none matched -> fall back to the default (first existing) resume.

Optional AI selection (config ai.enable_resume_selection) is NOT implemented
here by default to keep scoring to a single AI call; the rule prefilter covers
the dual-track (embodied vs IC) use case well.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Direction keywords extracted from common Chinese job-track vocabulary.
_DIRECTION_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("ic", ("芯片", "ic", "模拟", "集成电路", "版图", "spice", "cadence", "半导体", "cis", "cmos")),
    ("embodied", ("具身", "机器人", "机械臂", "灵巧手", "ros", "slam", "视觉", "感知", "自动驾驶", "无人机", "嵌入式")),
]


def _normalize(text: str) -> str:
    return (text or "").lower()


def _resume_direction(stem: str) -> str:
    """Guess a resume's direction tag by its filename stem keywords."""
    s = _normalize(stem)
    for tag, keywords in _DIRECTION_RULES:
        for kw in keywords:
            if _normalize(kw) in s:
                return tag
    return ""


def _job_direction(text: str) -> str:
    """Guess a job's direction by scanning its title/company text."""
    s = _normalize(text)
    for tag, keywords in _DIRECTION_RULES:
        for kw in keywords:
            if kw in s:
                return tag
    return ""


def _resume_paths(config: dict[str, Any]) -> list[Path]:
    """Resolve the ordered list of base resume paths from config."""
    profile = config.get("profile", {}) if isinstance(config.get("profile"), dict) else {}
    paths: list[Path] = []
    raw = profile.get("resume_paths", [])
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item.strip():
                paths.append(Path(item.strip()))
    legacy = profile.get("resume_path", "./resume.md")
    if legacy and not paths:
        paths.append(Path(str(legacy)))
    # Dedupe preserving order and keep only non-empty
    seen: set[str] = set()
    result: list[Path] = []
    for p in paths:
        key = str(p)
        if key and key not in seen:
            seen.add(key)
            result.append(p)
    return result


def select_resume_path_for_job(job: dict[str, Any], config: dict[str, Any]) -> Path | None:
    """Return the best-matching base resume path for a job, or None if none exist.

    job: dict with at least 'title' and 'company' keys.
    """
    paths = _resume_paths(config)
    if not paths:
        return None

    job_text = " ".join(
        filter(None, [str(job.get("title") or ""), str(job.get("company") or "")])
    )
    job_dir = _job_direction(job_text)

    if job_dir:
        # Prefer resumes whose direction tag matches the job direction;
        # order by (match, configured order) with default order as tiebreak.
        def order_key(p: Path) -> tuple[int, int]:
            tag = _resume_direction(p.stem)
            match = 1 if tag == job_dir else 0
            return (match, -_order_index(p, paths))

        candidates = sorted(paths, key=order_key, reverse=True)
        # Best candidate = first with match=1; else fall back to default order
        best = next((p for p in candidates if _resume_direction(p.stem) == job_dir), None)
        if best:
            return best
        # No direction match: use first existing resume (default)
        return next((p for p in paths if p.exists()), paths[0])

    # No job direction keyword: default resume (first existing)
    return next((p for p in paths if p.exists()), paths[0])


def _order_index(p: Path, paths: list[Path]) -> int:
    """Return the configured position of path p (earlier = larger value for sort)."""
    for i, other in enumerate(paths):
        if str(other) == str(p):
            return i
    return len(paths)


def resume_paths(config: dict[str, Any]) -> list[Path]:
    """Public helper returning the resolved resume paths."""
    return _resume_paths(config)
