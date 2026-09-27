"""Safe staging of AstrBot File message components."""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import socket
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .settings import format_bytes

log = logging.getLogger("astrbot_plugin_cnb_bot")


class FileInputError(ValueError):
    pass


def _validate_download_url(url: str, allowed_hosts: list[str]) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"https", "http"}:
        raise FileInputError("附件下载链接协议不受支持。")
    if not parsed.hostname or parsed.username or parsed.password:
        raise FileInputError("附件下载链接无效。")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise FileInputError("拒绝访问本机或本地域名附件地址。")

    if allowed_hosts and not any(
        host == item.lower().strip(".")
        or host.endswith("." + item.lower().strip("."))
        for item in allowed_hosts
        if item.strip()
    ):
        raise FileInputError("附件下载域名不在配置的白名单中。")

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise FileInputError("无法解析附件下载域名。") from exc
    if not addresses:
        raise FileInputError("附件下载域名没有可用地址。")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise FileInputError("附件下载域名解析结果无效。") from exc
        if not ip.is_global:
            raise FileInputError("拒绝访问内网或保留地址附件链接。")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: list[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_download_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _copy_limited(
    source,
    target: Path,
    max_bytes: int,
    deadline: float | None = None,
) -> int:
    total = 0
    with target.open("xb") as output:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise FileInputError("下载日志文件超时，请重新上传。")
            chunk = source.read(min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise FileInputError("下载日志文件超时，请重新上传。")
            total += len(chunk)
            if total > max_bytes:
                raise FileInputError(f"日志文件超过大小上限（{format_bytes(max_bytes)}），请压缩或只保留相关日志后重新上传。")
            output.write(chunk)
    return total


def _download_limited(url: str, target: Path, max_bytes: int, allowed_hosts: list[str]) -> int:
    deadline = time.monotonic() + 120
    _validate_download_url(url, allowed_hosts)
    opener = urllib.request.build_opener(_SafeRedirectHandler(allowed_hosts))
    request = urllib.request.Request(url, headers={"User-Agent": "AstrBot-CNB-Report/0.1"})
    try:
        with opener.open(request, timeout=20) as response:
            final_url = response.geturl()
            _validate_download_url(final_url, allowed_hosts)
            header_size = response.headers.get("Content-Length")
            if header_size and int(header_size) > max_bytes:
                raise FileInputError(f"日志文件超过大小上限（{format_bytes(max_bytes)}），请压缩或只保留相关日志后重新上传。")
            return _copy_limited(response, target, max_bytes, deadline)
    except FileInputError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise FileInputError("下载日志文件失败，文件链接可能已过期，请重新上传。") from exc


def _component_name(component) -> str:
    name = str(getattr(component, "name", "") or "")
    return Path(name.replace("\\", "/")).name or "log.zip"


def _component_local_path(component) -> Path | None:
    value = str(getattr(component, "file_", "") or "").strip()
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            return None
        return Path(urllib.request.url2pathname(parsed.path))
    if parsed.scheme:
        return None
    return Path(value)


def stage_log_file(
    component,
    destination: Path,
    max_bytes: int,
    allowed_hosts: list[str] | None = None,
) -> tuple[Path, str, int]:
    """Copy/download one raw ZIP or LOG attachment under a strict byte cap."""
    allowed_hosts = allowed_hosts or []
    name = _component_name(component)
    suffix = Path(name).suffix.lower()
    if suffix not in {".zip", ".log"}:
        raise FileInputError("只接受 .zip 或 .log 日志文件，请重新上传。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    url = str(getattr(component, "url", "") or "")
    source_path = _component_local_path(component)
    if source_path and source_path.exists():
        log.info("报障日志文件使用 AstrBot 提供的本地文件副本。")
        try:
            info = source_path.lstat()
        except OSError as exc:
            raise FileInputError("日志文件已失效，请重新上传。") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise FileInputError("无法读取这个日志文件，请重新上传。")
        if info.st_size > max_bytes:
            raise FileInputError(f"日志文件超过大小上限（{format_bytes(max_bytes)}），请压缩或只保留相关日志后重新上传。")
        try:
            with source_path.open("rb") as source:
                byte_count = _copy_limited(source, destination, max_bytes)
        except OSError as exc:
            raise FileInputError("读取日志文件失败，请重新上传。") from exc
    elif url:
        # AstrBot adapters often expose both a local file_ and a remote url.
        # Reuse the downloaded local copy when available so QQ/CDN URL fetches
        # cannot stall an otherwise ready upload.
        log.info("报障日志文件没有可用的本地副本，正在下载适配器提供的文件 URL。")
        byte_count = _download_limited(url, destination, max_bytes, allowed_hosts)
    elif source_path:
        raise FileInputError("日志文件已失效，请重新上传。")
    else:
        raise FileInputError("此 QQ 适配器没有提供可读取的群文件 URL 或本地文件。")

    if byte_count <= 0:
        destination.unlink(missing_ok=True)
        raise FileInputError("上传的日志文件为空。")
    return destination, name, byte_count


# Keep the previous internal helper name for deployments recovering older code.
stage_zip_file = stage_log_file
