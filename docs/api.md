# 接口说明

## CNB OpenAPI

所有请求都发到 `cnb_api_endpoint`，带 `Authorization: Bearer <cnb_token>` 请求头。下表中的 `{repo}` 是 `cnb_repository`，`{n}` 是 Issue 编号。

| 用途 | 请求 |
| --- | --- |
| 申请附件上传地址 | `POST /{repo}/-/issues/asset-groups` |
| 上传附件 | `PUT <上一步返回的 upload_url>` |
| 创建 Issue | `POST /{repo}/-/issues`，body 为 `{title, body}` |
| 查询 Issue | `GET /{repo}/-/issues/{n}` |
| 关闭 Issue | `PATCH /{repo}/-/issues/{n}`，body 为 `{"state": "closed", "state_reason": "completed"}` |
| 发评论 | `POST /{repo}/-/issues/{n}/comments`，body 为 `{body}` |
| 拉评论 | `GET /{repo}/-/issues/{n}/comments?page=&page_size=100`，分页拉取 |

接口路径和字段以你所用 CNB 部署的 Swagger 为准。参考：[CNB OpenAPI 文档](https://docs.cnb.cool/zh/develops/openapi.html)。

### 令牌权限

| 权限 | 用途 |
| --- | --- |
| `repo-issue:rw` | 上传附件，创建、查询、关闭 Issue |
| `repo-notes:r` | 拉取评论 |
| `repo-notes:rw` | 发评论（触发 NPC、写入用户补充、超时说明） |

### 错误处理

- 创建 Issue 或发评论时遇到 HTTP 4xx：说明请求被明确拒绝了。插件不重试，直接把错误告诉用户。
- 遇到 HTTP 5xx、超时或连接断开：请求有没有成功说不准，按 [工作流程](how-it-works.md#网络中断和重启) 里的规则处理。
- 查询评论、查询和关闭 Issue 可以放心重复，失败了就退避重试。

## NPC 触发与回复匹配

插件在 Issue 下发这样一条评论：

```text
@CodeBuddy 请分析本 Issue。报障编号：<追踪编号>。

请先核实日志是否读取成功，再结合仓库代码说明原因、证据、处理步骤和需要补充的信息。……

最终回复请严格分为两部分：
一句话描述：……
详细分析：……

最终回复请包含 [CNB-BOT:<追踪编号>:FINAL]。
```

用户发 `/debug analyze` 时，插件会再发一条类似的评论，要求结合新增评论重新分析。这时标记换成 `[CNB-BOT:<追踪编号>:ANALYSIS:<轮次>:FINAL]`，所以旧的回复不会被当成新一轮的结果。

一条评论要同时满足下面三个条件，才算 NPC 的最终回复：

1. 作者的 ID 在 `npc_author_ids` 里，或者 username 在 `npc_author_usernames` 里
2. 正文包含本轮的标记
3. 评论时间不早于本轮触发的时间

光有标记不够，因为任何人都能在评论里抄一份标记。有多条符合条件时，取最早的那条。

回复里如果有“一句话描述”“详细分析”两个小标题（支持 Markdown 标题和加粗写法），插件会按这两部分整理。没有的话，取正文第一句当结论，全文照发。

插件自己写的其他评论：

- 用户补充：以 `报障补充说明：` 开头，后面是用户原话
- 超时关闭：`报障人在 N 分钟内没有确认问题是否解决，插件已自动关闭此 Issue。……`

NPC 本身的配置和能力（比如能不能读 zip 附件）见 [CNB NPC 文档](https://docs.cnb.cool/zh/build/npc.html)。

## AstrBot

| 用途 | 接口 |
| --- | --- |
| `/debug` 指令 | `filter.command("debug")` |
| 接收日志文件和补充文字 | `filter.event_message_type(GROUP_MESSAGE / PRIVATE_MESSAGE)` |
| 主动发消息（通知、结论） | `context.send_message(unified_msg_origin, MessageChain)` |
| 合并转发 | `MessageChain` 中放一个 `Node` 组件 |

说明：

- AstrBot 的唤醒前缀需要包含 `/`，默认配置就是这样。
- 私信里的合并转发，要求 OneBot 客户端实现了 `send_private_forward_msg`。
- 日志文件要么由适配器提供本地路径（`File.file_`），要么提供下载 URL（`File.url`）。有本地路径时优先用本地路径。

参考：[AstrBot 消息事件](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)、[AstrBot 主动消息](https://docs.astrbot.app/dev/star/guides/send-message.html)。

## QQ 官方指令面板

启用 `qq_command_panel_enabled` 后，插件使用 `qq_command_panel_app_id` 和 `qq_command_panel_client_secret` 获取 QQ Bot `access_token`，然后对 `/v2/panels` 同步本插件标记的单聊与群聊全局面板。它只更新备注为 `astrbot_plugin_cnb_bot:managed_command_panel` 的面板，不删除其他面板。详见 [QQ 官方菜单与指令面板文档](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/menu-panel/) 和 [访问凭证说明](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html)。
