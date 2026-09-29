"""AstrBot /debug commands and log attachment handling."""

from __future__ import annotations

import asyncio
import difflib
import re
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .jobs import ReportJobs, status_label
from .settings import assistant_name, format_duration, log_wait_seconds
from .storage import ACTIVE_STATUSES, TaskStore

SUBCOMMANDS = {
    "start": {"start", "new", "开始"},
    "help": {"help", "帮助", "?", "？"},
    "analyze": {"analyze", "分析", "重新分析"},
    "status": {"status", "状态"},
    "resolve": {"resolve", "resolved", "恢复", "已恢复", "解决", "已解决"},
    "cancel": {"cancel", "取消"},
}
_ENGLISH_SUBCOMMANDS = ["help", "analyze", "status", "resolve", "cancel"]


def _subcommand(argument: str) -> str:
    lowered = argument.strip().lower()
    for name, aliases in SUBCOMMANDS.items():
        if lowered in aliases:
            return name
    return ""


def _likely_typo(argument: str) -> str:
    """Return the subcommand a single-word argument most likely misspells."""
    word = argument.strip().lower()
    if not re.fullmatch(r"[a-z]{3,12}", word):
        return ""
    matches = difflib.get_close_matches(word, _ENGLISH_SUBCOMMANDS, n=1, cutoff=0.6)
    return matches[0] if matches else ""


def _component_type(component: Any) -> str:
    value = getattr(component, "type", "")
    value = getattr(value, "value", value)
    return str(value or component.__class__.__name__).lower()


def _file_components(event: AstrMessageEvent) -> list[Any]:
    message_obj = getattr(event, "message_obj", None)
    components = getattr(message_obj, "message", []) or []
    return [component for component in components if _component_type(component) in {"file", "componenttype.file"}]


def _mentions_bot(event: AstrMessageEvent, bot_id: str) -> bool:
    message_obj = getattr(event, "message_obj", None)
    components = getattr(message_obj, "message", []) or []
    for component in components:
        if _component_type(component) not in {"at", "componenttype.at"}:
            continue
        target = getattr(component, "qq", None)
        if target is None:
            target = getattr(component, "target", None)
        if str(target or "") == str(bot_id):
            return True
    return False


def _plain_message_text(event: AstrMessageEvent) -> str:
    message_obj = getattr(event, "message_obj", None)
    components = getattr(message_obj, "message", []) or []
    text = "".join(
        str(getattr(component, "text", "") or "")
        for component in components
        if _component_type(component) in {"plain", "componenttype.plain"}
    ).strip()
    if components:
        return text
    return str(getattr(event, "message_str", "") or "").strip()


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _private_user_allowed(config: dict[str, Any], sender_id: str) -> bool:
    """An empty private allowlist leaves the private entry point open."""
    allowed_users = _as_string_list(config.get("private_whitelist", []))
    return not allowed_users or str(sender_id) in set(allowed_users)


def _is_private_message(event: AstrMessageEvent) -> bool:
    checker = getattr(event, "is_private_chat", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            pass
    return not str(event.get_group_id() or "")


def _event_scope(event: AstrMessageEvent) -> tuple[str, str, str, str] | None:
    private = _is_private_message(event)
    scope = (
        str(event.get_platform_name() or ""),
        str(event.get_self_id() or ""),
        "" if private else str(event.get_group_id() or ""),
        str(event.get_sender_id() or ""),
    )
    if not (scope[0] and scope[1] and scope[3] and event.unified_msg_origin):
        return None
    return scope if scope[2] or private else None


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
    "通过群聊或私信 /debug 提交原始 ZIP 或 LOG 日志，创建 CNB Issue 并跟踪 NPC 回复。",
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
        except Exception as exc:
            raise RuntimeError(
                "无法获取 AstrBot 插件数据目录，拒绝在插件目录中创建数据文件。"
            ) from exc
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
    async def debug(self, event: AstrMessageEvent, action: str = ""):
        """提交 ZIP 或 LOG 日志，可附故障描述作为 Issue 标题。"""
        scope = _event_scope(event)
        if not scope:
            return
        private = _is_private_message(event)
        if private:
            if not _private_user_allowed(self.config, scope[3]):
                event.stop_event()
                yield event.plain_result("此账号未获准使用私信报障，请联系管理员调整私信白名单。")
                return
        elif scope[2] not in set(_as_string_list(self.config.get("group_whitelist", []))):
            # Stay silent by default so another plugin's /debug still works here.
            if self.config.get("reply_in_disabled_groups", False):
                event.stop_event()
                yield event.plain_result("此群未启用报障功能，请联系管理员配置群白名单。")
            return
        event.stop_event()
        argument = _debug_argument(event, action)
        command = _subcommand(argument) if argument else ""
        platform_name, bot_id, group_id, sender_id = scope
        if not argument or command == "start":
            response = self._start_report(scope, str(event.unified_msg_origin))
        elif command == "help":
            response = self._help_text(private)
        elif command == "analyze":
            task = self.store.find_current(*scope)
            if task:
                _, response = await self.jobs.request_npc_analysis(
                    task["id"], sender_id, platform_name, bot_id, group_id
                )
            else:
                response = "你在这里还没有报障，请先发送 /debug 开始。"
        elif command == "status":
            task = self.store.find_current(*scope)
            if task:
                task = await self.jobs.refresh_task(task["id"], notify=False) or task
                task = self.store.get(task["id"]) or task
                response = self.jobs.format_status(task)
            else:
                response = "你在这里还没有报障，请先发送 /debug 开始。"
        elif command == "resolve":
            task = self.store.find_current(*scope)
            if task:
                previous_status = str(task.get("status", ""))
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
                response = "你在这里还没有报障。"
        elif command == "cancel":
            task = self.store.find_current(*scope)
            if task:
                _, response = self.jobs.cancel(task["id"], sender_id, platform_name, bot_id, group_id)
            else:
                response = "你在这里还没有报障。"
        elif _likely_typo(argument):
            guess = _likely_typo(argument)
            response = (
                f"没有 /debug {argument.strip()} 这个指令，你是不是想发送 /debug {guess}？\n"
                "如果这是故障描述，请写得更具体一些，例如：/debug 启动后闪退。\n"
                "发送 /debug help 查看全部指令。"
            )
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
        scope = _event_scope(event)
        if (
            not scope
            or _is_private_message(event)
            or scope[2] not in set(_as_string_list(self.config.get("group_whitelist", [])))
        ):
            return
        response = await self._handle_report_message(event, scope, private=False)
        if response:
            yield event.plain_result(response)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        scope = _event_scope(event)
        if (
            not scope
            or not _is_private_message(event)
            or not _private_user_allowed(self.config, scope[3])
        ):
            return
        response = await self._handle_report_message(event, scope, private=True)
        if response:
            yield event.plain_result(response)

    async def _handle_report_message(
        self,
        event: AstrMessageEvent,
        scope: tuple[str, str, str, str],
        *,
        private: bool,
    ) -> str | None:
        # Ordinary messages are neither cached nor included in CNB Issues.
        incoming_files = _file_components(event)
        if incoming_files:
            waiting = self.store.find_waiting(*scope)
            if not waiting:
                return
            if len(incoming_files) != 1:
                event.stop_event()
                return "每次报障只接收一个 .zip 或 .log 文件，请只发送一个日志文件。"
            if not self.store.claim_waiting_log(waiting["id"], scope[3]):
                return
            event.stop_event()
            # The pipeline can close a stopped event at its first yield. Finish the
            # attachment processing before yielding; send the receipt directly.
            try:
                await asyncio.wait_for(
                    event.send(event.plain_result("已收到日志，正在提交，请稍候…")),
                    timeout=10,
                )
            except Exception:
                logger.warning("日志接收回执发送失败，继续处理报障 %s。", waiting["id"], exc_info=True)
            ok, response = await self.jobs.accept_attachment(waiting["id"], incoming_files[0])
            if not ok:
                latest = self.store.get(waiting["id"])
                if latest and latest["status"] == "WAITING_LOG":
                    remaining = format_duration(float(latest.get("deadline", 0)) - time.time())
                    response += f"\n请在 {remaining}内重新上传，或发送 /debug cancel 取消报障。"
            return response

        if not private and not _mentions_bot(event, scope[1]):
            return
        text = _plain_message_text(event)
        if text.lstrip().lower().startswith(("/debug", "!debug")):
            return
        task = self.store.find_current(*scope)
        if not task or task.get("status") not in ACTIVE_STATUSES:
            return
        if private and not text:
            return
        event.stop_event()
        if not private and not text:
            response = "请在 @我 的同一条消息里写上要补充的文字，它会作为评论写入 Issue。"
        elif not task.get("issue_number"):
            response = (
                "Issue 还没创建好，请稍后再直接私信发送补充信息。"
                if private
                else "Issue 还没创建好，请稍后再 @我 发送补充信息。"
            )
        elif task.get("status") == "CLOSING_ISSUE":
            response = "报障正在结束，这条补充信息没有提交。"
        else:
            ok, response = await self.jobs.append_issue_comment(task["id"], text)
        return response

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
        wait_seconds = log_wait_seconds(self.config)
        task = {
            "id": str(uuid.uuid4()),
            "active_key": f"{platform_name}:{bot_id}:{group_id}:{sender_id}",
            "platform_name": platform_name,
            "bot_id": bot_id,
            "group_id": group_id,
            "user_id": sender_id,
            "unified_msg_origin": unified_msg_origin,
            "deadline": time.time() + wait_seconds,
            "repository": str(self.config.get("cnb_repository", "")).strip().strip("/"),
            "issue_title": issue_title,
        }
        created, existing = self.store.create_waiting(task)
        if not created and existing:
            return (
                f"你在这里已有一个未结束的报障（{status_label(existing)}）。\n"
                "发送 /debug status 查看进度，或 /debug cancel 取消后重新开始。"
            )
        private = not group_id
        lines = [
            f"请在 {format_duration(wait_seconds)}内"
            + ("" if private else "由你本人在本群")
            + "上传一个 .zip 或 .log 日志文件。"
        ]
        hint = str(self.config.get("log_location_hint", "") or "").strip()
        if hint:
            lines.append(hint)
        if issue_title:
            lines.append(f"Issue 标题：{issue_title}")
        how = "直接私信" if private else "@我"
        lines.append(
            f"上传后会创建 Issue 并请{assistant_name(self.config)}分析；之后可{how}补充信息。"
        )
        lines.append("注意：文件会原样提交到 CNB 仓库，不会读取或脱敏，请确认不含隐私内容。")
        lines.append("/debug cancel 取消 · /debug help 查看帮助")
        return "\n".join(lines)

    def _help_text(self, private: bool) -> str:
        how = "直接私信我" if private else "@我"
        return (
            "报障指令：\n"
            "/debug [故障描述] 开始报障，描述会作为 Issue 标题\n"
            "/debug status 查看进度\n"
            f"/debug analyze 补充信息后请{assistant_name(self.config)}重新分析\n"
            "/debug resolve 确认问题已解决\n"
            "/debug cancel 取消报障\n"
            f"Issue 创建后，{how}发送文字即可补充到 Issue。"
        )
