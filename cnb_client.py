"""Small CNB OpenAPI client for Issues, comments, and issue assets."""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class CNBAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CNBNetworkError(RuntimeError):
    """The request outcome may be unknown after a transport-level failure."""


class CNBClient:
    def __init__(
        self,
        api_endpoint: str,
        web_endpoint: str,
        repository: str,
        token: str,
        timeout: int = 30,
    ) -> None:
        self.api_endpoint = api_endpoint.rstrip("/")
        self.web_endpoint = web_endpoint.rstrip("/")
        self.repository = repository.strip().strip("/")
        self.token = token.strip()
        self.timeout = max(5, min(int(timeout), 120))
        for endpoint in (self.api_endpoint, self.web_endpoint):
            parsed = urllib.parse.urlsplit(endpoint)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("CNB API/Web endpoint 必须是有效的 HTTPS 地址。")
        if not self.repository or any(
            not part or part in {".", ".."} for part in self.repository.split("/")
        ):
            raise ValueError("CNB 仓库路径格式无效。")
        if not self.token:
            raise ValueError("请先在插件配置中填写 CNB 访问令牌。")

    def _repo_path(self) -> str:
        return urllib.parse.quote(self.repository, safe="/")

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.api_endpoint}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "AstrBot-CNB-Report/0.1",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise CNBAPIError("CNB API 响应超过 4 MiB 限制。", response.status)
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise CNBAPIError("CNB API 返回了无法解析的 JSON。", response.status) from exc
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(64 * 1024)
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                message = payload.get("errmsg") or payload.get("message") or str(exc.reason)
            except Exception:
                message = str(exc.reason)
            raise CNBAPIError(f"CNB API 请求失败（HTTP {exc.code}）：{message}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CNBNetworkError(f"CNB API 网络请求结果不确定：{exc}") from exc

    def _put_upload(self, upload_url: str, path: str) -> None:
        parsed = urllib.parse.urlsplit(upload_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise CNBAPIError("CNB 返回的附件上传 URL 不是有效 HTTPS 地址。")
        file_path = Path(path)
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        request = urllib.request.Request(
            upload_url,
            data=file_path.read_bytes(),
            headers={"Content-Type": content_type},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise CNBAPIError(f"CNB 附件上传失败（HTTP {response.status}）。", response.status)
                response.read(64 * 1024)
        except urllib.error.HTTPError as exc:
            raise CNBAPIError(f"CNB 附件上传失败（HTTP {exc.code}）。", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # The signed upload URL can contain a temporary credential. Do not
            # persist or relay the transport exception because it may include it.
            raise CNBNetworkError("CNB 附件上传结果不确定，请检查 CNB 外部状态。") from exc

    def upload_issue_attachment(self, path: str, filename: str = "") -> dict[str, str]:
        file_path = Path(path)
        upload_name = Path(filename).name if filename else file_path.name
        if not upload_name:
            upload_name = file_path.name
        content_type = mimetypes.guess_type(upload_name)[0] or "application/octet-stream"
        upload_request = {
            "file_assets": [
                {
                    "name": upload_name,
                    "size": file_path.stat().st_size,
                    "content_type": content_type,
                }
            ]
        }
        created = self._request_json(
            "POST", f"/{self._repo_path()}/-/issues/asset-groups", upload_request
        )
        candidates = created.get("file_upload_urls", []) if isinstance(created, dict) else []
        if not candidates and isinstance(created, dict):
            candidates = created.get("assets", [])
        if not candidates or not isinstance(candidates[0], dict):
            raise CNBAPIError("CNB 创建附件组时没有返回文件上传地址。")
        asset = candidates[0]
        upload_url = asset.get("upload_url")
        asset_link = asset.get("asset_link")
        if not upload_url or not asset_link:
            raise CNBAPIError("CNB 附件上传响应缺少 upload_url 或 asset_link。")
        self._put_upload(str(upload_url), path)
        return {
            "asset_link": str(asset_link),
            "download_url": str(asset.get("download_url", "")),
            "path": str(asset.get("path", "")),
            "name": str(asset.get("name", upload_name)),
        }

    def create_issue(self, title: str, body: str) -> dict[str, Any]:
        result = self._request_json(
            "POST",
            f"/{self._repo_path()}/-/issues",
            {"title": title, "body": body},
        )
        if not isinstance(result, dict) or not result.get("number"):
            raise CNBAPIError("CNB 创建 Issue 响应缺少 Issue 编号。")
        result.setdefault("html_url", self.issue_url(str(result["number"])))
        return result

    def get_issue(self, issue_number: str) -> dict[str, Any]:
        result = self._request_json(
            "GET",
            f"/{self._repo_path()}/-/issues/{urllib.parse.quote(str(issue_number), safe='')}",
        )
        if not isinstance(result, dict):
            raise CNBAPIError("CNB Issue 查询响应格式异常。")
        return result

    def close_issue(self, issue_number: str) -> dict[str, Any]:
        result = self._request_json(
            "PATCH",
            f"/{self._repo_path()}/-/issues/{urllib.parse.quote(str(issue_number), safe='')}",
            {"state": "closed", "state_reason": "completed"},
        )
        if not isinstance(result, dict):
            raise CNBAPIError("CNB Issue 关闭响应格式异常。")
        return result

    def issue_url(self, issue_number: str) -> str:
        repo = urllib.parse.quote(self.repository, safe="/")
        number = urllib.parse.quote(str(issue_number), safe="")
        return f"{self.web_endpoint}/{repo}/-/issues/{number}"

    def create_comment(self, issue_number: str, body: str) -> dict[str, Any]:
        result = self._request_json(
            "POST",
            f"/{self._repo_path()}/-/issues/{urllib.parse.quote(str(issue_number), safe='')}/comments",
            {"body": body},
        )
        return result if isinstance(result, dict) else {}

    def list_comments(self, issue_number: str, page: int, page_size: int = 100) -> list[dict[str, Any]]:
        result = self._request_json(
            "GET",
            f"/{self._repo_path()}/-/issues/{urllib.parse.quote(str(issue_number), safe='')}/comments",
            query={"page": page, "page_size": page_size},
        )
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if isinstance(result, dict) and isinstance(result.get("data"), list):
            return [item for item in result["data"] if isinstance(item, dict)]
        raise CNBAPIError("CNB 评论列表响应格式异常。")
