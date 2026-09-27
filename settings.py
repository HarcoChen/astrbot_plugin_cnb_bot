"""Plugin settings with legacy-key fallback, plus user-facing text helpers."""

from __future__ import annotations

from typing import Any

DEFAULT_ASSISTANT_NAME = "分析助手"


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _migrated(
    config: dict[str, Any],
    key: str,
    default: float,
    legacy_key: str,
    legacy_default: float,
    legacy_per_unit: float,
) -> float:
    """Read a setting whose unit changed.

    The legacy key stays in the schema as an invisible item, so AstrBot keeps a
    value customized before the rename. It wins only while the new key is still
    at its default, so any explicit edit of the new key takes precedence.
    """
    value = _number(config.get(key))
    legacy = _number(config.get(legacy_key))
    if (value is None or value == default) and legacy is not None and legacy != legacy_default:
        return legacy / legacy_per_unit
    return default if value is None else value


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def log_wait_seconds(config: dict[str, Any]) -> int:
    minutes = _migrated(config, "log_wait_minutes", 10, "log_wait_seconds", 600, 60)
    return int(_clamp(minutes, 1, 24 * 60) * 60)


def analysis_wait_seconds(config: dict[str, Any]) -> int:
    minutes = _migrated(config, "analysis_wait_minutes", 20, "analysis_wait_seconds", 1200, 60)
    return int(_clamp(minutes, 1, 24 * 60) * 60)


def max_log_file_bytes(config: dict[str, Any]) -> int:
    mib = _migrated(
        config, "max_log_file_mib", 20, "max_archive_bytes", 20 * 1024 * 1024, 1024 * 1024
    )
    return int(_clamp(mib, 0.01, 1024) * 1024 * 1024)


def recovery_confirm_seconds(config: dict[str, Any]) -> int:
    minutes = _number(config.get("recovery_confirm_minutes"))
    return int(_clamp(30 if minutes is None else minutes, 1, 7 * 24 * 60) * 60)


def assistant_name(config: dict[str, Any]) -> str:
    return str(config.get("assistant_name") or "").strip() or DEFAULT_ASSISTANT_NAME


def format_bytes(size: Any) -> str:
    value = _number(size) or 0
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if value >= scale:
            text = f"{value / scale:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
    return f"{int(value)} B"


def format_duration(seconds: Any) -> str:
    total = int(_number(seconds) or 0)
    if total < 60:
        return "不到 1 分钟"
    minutes = (total + 30) // 60
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分钟"
