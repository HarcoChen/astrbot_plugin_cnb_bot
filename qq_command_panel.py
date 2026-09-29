"""Optional synchronization of QQ Open Platform command panels."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from astrbot.api import logger

_TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
_PANELS_URL = "https://api.bot.qq.com/v2/panels"
_MANAGED_REMARK = "astrbot_plugin_cnb_bot:managed_command_panel"
_TIMEOUT_SECONDS = 15
_MAX_RESPONSE_BYTES = 1024 * 1024

_PANEL_ITEMS = [
    {
        "type": "command",
        "name": "/debug",
        "desc": "开始报障并上传日志",
        "only_admin": False,
    },
    {
        "type": "command",
        "name": "/debug status",
        "desc": "查看当前报障进度",
        "only_admin": False,
    },
    {
        "type": "command",
        "name": "/debug analyze",
        "desc": "补充信息后重新分析",
        "only_admin": False,
    },
    {
        "type": "command",
        "name": "/debug resolve",
        "desc": "确认问题已解决",
        "only_admin": False,
    },
    {
        "type": "command",
        "name": "/debug cancel",
        "desc": "取消当前报障",
        "only_admin": False,
    },
    {
        "type": "command",
        "name": "/debug help",
        "desc": "查看报障帮助",
        "only_admin": False,
    },
]


class QQCommandPanelError(RuntimeError):
    """Raised when QQ Open Platform rejects or cannot complete panel sync."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class QQCommandPanelClient:
    def __init__(self, app_id: str, client_secret: str) -> None:
        self.app_id = app_id.strip()
        self.client_secret = client_secret.strip()
        if not self.app_id or not self.client_secret:
            raise ValueError("请填写 QQ 开放平台 AppID 和机器人密钥。")
        self._opener = urllib.request.build_opener(_NoRedirectHandler())
        self._access_token = ""

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        access_token: str = "",
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", "User-Agent": "AstrBot-CNB-Report/0.1"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if access_token:
            headers["Authorization"] = f"QQBot {access_token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read(64 * 1024)
            try:
                error_body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeError, json.JSONDecodeError):
                error_body = {}
            message = error_body.get("message") or error_body.get("msg") or str(exc.reason)
            raise QQCommandPanelError(
                f"QQ 开放平台请求失败（HTTP {exc.code}）：{message}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise QQCommandPanelError("QQ 开放平台网络请求失败。") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise QQCommandPanelError("QQ 开放平台响应超过 1 MiB 限制。")
        if not raw:
            return {}
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise QQCommandPanelError(
                f"QQ 开放平台返回了无法解析的 JSON（HTTP {status}）。"
            ) from exc
        if not isinstance(result, dict):
            raise QQCommandPanelError("QQ 开放平台返回了非对象 JSON。")
        code = result.get("code")
        if code not in (None, 0, "0"):
            message = result.get("message") or result.get("msg") or "未知业务错误"
            raise QQCommandPanelError(f"QQ 开放平台返回错误 {code}：{message}")
        return result

    def _get_access_token(self) -> str:
        response = self._request_json(
            "POST",
            _TOKEN_URL,
            body={"appId": self.app_id, "clientSecret": self.client_secret},
        )
        token = str(response.get("access_token", "")).strip()
        if not token:
            raise QQCommandPanelError("QQ 开放平台没有返回 access_token。")
        return token

    def _get_panels(self, scope: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(10):
            query: dict[str, str | int] = {"scope": scope, "limit": 50}
            if cursor:
                query["cursor"] = cursor
            url = f"{_PANELS_URL}?{urllib.parse.urlencode(query)}"
            response = self._request_json("GET", url, access_token=self._access_token)
            page_records = response.get("records", [])
            if not isinstance(page_records, list):
                raise QQCommandPanelError("QQ 开放平台返回了无效的面板列表。")
            records.extend(record for record in page_records if isinstance(record, dict))
            if response.get("is_end") is True:
                return records
            next_cursor = str(response.get("next_cursor", "")).strip()
            if not next_cursor:
                return records
            cursor = next_cursor
        raise QQCommandPanelError("QQ 开放平台面板列表分页超过安全上限。")

    def _sync_scope(self, scope: str) -> str:
        panels = self._get_panels(scope)
        existing = next(
            (
                record
                for record in panels
                if record.get("target_type") == "all"
                and isinstance(record.get("panel"), dict)
                and record["panel"].get("remark") == _MANAGED_REMARK
            ),
            None,
        )
        panel = {"items": _PANEL_ITEMS, "remark": _MANAGED_REMARK}
        if existing:
            current_panel = existing["panel"]
            if (
                current_panel.get("remark") == _MANAGED_REMARK
                and current_panel.get("items") == _PANEL_ITEMS
            ):
                return "unchanged"
            panel_id = str(existing.get("panel_id", "")).strip()
            if not panel_id:
                raise QQCommandPanelError("已存在的插件面板缺少 panel_id。")
            url = f"{_PANELS_URL}/{urllib.parse.quote(panel_id, safe='')}"
            self._request_json(
                "PUT",
                url,
                body={"panel": panel},
                access_token=self._access_token,
            )
            return "updated"

        self._request_json(
            "POST",
            _PANELS_URL,
            body={"scope": scope, "target_type": "all", "panel": panel},
            access_token=self._access_token,
        )
        return "created"

    def sync(self) -> dict[str, str]:
        self._access_token = self._get_access_token()
        results = {}
        for scope in ("c2c", "group"):
            results[scope] = self._sync_scope(scope)
        return results


def sync_qq_command_panels(app_id: str, client_secret: str) -> dict[str, str]:
    """Create or update this plugin's global C2C and group command panels."""
    return QQCommandPanelClient(app_id, client_secret).sync()
