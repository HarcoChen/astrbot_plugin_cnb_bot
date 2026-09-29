# 配置说明

所有配置都在 AstrBot 面板的插件配置页里修改。

## CNB 连接

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `cnb_repository` | 空 | 仓库路径，如 `group/repo`。必填 |
| `cnb_token` | 空 | 访问令牌。必填。权限见 [接口说明](api.md#令牌权限) |
| `cnb_api_endpoint` | `https://api.cnb.cool` | OpenAPI 地址，只接受 HTTPS |
| `cnb_web_endpoint` | `https://cnb.cool` | 网页地址，用来拼 Issue 链接，只接受 HTTPS |

## NPC

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `npc_mention` | `@CodeBuddy` | 写进 Issue 评论、用来触发 NPC 的文本 |
| `npc_author_ids` | 空 | NPC 回复作者的用户 ID。比 username 更可靠，优先填这个 |
| `npc_author_usernames` | `CodeBuddy` | NPC 回复作者的 username，精确匹配 |

两个作者字段至少要有一个非空。插件只认这里列出的作者发的回复，所以请先在仓库里看一眼 NPC 实际是用哪个账号回复的。

## 谁能用

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `group_whitelist` | 空 | 允许报障的群 ID。OneBot 填 QQ 群号；QQ 官方机器人填群 OpenID。空表示所有群都不启用 |
| `private_whitelist` | 空 | 私信白名单。空表示所有人都能私信报障，填了就只允许名单里的用户 |
| `reply_in_disabled_groups` | 关 | 未启用的群里有人发 `/debug` 时是否回复提示。关闭时插件完全不响应，方便和其他插件的 `/debug` 指令共存 |

## QQ 指令面板

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `qq_command_panel_enabled` | 关 | 开启后，插件启动时自动创建或更新单聊和群聊的全局指令面板 |
| `qq_command_panel_app_id` | 空 | QQ 开放平台机器人的 AppID |
| `qq_command_panel_client_secret` | 空 | 同一机器人的密钥；在 AstrBot 插件配置中以密钥字段保存 |

面板包含 `/debug`、`/debug status`、`/debug analyze`、`/debug resolve`、`/debug cancel` 和 `/debug help`。面板同步仅调用 QQ 开放平台 API；未启用时不会请求该 API。OneBot/aiocqhttp 无需配置面板凭据，继续使用聊天指令。官方机器人群面板对所有群可见，实际能否报障仍由 `group_whitelist` 控制。

## 用户看到的文字

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `assistant_name` | `分析助手` | 聊天里对 NPC 的称呼，合并转发的发送者名也用它 |
| `log_location_hint` | 空 | 开始报障时附上的一句提示，告诉用户日志在哪。比如：`日志在程序目录的 debug 文件夹里，整个打包成 zip 上传即可。` |

## 时间与大小

| 配置 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `log_wait_minutes` | 10 | 1–1440 | 发起报障后，等用户上传日志的时间 |
| `analysis_wait_minutes` | 20 | 1–1440 | 等 NPC 给出结果的最长时间，超时后报障失败，Issue 保留 |
| `recovery_confirm_minutes` | 30 | 1–10080 | 结果发出后，等用户确认是否解决的时间。超时后插件会在 Issue 里留言说明，再关闭 Issue |
| `max_log_file_mib` | 20 | 1–1024 | 单个日志文件的大小上限 |

## 轮询与重试

一般不需要改。

| 配置 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `poll_interval_seconds` | 10 | 5–120 | 多久查一次 Issue 评论。CNB 请求失败时间隔翻倍，最长 120 秒 |
| `issue_check_interval_seconds` | 30 | 5–300 | 等待用户确认期间，多久同步一次 Issue 的开关状态（`/debug status` 会显示） |
| `delivery_send_timeout_seconds` | 30 | 1–300 | 单次发送合并转发的超时时间 |
| `max_delivery_attempts` | 10 | 1–100 | 合并转发最多尝试几次，都失败后报障标记为失败 |
| `file_url_host_allowlist` | 空 | | 只允许从这些域名下载日志文件。留空时仍然会拦截内网地址 |
| `history_retention_days` | 90 | 1–3650 | 已结束的报障在本地数据库里保留多少天 |

超出范围的值会被截到边界上。

## 从旧版本升级

0.2.0 及以前的版本里，这几个配置项用的是秒和字节，现在改成了分钟和 MiB：

| 旧配置 | 新配置 |
| --- | --- |
| `log_wait_seconds` | `log_wait_minutes` |
| `analysis_wait_seconds` | `analysis_wait_minutes` |
| `max_archive_bytes` | `max_log_file_mib` |

旧配置项还在，只是在面板里隐藏了，所以之前改过的值不会丢。规则很简单：如果新配置项还是默认值，而旧配置项被改过，就沿用旧值；你在面板里改了新配置项之后，就以新值为准。

更早版本里的 `context_message_limit` 和 `context_window_seconds` 已经删除，插件不再收集群聊上下文。
