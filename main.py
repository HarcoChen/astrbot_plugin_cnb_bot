"""AstrBot /debug commands and log attachment handling."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .jobs import ReportJobs, STATUS_LABELS
from .storage import TaskStore


def _component_type(component: Any) -> str:
    value = getattr(component, "type", "")
    value = getattr(value, "value", value)
    return str(value or component.__class__.__name__).lower()


def _file_components(event: AstrMessageEvent) -> list[Any]:
    message_obj = getattr(event, "message_obj", None)
    components = getattr(message_obj, "message", []) or []
    return [component for component in components if _component_type(component) in {"file", "componenttype.file"}]


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _event_scope(event: AstrMessageEvent) -> tuple[str, str, str, str] | None:
    scope = (
        str(event.get_platform_name() or ""),
        str(event.get_self_id() or ""),
        str(event.get_group_id() or ""),
        str(event.get_sender_id() or ""),
    )
    return scope if all(scope) and event.unified_msg_origin else None


def _debug_argument(event: AstrMessageEvent, parsed_action: str) -> str:
    """Return all text after /debug, preserving multi-word issue titles."""
    raw = str(getattr(event, "message_str", "") or "").strip()
    parts = raw.split(maxsplit=1)
    if len(parts) == 2:
        command_head = parts[0].lstrip("/!#$%^&*+=:;~.,<>").lower()
        if command_head == "debug":
            return parts[1].strip()
    return str(parsed_action or "").strip()


@register(
    "astrbot_plugin_cnb_bot",
    "harco",
    "通过 /debug 提交原始 ZIP 或 LOG 日志，创建 CNB Issue 并跟踪 NPC 回复。",
    "0.2.0",
    "",
)
class CNBReportPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.context = context
        self.config = dict(config or {})
        try:
            data_dir = Path(StarTools.get_data_dir("astrbot_plugin_cnb_bot"))
        except Exception:
            data_dir = Path(__file__).resolve().parent / "data"
            logger.warning("无法定位 AstrBot 插件数据目录，回退到插件目录 data。")
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = TaskStore(data_dir / "reports.sqlite3")
        self.jobs = ReportJobs(context, self.config, self.store, data_dir)

    async def initialize(self) -> None:
        # Plugin initialization also runs on hot reload; the global loaded event
        # alone does not cover that lifecycle.
        self.jobs.start()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        self.jobs.start()

    async def terminate(self) -> None:
        await self.jobs.shutdown()
        self.store.close()

    @filter.command("debug")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def debug(self, event: AstrMessageEvent, action: str = ""):
        """提交 ZIP 或 LOG 日志，可附故障描述作为 Issue 标题。"""
        scope = _event_scope(event)
        if not scope:
            return
        event.stop_event()
        if scope[2] not in set(_as_string_list(self.config.get("group_whitelist", []))):
            yield event.plain_result("此群未启用报障功能，请联系管理员配置群白名单。")
            return
        argument = _debug_argument(event, action)
        if not argument:
            response = self._start_report(scope, str(event.unified_msg_origin))
        elif argument.lower() in {"status", "状态"}:
            task = self.store.find_current(*scope)
            if task:
                task = await self.jobs.refresh_task(task["id"], notify=False) or task
                task = self.store.get(task["id"]) or task
                if task["status"] == "WAITING_NPC" and task.get("last_poll_error"):
                    response = "已刷新完整报障状态；刚刚查询 CNB 评论失败，插件会自动重试。\n" + self._format_status(task)
                else:
                    response = "已刷新完整报障状态。\n" + self._format_status(task)
            else:
                response = "你在此群还没有报障，请发送 /debug 开始。"
        elif argument.lower() in {"resolve", "resolved", "恢复", "已恢复"}:
            task = self.store.find_current(*scope)
            if task:
                previous_status = str(task.get("status", ""))
                platform_name, bot_id, group_id, sender_id = scope
                _, response = await self.jobs.confirm_recovery(
                    task["id"], sender_id, platform_name, bot_id, group_id
                )
                latest = self.store.get(task["id"])
                if (
                    previous_status != "DONE"
                    and latest
                    and latest.get("status") == "DONE"
                ):
                    # _finish already sent the completion notice with a mention.
                    response = ""
            else:
                response = "你在此群还没有报障。"
        elif argument.lower() in {"cancel", "取消"}:
            task = self.store.find_current(*scope)
            if task:
                platform_name, bot_id, group_id, sender_id = scope
                _, response = self.jobs.cancel(task["id"], sender_id, platform_name, bot_id, group_id)
            else:
                response = "你在此群还没有报障。"
        else:
            title = " ".join(argument.split())
            response = self._start_report(
                scope,
                str(event.unified_msg_origin),
                issue_title=title,
            )
        if response:
            yield event.plain_result(response)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        # Ordinary messages are neither cached nor included in CNB Issues.
        incoming_files = _file_components(event)
        if not incoming_files:
            return
        scope = _event_scope(event)
        if not scope or scope[2] not in set(_as_string_list(self.config.get("group_whitelist", []))):
            return
        waiting = self.store.find_waiting(*scope)
        if not waiting:
            return
        if len(incoming_files) != 1:
            event.stop_event()
            yield event.plain_result("每个报障任务只接收一个 .zip 或 .log 文件，请只发送一个日志文件。")
            return
        if not self.store.claim_waiting_log(waiting["id"], scope[3]):
            return
        event.stop_event()
        # The pipeline can close a stopped event at its first yield. Finish the
        # attachment processing before yielding; send the receipt directly.
        try:
            await asyncio.wait_for(
                event.send(event.plain_result("已收到日志文件，正在准备上传 CNB。")),
                timeout=10,
            )
        except Exception:
            logger.warning("日志接收回执发送失败，继续处理报障 %s。", waiting["id"], exc_info=True)
        ok, response = await self.jobs.accept_attachment(waiting["id"], incoming_files[0])
        if not ok:
            latest = self.store.get(waiting["id"])
            if latest and latest["status"] == "WAITING_LOG":
                response += "\n请在等待时限内重新上传 .zip 或 .log 文件，或使用 /debug cancel 结束报障。"
        yield event.plain_result(response)

    def _start_report(
        self,
        scope: tuple[str, str, str, str],
        unified_msg_origin: str,
        issue_title: str = "",
    ) -> str:
        missing = []
        if not self.config.get("cnb_repository"):
            missing.append("目标 CNB 仓库")
        if not self.config.get("cnb_token"):
            missing.append("CNB 访问令牌")
        if not _as_string_list(self.config.get("npc_author_ids", [])) and not _as_string_list(
            self.config.get("npc_author_usernames", [])
        ):
            missing.append("已验证的 NPC 作者 ID 或 username")
        if not missing:
            try:
                self.jobs.validate_configuration()
            except (TypeError, ValueError) as exc:
                missing.append(str(exc))
        if missing:
            return "插件配置尚未完成：" + "、".join(missing) + "。请联系管理员配置后再试。"

        platform_name, bot_id, group_id, sender_id = scope
        task = {
            "id": str(uuid.uuid4()),
            "active_key": f"{platform_name}:{bot_id}:{group_id}:{sender_id}",
            "platform_name": platform_name,
            "bot_id": bot_id,
            "group_id": group_id,
            "user_id": sender_id,
            "unified_msg_origin": unified_msg_origin,
            "deadline": time.time() + int(self.config.get("log_wait_seconds", 600)),
            "repository": str(self.config.get("cnb_repository", "")).strip().strip("/"),
            "issue_title": issue_title,
        }
        created, existing = self.store.create_waiting(task)
        if not created and existing:
            label = STATUS_LABELS.get(existing["status"], existing["status"])
            return f"你在此群已有未结束的报障（{label}）。\n使用 /debug status 查询，或 /debug cancel 取消。"
        title_line = f"Issue 标题：{issue_title}\n" if issue_title else ""
        return (
            f"已开始报障。\n{title_line}"
            f"请在 {max(1, int(self.config.get('log_wait_seconds', 600)) // 60)} 分钟内，在本群用当前账号上传一个 .zip 或 .log 日志文件。\n"
            "仅提交原始文件，不采集聊天上下文；不会读取或脱敏文件内容。\n"
            "使用 /debug status 查询，或 /debug cancel 取消。"
        )

    @staticmethod
    def _format_status(task: dict[str, Any]) -> str:
        label = STATUS_LABELS.get(task["status"], task["status"])
        lines = [f"状态：{label}"]
        if task.get("issue_url"):
            lines.append(f"Issue：{task['issue_url']}")
            issue_state = task.get("issue_state")
            if issue_state:
                lines.append(f"Issue 状态：{'已关闭' if issue_state == 'closed' else '打开'}")
        if task.get("last_error"):
            lines.append(f"说明：{task['last_error']}")
        if task.get("status") == "AWAITING_RECOVERY":
            lines.append("确认按建议处理后发送 /debug resolve，插件会关闭此 Issue。")
        if task.get("status") in {"AWAITING_RECOVERY", "CLOSING_ISSUE"} and task.get("last_issue_error"):
            lines.append(f"Issue 状态同步遇到问题，插件会自动重试：{task['last_issue_error']}")
        if task.get("status") == "DELIVERING" and task.get("last_delivery_error"):
            lines.append(
                f"最近一次转发发送失败：{task['last_delivery_error']}；插件会自动重试。"
            )
        if task.get("status") == "UNCERTAIN" and task.get("uncertain_kind") == "issue_creation":
            lines.append(f"请管理员在 CNB 核对内部追踪编号 {task['id']}；插件不会自动重复创建。")
        return "\n".join(lines)
