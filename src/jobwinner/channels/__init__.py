"""Channel adapters for JobWinner multi-channel delivery.

Each recruitment platform gets a ``ChannelAdapter`` subclass providing the
platform-specific bits (search URL, DOM extraction scripts, chat entry,
login detection) while the core pipeline stays channel-agnostic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jobwinner.channels.base import ChannelAdapter

if TYPE_CHECKING:
    from typing import Any

_REGISTRY: dict[str, type[ChannelAdapter]] = {}
"""Lazy registry: key -> adapter class. Populated on first import of
``jobwinner.channels.bosszp`` etc. so importing the package never pulls in
heavy per-channel dependencies unless a channel is actually used."""

_ACTIVE: ChannelAdapter | None = None
"""Process-wide active channel cache. Set by :func:`set_active_channel` so
deep call chains (sender/monitor internals without config access) can resolve
the channel without threading a config parameter through every function."""


def register_channel(adapter_cls: type[ChannelAdapter]) -> type[ChannelAdapter]:
    """Register a channel adapter class under ``adapter_cls.key``."""
    _REGISTRY[adapter_cls.key] = adapter_cls
    return adapter_cls


def available_channels() -> list[str]:
    """Return sorted list of registered channel keys."""
    _ensure_registry_loaded()
    return sorted(_REGISTRY)


def get_channel(key: str, config: dict[str, Any] | None = None) -> ChannelAdapter:
    """Return a configured channel adapter instance.

    Falls back to the built-in ``bosszp`` channel when ``key`` is unknown so
    existing configs and tests keep working unchanged.
    """
    _ensure_registry_loaded()
    cls = _REGISTRY.get(key) or _REGISTRY.get("bosszp")
    if cls is None:  # pragma: no cover - bosszp is always registered
        raise ValueError("no channels registered; import jobwinner.channels.bosszp")
    return cls(config or {})


def get_active_channel(config: dict[str, Any]) -> ChannelAdapter:
    """Return the adapter for the configured active channel (default bosszp).

    Also caches it process-wide so later ``current_channel()`` calls from deep
    call chains (sender/monitor internals without config access) return the
    same instance.
    """
    global _ACTIVE
    channels_cfg = config.get("channels", {})
    if not isinstance(channels_cfg, dict):
        channels_cfg = {}
    active = channels_cfg.get("active", "bosszp") or "bosszp"
    _ACTIVE = get_channel(str(active), config)
    return _ACTIVE


def set_active_channel(channel: ChannelAdapter) -> ChannelAdapter:
    """Set the process-wide active channel used by deep call chains."""
    global _ACTIVE
    _ACTIVE = channel
    return channel


def current_channel() -> ChannelAdapter:
    """Return the process-wide active channel (falls back to bosszp)."""
    global _ACTIVE
    if _ACTIVE is not None:
        return _ACTIVE
    channel = get_channel("bosszp")
    _ACTIVE = channel
    return channel


def _ensure_registry_loaded() -> None:
    """Import built-in channel modules so they self-register."""
    if "bosszp" not in _REGISTRY:
        from jobwinner.channels import bosszp  # noqa: F401  (side-effect import)

        assert bosszp  # keep linters quiet about the unused import


__all__ = [
    "ChannelAdapter",
    "register_channel",
    "available_channels",
    "get_channel",
    "get_active_channel",
    "set_active_channel",
    "current_channel",
]
