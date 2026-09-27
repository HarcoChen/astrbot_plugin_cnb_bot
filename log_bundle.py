"""Constrained ZIP inspection and text-log preparation."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath


class BundleError(ValueError):
    pass


_SECRET_KEY_VALUE_PATTERN = re.compile(
    r"(?im)([\"']?\b(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|client[_-]?secret)\b[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)

_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    _SECRET_KEY_VALUE_PATTERN,
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----"),
]

_TEXT_EXTENSIONS = {
    ".log",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".ini",
    ".cfg",
    ".conf",
    ".trace",
    ".out",
    ".err",
}


@dataclass(frozen=True)
class BundleLimits:
    max_archive_entries: int = 100
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 10 * 1024 * 1024
    max_compression_ratio: int = 100


@dataclass(frozen=True)
class BundleSummary:
    archive_sha256: str
    archive_bytes: int
    text_files: int
    skipped_files: int
    extracted_bytes: int
    key_log_excerpt: str


def redact_text(text: str) -> str:
    cleaned = text
    for pattern in _SECRET_PATTERNS:
        if pattern is _SECRET_KEY_VALUE_PATTERN:
            cleaned = pattern.sub(lambda match: match.group(1) + '"[已脱敏]"', cleaned)
        elif pattern.groups >= 2:
            cleaned = pattern.sub(r"\1[已脱敏]", cleaned)
        else:
            cleaned = pattern.sub("[已脱敏]", cleaned)
    return cleaned


def _validate_member_name(name: str) -> None:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    win_path = PureWindowsPath(name)
    if (
        "\x00" in name
        or normalized.startswith("/")
        or win_path.is_absolute()
        or bool(win_path.drive)
        or any(part in {"..", ""} for part in path.parts)
    ):
        raise BundleError("压缩包包含路径穿越或绝对路径条目，已拒绝处理。")


def _decode_text(data: bytes) -> str | None:
    if b"\x00" in data[:4096]:
        try:
            text = data.decode("utf-16")
        except UnicodeError:
            return None
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            try:
                text = data.decode("gb18030")
            except UnicodeError:
                return None
    if not text:
        return ""
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\n\r\t\f")
    if controls / max(1, len(text)) > 0.02:
        return None
    return text


def _key_excerpt(text: str, limit: int = 8000) -> str:
    candidates = []
    pattern = re.compile(r"(?i)(error|exception|traceback|fatal|panic|caused by|failed|失败|异常|错误)")
    for line in text.splitlines():
        if pattern.search(line):
            candidates.append(line)
            if sum(len(item) + 1 for item in candidates) >= limit:
                break
    excerpt = "\n".join(candidates)
    if not excerpt:
        excerpt = "\n".join(text.splitlines()[-80:])
    return excerpt[:limit]


def prepare_zip(
    archive_path: str,
    output_path: str,
    archive_bytes: int,
    limits: BundleLimits,
) -> BundleSummary:
    if archive_bytes <= 0:
        raise BundleError("压缩包为空。")
    digest = hashlib.sha256()
    with open(archive_path, "rb") as archive_stream:
        for chunk in iter(lambda: archive_stream.read(1024 * 1024), b""):
            digest.update(chunk)

    text_blocks: list[str] = []
    skipped = 0
    expanded_total = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > limits.max_archive_entries:
                raise BundleError(
                    f"压缩包条目过多（{len(members)}），上限为 {limits.max_archive_entries}。"
                )

            file_number = 0
            for member in members:
                _validate_member_name(member.filename)
                if member.is_dir():
                    continue
                mode = (member.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise BundleError("压缩包包含符号链接条目，已拒绝处理。")
                if member.flag_bits & 0x1:
                    raise BundleError("压缩包包含加密文件，无法安全读取。")
                if member.file_size < 0 or member.file_size > limits.max_file_bytes:
                    raise BundleError(
                        f"压缩包内单个文件超过限制（{limits.max_file_bytes} 字节）。"
                    )
                if member.file_size:
                    if member.compress_size <= 0:
                        raise BundleError("压缩包存在异常的压缩比。")
                    ratio = member.file_size / member.compress_size
                    if ratio > limits.max_compression_ratio:
                        raise BundleError(
                            f"压缩包压缩比过高（>{limits.max_compression_ratio}:1）。"
                        )
                expanded_total += member.file_size
                if expanded_total > limits.max_total_bytes:
                    raise BundleError(
                        f"压缩包累计解压体积超过限制（{limits.max_total_bytes} 字节）。"
                    )

                suffix = PurePosixPath(member.filename.replace("\\", "/")).suffix.lower()
                if suffix not in _TEXT_EXTENSIONS:
                    skipped += 1
                    continue
                with archive.open(member, "r") as stream:
                    raw = stream.read(limits.max_file_bytes + 1)
                if len(raw) > limits.max_file_bytes or len(raw) != member.file_size:
                    raise BundleError("压缩包内文件大小与目录信息不一致。")
                decoded = _decode_text(raw)
                if decoded is None:
                    skipped += 1
                    continue
                file_number += 1
                redacted = redact_text(decoded)
                text_blocks.append(f"===== 日志文件 {file_number}（{suffix}）=====\n{redacted}")
    except BundleError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise BundleError("ZIP 文件损坏，或包含不支持的压缩算法。") from exc

    if not text_blocks:
        raise BundleError("压缩包中没有可读取的文本日志文件。")
    content = "\n\n".join(text_blocks)
    encoded = content.encode("utf-8")
    if len(encoded) > limits.max_total_bytes:
        raise BundleError("脱敏后的文本日志超过累计体积限制。")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "x", encoding="utf-8", newline="\n") as output:
        output.write(content)
    return BundleSummary(
        archive_sha256=digest.hexdigest(),
        archive_bytes=archive_bytes,
        text_files=len(text_blocks),
        skipped_files=skipped,
        extracted_bytes=len(encoded),
        key_log_excerpt=_key_excerpt(content),
    )
