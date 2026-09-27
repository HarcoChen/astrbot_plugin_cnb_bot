"""AstrBot /debug commands and ZIP attachment handling."""

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

_HELP = "/debug：提交 ZIP 日志\n/debug status：查看自己的报障状态\n/debug cancel：取消自己的报障"


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


@register(
    "astrbot_plugin_cnb_bot",
    "harco",
    "通过 /debug 提交原始 ZIP 日志，创建 CNB Issue 并跟踪 NPC 回复。",
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
        self._started = False

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        if not self._started:
            self.jobs.start()
            self._started = True
            logger.info("CNB 报障插件后台任务已启动。")

    async def terminate(self) -> None:
        await self.jobs.shutdown()
        self.store.close()

    @filter.command("debug")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def debug(self, event: AstrMessageEvent, action: str = ""):
        """提交 ZIP 日志；status 查询，cancel 取消。无需 @ 或报障编号。"""
        scope = _event_scope(event)
        if not scope:
            return
        event.stop_event()
        if scope[2] not in set(_as_string_list(self.config.get("group_whitelist", []))):
            yield event.plain_result("此群未启用报障功能，请联系管理员配置群白名单。")
            return
        action = action.strip().lower()
        if not action:
            response = self._start_report(scope, str(event.unified_msg_origin))
        elif action in {"status", "状态"}:
            task = self.store.find_current(*scope)
            if task and task["status"] == "WAITING_NPC":
                await self.jobs.poll_npc_now(task["id"])
                task = self.store.get(task["id"]) or task
                if task["status"] == "WAITING_NPC":
                    if task.get("last_poll_error"):
                        response = "刚刚查询 CNB 评论失败，插件会自动重试。\n" + self._format_status(task)
                    else:
                        response = "已立即查询 CNB 评论，暂未发现符合条件的 NPC 最终回复。\n" + self._format_status(task)
                else:
                    response = self._format_status(task)
            else:
                response = self._format_status(task) if task else "你在此群还没有报障，请发送 /debug 开始。"
        elif action in {"cancel", "取消"}:
            task = self.store.find_current(*scope)
            if task:
                platform_name, bot_id, group_id, sender_id = scope
                _, response = self.jobs.cancel(task["id"], sender_id, platform_name, bot_id, group_id)
            else:
                response = "你在此群还没有报障。"
        else:
            response = _HELP
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
            yield event.plain_result("每个报障任务只接收一个 ZIP 文件，请只发送一个 ZIP。")
            return
        if not self.store.claim_waiting_log(waiting["id"], scope[3]):
            return
        event.stop_event()
        # The pipeline can close a stopped event at its first yield. Finish the
        # attachment processing before yielding; send the receipt directly.
        try:
            await asyncio.wait_for(
                event.send(event.plain_result("已收到 ZIP，正在准备上传 CNB。")),
                timeout=10,
            )
        except Exception:
            logger.warning("ZIP 接收回执发送失败，继续处理报障 %s。", waiting["id"], exc_info=True)
        ok, response = await self.jobs.accept_attachment(waiting["id"], incoming_files[0])
        if not ok:
            latest = self.store.get(waiting["id"])
            if latest and latest["status"] == "WAITING_LOG":
                response += "\n请在等待时限内重新上传 ZIP，或使用 /debug cancel 结束报障。"
        yield event.plain_result(response)

    def _start_report(self, scope: tuple[str, str, str, str], unified_msg_origin: str) -> str:
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
        }
        created, existing = self.store.create_waiting(task)
        if not created and existing:
            label = STATUS_LABELS.get(existing["status"], existing["status"])
            return f"你在此群已有未结束的报障（{label}）。\n使用 /debug status 查询，或 /debug cancel 取消。"
        return (
            "已开始报障。\n"
            f"请在 {max(1, int(self.config.get('log_wait_seconds', 600)) // 60)} 分钟内，在本群用当前账号上传一个 ZIP 日志。\n"
            "仅提交原始 ZIP，不采集聊天上下文；ZIP 不会解压或脱敏。\n"
            "使用 /debug status 查询，或 /debug cancel 取消。"
        )

    @staticmethod
    def _format_status(task: dict[str, Any]) -> str:
        label = STATUS_LABELS.get(task["status"], task["status"])
        lines = [f"状态：{label}"]
        if task.get("issue_url"):
            lines.append(f"Issue：{task['issue_url']}")
        if task.get("last_error"):
            lines.append(f"说明：{task['last_error']}")
        if task.get("status") == "DELIVERING" and task.get("last_delivery_error"):
            lines.append(
                f"最近一次转发发送失败：{task['last_delivery_error']}；插件会自动重试。"
            )
        if task.get("status") == "UNCERTAIN" and task.get("uncertain_kind") == "issue_creation":
            lines.append(f"请管理员在 CNB 核对内部追踪编号 {task['id']}；插件不会自动重复创建。")
        return "\n".join(lines)
