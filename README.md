# AstrBot CNB 报障助手

AstrBot 插件：在白名单 QQ 群中由用户发送 `/debug` 发起报障，接收原始 ZIP 日志，创建 CNB Issue，再跟踪已验证身份的 CNB NPC 最终回复并通过 QQ 合并转发发回原群。

当前版本面向 AstrBot 4.16–4.x 和 `aiocqhttp`（OneBot v11）平台。群文件接收依赖适配器提供的 File 消息；NPC 作者字段应按目标仓库实际回复配置。

## 用户指令

直接发送以下命令，无需 @机器人、问题描述或 UUID。命令只在管理员配置的群白名单内生效。AstrBot 的唤醒前缀需包含 `/`（默认配置）。

```text
/debug
/debug status
/debug resolve
/debug cancel
```

发起后，原群中发起任务的同一账号须在默认 10 分钟内上传一个 `.zip` 文件。插件将任务绑定到平台实例、机器人、群和发起用户；每个机器人平台实例中，同一用户在同一群同时只能有一个未结束任务。重复发送 `/debug` 会提示现有任务，不会另建任务。`/debug status` 会立即推进并核对当前任务的全部安全状态步骤，不等待后台轮询；没有活跃任务时显示最近一次结果。NPC 分析转发完成后任务会等待恢复确认；确认已恢复时发送 `/debug resolve`，插件核对并关闭 CNB Issue 后才结束任务。`/debug cancel` 只取消自己的任务，不会删除已创建的 Issue。内部仍使用 UUID 关联 NPC 回复和恢复状态，用户无需输入。

## 安装与配置

将本目录作为 `astrbot_plugin_cnb_bot` 插件放入 AstrBot 插件目录并重载插件。部署时确认 AstrBot 版本为 4.16–4.x，并启用可提供群消息和文件消息的 `aiocqhttp` 适配器。

在插件配置中填写：

| 配置 | 必填 | 说明 |
| --- | --- | --- |
| `group_whitelist` | 是 | 启用功能的 QQ 群号列表。留空表示不启用任何群。 |
| `cnb_repository` | 是 | CNB 仓库路径，例如 `group/repo`。 |
| `cnb_token` | 是 | CNB 访问令牌；需要 Issue 写入和评论读写权限（`repo-issue:rw`、`repo-notes:r`、`repo-notes:rw`）。 |
| `npc_mention` | 是 | CNB Issue 评论中真实可触发 NPC 的提及文本，默认 `@CodeBuddy`。 |
| `npc_author_ids` 或 `npc_author_usernames` | 否 | 作者 username 默认 `CodeBuddy`；若目标仓库实际作者不同，请填写核实过的 ID 或 username，精确匹配。优先使用 ID。 |

插件只接受 HTTPS 的 CNB API 和网页端点。默认地址分别是 `https://api.cnb.cool` 和 `https://cnb.cool`。令牌保存在 AstrBot 插件配置中，不写入报障任务或 Issue。

其余参数可调整日志等待和 NPC 等待时长、评论轮询间隔、ZIP 文件大小上限、群文件下载域名白名单、分析转发单次发送超时及最大重试次数，以及本地历史保留天数。默认日志等待 10 分钟；NPC 最长等待 20 分钟；ZIP 最大 20 MiB；分析转发单次发送超时 30 秒，失败最多尝试 10 次。

插件元数据的 `repo` 目前为空，因为设计材料未提供该插件的实际仓库地址。发布前应填写真实仓库地址。

## 处理流程

1. 发送 `/debug` 创建等待日志的任务，然后上传一个 ZIP 文件。普通群消息不缓存、不收集。
2. 收到同一用户上传的 ZIP 后，插件只检查文件扩展名、下载来源和压缩文件大小，然后将原始 ZIP 作为 Issue 附件上传 CNB；不解压、扫描或脱敏 ZIP 内容。
3. Issue 使用固定标题，仅包含 ZIP 附件链接、文件大小、处理说明和内部追踪编号；不提交问题描述或聊天上下文。升级前任务中的旧描述与聊天快照也不会写入新的 Issue。
4. Issue 创建后，插件在独立普通评论中 @配置的 NPC。插件分页轮询评论，只接受同时满足以下条件的回复：作者 ID 或 username 与配置完全匹配；正文包含该报障的 `[CNB-BOT:<UUID>:FINAL]` 标记；评论时间不早于本次触发。触发提示要求 NPC 将回复分为“一句话描述”和“详细分析”，并区分已确认事实与推测。
5. 找到最终回复后，插件把 Issue 链接以及整理为“一句话描述 / 详细分析”的完整内容放入一个 QQ 合并转发节点，并 @报障用户。旧回复或未按格式回复的 NPC 内容会以原文首句作为描述，并完整保留原文。单次发送有超时限制，网络超时后会重试；如果 AstrBot 在平台已发送、但本地尚未记录时退出或发送确认超时，该转发可能重复发送。转发成功后任务进入“等待用户确认恢复”，此时 Issue 保持打开。
6. 用户按建议处理并确认故障恢复后，在原群发送 `/debug resolve`。插件先核对 Issue 状态，关闭 Issue，再验证已关闭后将任务标为完成；关闭请求失败时会自动重试。`/debug status` 也会检查 Issue 状态。

## 状态与恢复

任务状态包括 `WAITING_LOG`、`PREPARING_LOG`、`CREATING_ISSUE`、`TRIGGERING_NPC`、`WAITING_NPC`、`DELIVERING`、`AWAITING_RECOVERY`、`CLOSING_ISSUE`、`DONE`、`EXPIRED`、`CANCELLED`、`FAILED` 和 `UNCERTAIN`。

状态保存在插件数据目录的 `reports.sqlite3` 中；临时目录 `tmp/` 暂存收到的原始 ZIP，`prepared/` 暂存等待 CNB 上传的原始 ZIP。上传成功、失败或取消后会删除暂存文件。完成任务后，插件会清除 NPC 转发正文及旧版本遗留的问题描述、群上下文字段。终态任务记录默认在本地保留 90 天，之后在插件启动时清理；活跃任务不受该清理影响。数据库会保存平台、机器人、群、用户及会话标识以支持路由和状态查询，这些标识不会写入 CNB Issue。

创建 Issue 或 NPC 评论遇到网络中断或插件重启时，结果可能不确定。为避免重复 Issue 或重复触发 NPC，插件不会盲目重试：Issue 创建阶段会提示管理员按 UUID 到仓库核对；触发评论阶段会先按完整评论正文核对。若状态显示 `UNCERTAIN`，请用 `/debug status` 查看说明。取消不删除可能已经创建的 CNB 内容。

## 安全与边界

- 开始报障时插件会提醒用户：仅提交原始 ZIP，不采集聊天上下文；ZIP 不会解压或脱敏，会直接提交到配置的 CNB 仓库。上传前请确认文件中没有不应提交的内容。
- 只支持单个 ZIP；插件不读取 ZIP 内容。适配器必须向 AstrBot 提供可读取的 `File` URL 或本地文件路径。
- 群文件 URL 仅允许 HTTP(S)，下载前和重定向后都会拒绝解析到内网或保留地址的主机；可配置域名白名单进一步收紧范围。不同 QQ / OneBot 实现的文件通知和链接有效期仍需部署实测。
- Issue 正文不会包含 QQ 群号、QQ 用户号或 AstrBot 会话标识。插件本地数据库会保存用于任务路由的标识，见上方保留说明。
- 回复作者必须先在目标 CNB 仓库中验证再配置。仅凭 UUID 标记不能通过校验；NPC 能否读取 ZIP 附件取决于 CNB NPC 的实际能力。
- 不记录普通群消息，不读取群历史。旧配置中的 `context_message_limit` 和 `context_window_seconds` 已停用。

## 官方接口依据

- [AstrBot 消息事件](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
- [AstrBot 主动消息](https://docs.astrbot.app/dev/star/guides/send-message.html)
- [CNB OpenAPI 文档](https://docs.cnb.cool/zh/develops/openapi.html)（接口路径与请求字段应以目标 CNB 部署的 Swagger 为准）
- [CNB NPC 文档](https://docs.cnb.cool/zh/build/npc.html)

## 本地回归测试

运行 `python3.12 -m unittest discover -s tests -v`。测试使用 AstrBot 接口替身和模拟 CNB 客户端，验证无 @ 命令、用户隔离、ZIP 提交及不上传上下文；不会创建真实 Issue。
