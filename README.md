# CNB 报障助手

一个 AstrBot 插件。用户在 QQ 群或私信里发 `/debug`、传一份日志，插件会在 CNB 仓库开一个 Issue，让仓库的 NPC（比如 CodeBuddy）分析，再把结论发回聊天里。

<p align="center">
  <a href="https://github.com/AstrBotDevs/AstrBot">
    <img src="https://img.shields.io/badge/AstrBot-Plugin-635BFF" alt="AstrBot Plugin">
  </a>
  </a>
  <a href="https://github.com/HarcoChen/astrbot_plugin_cnb_bot/commits">
    <img src="https://img.shields.io/github/last-commit/HarcoChen/astrbot_plugin_cnb_bot" alt="Last Commit">
  </a>
  <a href="https://github.com/HarcoChen/astrbot_plugin_cnb_bot">
    <img src="https://img.shields.io/github/license/HarcoChen/astrbot_plugin_cnb_bot" alt="License">
  </a>
  </a>
</p>

> [!TIP]
> Koishi版本见(CNB-Bot-Koishi)[https://github.com/HarcoChen/cnb-bot-koishi]

一次完整的报障大概是这样：

```text
用户    /debug 启动后闪退
机器人  请在 10 分钟内由你本人在本群上传一个 .zip 或 .log 日志文件。
        上传后会创建 Issue 并请分析助手分析；之后可 @我 补充信息。
用户    [上传 debug.zip]
机器人  日志已提交，分析助手正在分析，通常需要几分钟（最长 20 分钟）。
        Issue：https://cnb.cool/group/repo/-/issues/42
        ……几分钟后……
机器人  [合并转发] 分析助手分析结果
机器人  @用户 结论：配置文件路径错误。
        问题解决了吗？
        · 已解决：发送 /debug resolve
        · 没解决：@我 补充现象，再发送 /debug analyze 重新分析
用户    /debug resolve
机器人  已确认解决，报障结束，Issue 已关闭。感谢反馈！
```

## 环境要求

- AstrBot 4.16 及以上的 4.x 版本
- `aiocqhttp`（OneBot v11）适配器，需要能收到群文件消息
- 一个 CNB 仓库，并且仓库里配好了能被 @ 触发的 NPC



## 最少要填的配置

| 配置 | 说明 |
| --- | --- |
| `cnb_repository` | 仓库路径，如 `group/repo` |
| `cnb_token` | CNB 访问令牌，需要 `repo-issue:rw`、`repo-notes:r`、`repo-notes:rw` |
| `group_whitelist` | 允许报障的 QQ 群号；只用私信的话可以留空 |
| `npc_author_usernames` | NPC 回复作者的 username，默认 `CodeBuddy`，请按仓库里实际的回复账号填写 |

私信默认对所有人开放。其余配置项见 [配置说明](docs/configuration.md)。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/debug [描述]` | 开始报障。描述会作为 Issue 标题，可以不写 |
| `/debug status` | 查看进度 |
| `/debug analyze` | 补充信息后，让 NPC 重新分析 |
| `/debug resolve` | 确认问题已解决，关闭 Issue |
| `/debug cancel` | 取消报障，已创建的 Issue 会保留 |
| `/debug help` | 列出指令 |

这些子命令也能用中文：`状态`、`分析`、`已解决`、`取消`、`帮助`。

Issue 创建后，想补充情况时，群里 @机器人 发文字即可，私信里直接发。文字会作为评论写进 Issue。群里其他没有 @机器人 的消息，插件不会记录。

## 文档

- [配置说明](docs/configuration.md)：全部配置项、取值范围，以及旧版本升级说明
- [工作流程](docs/how-it-works.md)：报障的各个阶段、状态、超时，以及插件重启后怎样恢复
- [接口说明](docs/api.md)：用到的 CNB OpenAPI 与 AstrBot 接口，以及 NPC 回复的匹配规则
- [隐私与安全](docs/privacy.md)：会提交哪些内容，本地保存哪些数据
