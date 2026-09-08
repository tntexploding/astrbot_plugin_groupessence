# AstrBot 远端部署与验收

本项目可直接作为 AstrBot 插件安装。插件复用 AstrBot 已经建立的
NapCat/AIOCQHTTP（OneBot v11）连接：手动命令通过当前消息事件调用 OneBot Action，
可选计划任务按平台 ID 动态取得同一客户端；不启动 FastAPI/Uvicorn，不新增监听
端口，也不读取 `ONEBOT_BASE_URL` 或 `ONEBOT_ACCESS_TOKEN`。

## 1. 部署前检查

1. 确认远端 AstrBot 已能通过 NapCat 正常收发测试群消息。
2. 备份 AstrBot 数据卷，尤其是已有的 `plugin_data/`。
3. 确认测试管理员 QQ 号和一个测试群号，暂时不要加入更多群。
4. 保持 NapCat 和 AstrBot 的现有连接配置不变。

插件运行路径只使用 Python 标准库与 AstrBot 提供的 API，根目录
`requirements.txt` 没有第三方依赖。独立 CLI/API/OCR 所需依赖不应安装进 AstrBot
容器来完成本轮验收。

## 2. 安装插件

优先在 AstrBot WebUI 的插件管理页使用仓库地址安装：

```text
https://github.com/tntexploding/astrbot_plugin_groupessence
```

如果当前 AstrBot 版本只支持上传插件压缩包，压缩包根目录必须直接包含：

```text
main.py
metadata.yaml
_conf_schema.json
requirements.txt
src/
```

不要只上传 `src/group_essence_extractor/`，也不要在插件目录中创建 `.env`。安装或更新
后，通过 WebUI 重新加载插件；若热重载未生效，再重启 AstrBot 容器一次。

### 从 0.4.x 升级到 0.5.0

0.5.0 将插件标识从 `astrbot_plugin_group_essence` 调整为分发名称
`astrbot_plugin_groupessence`。升级前先禁用旧插件并备份 AstrBot 数据卷，不要让两个
插件目录同时启用。

AstrBot 按插件标识保存配置，因此应在首次加载新版本前，将旧配置文件复制为新名称，
同时保留旧文件用于回滚：

```text
data/config/astrbot_plugin_group_essence_config.json
  -> data/config/astrbot_plugin_groupessence_config.json
```

如果目标文件已存在，不要覆盖；应在 WebUI 中逐项核对并迁移管理员、群白名单和计划
任务配置。数据库不需要改名或重新导入：当新目录尚不存在而旧目录存在时，插件会继续
使用旧的 `plugin_data/astrbot_plugin_group_essence/`。若新旧数据目录同时存在，插件
优先使用新目录；此时应保持插件禁用并人工确认哪一份数据库是当前数据，不能合并或
覆盖唯一副本。完成升级后先执行 `/精华状态`，再按本文阶段 A、B、C 重新验收。

## 3. 阶段 A：只读契约验收

先在插件配置中填写：

```json
{
  "validation_mode": true,
  "admin_ids": ["管理员 QQ 号"],
  "allowed_group_ids": ["测试群号"],
  "default_group_id": "测试群号",
  "max_validation_detail_requests": 10,
  "max_sync_detail_requests": 10,
  "history_query_limit": 100,
  "max_query_results": 5,
  "max_content_chars": 300,
  "enable_image_enrichment": false,
  "enable_scheduled_sync": false,
  "onebot_platform_id": "",
  "enable_automatic_backups": false
}
```

`allowed_group_ids` 为空时会拒绝所有群；私聊命令只有在
`default_group_id` 同时位于白名单时才有目标群。当前所有指令都只允许
`admin_ids` 中的账号。

使用测试管理员执行：

```text
/精华状态
/精华验收
```

私聊且未配置默认群时，可显式执行 `/精华验收 测试群号`。验收命令只调用
`get_essence_msg_list`，并且只在正文缺失时用 `get_msg` 补全。验收最多发出
`max_validation_detail_requests` 次详情请求（范围 0–50，默认 10）；它不创建数据库
或数据目录。报告中的详情统计分别表示候选、实际请求、因上限或缺少消息 ID 而跳过、
以及失败的数量。
回复应只有数量、内容类型、缺失字段、字段类型和详情补全计数，不应出现群号、QQ
号、昵称、正文、图片地址或原始响应。

阶段 A 通过条件：

- 精华数量与群内实际情况相符；
- `message_id`、发送时间、精华时间和正文缺失量已被准确报告；
- 单条 `get_msg` 失败不会使整批验收失败；
- 普通聊天仍按原 AstrBot 流程处理，精华指令不会进入 LLM；
- AstrBot 日志只有 action 状态、异常类别和聚合数量，没有隐私正文或凭据；
- AstrBot 与 NapCat 没有持续的 CPU 或内存增长。

NapCat 可能不在精华列表中提供历史 `sender_time`，且旧消息已经无法由 `get_msg`
回查。阶段 A 仍把发送时间保留为质量字段，不触发群历史查询，也不会用
`essence_time` 代填；真实时间补全在阶段 B 由有界群历史匹配完成。

## 4. 阶段 B：启用同步与查询

阶段 A 通过后，将 `validation_mode` 改为 `false` 并重新加载插件，然后执行：

```text
/精华同步
/精华同步
/精华补全时间 100
/精华查询 脱敏测试关键词
/精华最近 5
/精华状态
```

第一次同步可以出现 `新增` 或 `更新`；没有新精华时，紧接着的第二次同步应主要为
`未变化`，不能再次新增同一批记录。OneBot 原始结构或短期图片地址变化只计入
`元数据刷新`。同步的正文详情请求受 `max_sync_detail_requests` 限制；失败消息按独立
截止时间指数退避，到期后重新尝试，避免每次同步重复请求，也不会永久跳过。

对新发现且缺少发送时间的精华，同步最多调用一次 `get_group_msg_history`，并只保留
消息 ID、序号、随机号和时间用于匹配；群历史正文和发送者不会进入时间索引或回复。
历史接口失败不会阻断同步，报告会显示“历史查询失败：是”。已有数据库中的缺失时间
使用 `/精华补全时间 [数量]` 显式处理，数量为 1 到 `history_query_limit`；只更新空值，
按消息 ID、序号与随机号或无歧义序号匹配，无法确定的记录保持为空。

查询始终限定当前授权群，空关键词会被拒绝，“最近”的数量只接受 1–20。

数据库按需创建在 AstrBot 数据根目录下：

```text
plugin_data/astrbot_plugin_groupessence/group_essence.db
```

它不位于插件源码目录，也不使用本仓库的 `data/group_essence.db`。完成一次同步后重启
AstrBot，再执行 `/精华最近 5`，确认数据卷中的记录仍可读取。自 0.4.0 起，首次打开已有
schema v2 数据库时，会先在 `backups/` 创建经 `quick_check` 验证的迁移前快照，再升级
到 schema v3。从 0.4.x 升级且继续使用旧数据目录时，实际路径仍为上一节所列兼容路径。

## 5. 阶段 C：计划同步与自动备份灰度

阶段 B 全部通过后，从 AstrBot WebUI 的消息平台配置中复制目标 AIOCQHTTP 实例的
唯一 ID。它不是 NapCat Token。先只保留一个已验收群，并设置：

```json
{
  "validation_mode": false,
  "onebot_platform_id": "AIOCQHTTP 平台唯一 ID",
  "enable_scheduled_sync": true,
  "scheduled_sync_interval_minutes": 30,
  "scheduled_sync_startup_delay_seconds": 60,
  "scheduled_sync_timeout_seconds": 90,
  "scheduled_sync_jitter_percent": 10,
  "scheduled_sync_failure_threshold": 3,
  "scheduled_sync_retry_base_seconds": 30,
  "scheduled_sync_max_backoff_minutes": 60,
  "detail_retry_base_minutes": 15,
  "detail_retry_max_hours": 24,
  "enable_failure_alerts": true,
  "enable_automatic_backups": true,
  "backup_interval_hours": 24,
  "backup_keep_daily": 7,
  "backup_keep_weekly": 4
}
```

重新加载插件后执行 `/精华状态`，预期“计划同步：运行中”且显示下次运行时间；等待
一个周期后再次检查，应出现上次自动成功时间。后台任务只按白名单群串行执行，不保留
触发事件、不调用 LLM。连续失败达到阈值时仅私聊 `admin_ids` 一次，恢复时再私聊一次；
错误正文、Action 参数和 OneBot payload 不会进入告警。告警发送失败不会阻塞同步，也不会
被标记为已送达；只要失败状态持续，后续降频周期会继续尝试。

运行文件位于：

```text
plugin_data/astrbot_plugin_groupessence/group_essence.db
plugin_data/astrbot_plugin_groupessence/backups/
plugin_data/astrbot_plugin_groupessence/ge_health.json
```

`ge_health.json` 应只含状态、时间、计数和错误类别，不应出现群号、QQ 号、正文、URL
或 Token。自动备份使用 SQLite 在线备份 API；不要用直接复制运行中数据库文件替代。
完成至少一个同步周期和一个备份周期前，不增加白名单群数量。

## 6. 故障定位

0.5.2 起，每次同步输出一条脱敏的“GroupEssence 同步耗时”日志，包括等待写操作队列、
精华列表 Action、受限详情 Action、标准化、数据库读取、历史补全和持久化的毫秒数。
失败或取消也记录已消耗的阶段时间；取消日志结合调度器的 timeout 类别判断超时。
日志不包含群号、QQ 号、正文、图片 URL 或凭据。只读查询使用独立 SQLite 连接，
不等待 OneBot 网络同步；同步、验收和修复操作仍串行。

若 list_ms 接近总耗时，瓶颈在 OneBot/NapCat 列表请求，不要据此优化数据库或增加并行同步。
NapCat 的全量列表 Action 可能在内部对每条精华查原消息，这不受 GE 的详情请求上限控制。
可按实测耗时留出同步超时余量，并适当延长重试间隔，降低超时请求仍在上游运行时的叠加风险；
这不能替代上游接口优化，也不能保证上游已取消。不要只提高告警采样次数掩盖真实同步失败。

远端反馈只保留以下信息：

- AstrBot、NapCat 和插件版本；
- 失败的指令和 action 名称；
- OneBot `status`、`retcode` 及脱敏 wording；
- 采集数量、缺失计数、详情补全失败计数；
- 计划任务是否运行、连续失败数、下次运行时间和最近备份时间；
- 时间补全扫描、匹配、更新和剩余数量；
- 异常类别和数据库 schema 版本。

不要复制 Access Token、Cookie、完整 QQ 号或群号、昵称、消息正文、图片 URL、原始
OneBot JSON、数据库路径或数据库文件。插件没有单独的 HTTP 地址；若
`get_essence_msg_list` 不可用，应先检查 AstrBot 当前 AIOCQHTTP 适配器和 NapCat
版本，而不是为插件新增 HTTP 监听。

## 7. 回滚

1. 在 AstrBot WebUI 禁用插件，确认普通对话与其他插件恢复正常。
2. 保留 `plugin_data/astrbot_plugin_groupessence/`；若由 0.4.x 升级，也保留兼容使用的
   `plugin_data/astrbot_plugin_group_essence/`，不要通过删库解决兼容问题。
3. schema v3 迁移前快照位于 `backups/pre-migration-*.db`；先保留当前数据库与全部
   备份，再决定是否恢复，不要覆盖唯一副本。
4. 回退插件版本；重新启用前确认旧版支持数据库当前的 `PRAGMA user_version`。
5. 如需采集现场信息，只复制脱敏后的聚合统计，不复制数据卷内容。

当前 schema 版本由核心迁移代码管理。更新插件不会自动删除数据；禁用插件也不会
删除持久化目录。

## 8. 本轮边界

远端必须按阶段 A、B、C 依次灰度；新安装和升级后计划同步、自动备份默认保持关闭。
当前发送时间修复只检查单次有界群历史，不自动分页追溯全部历史。截图 OCR、批量图片
归档和普通成员查询仍保持关闭。0.5.1 的授权查询可按需下载并回传图片（默认最多 5 张），
不调用 OCR/LLM，不要求修改 `enable_image_enrichment`。独立 CLI/API 只用于离线维护；只有未来出现
AstrBot 之外的远程调用方时，才评估部署额外服务。

### 0.5.1 图片回传验收

保留已有配置、数据库和备份；升级前备份 AstrBot 状态，确认只加载一个 GE 副本。
升级不迁移数据库。版本应为 0.5.1，`max_reply_images` 缺省为 5（0 关闭，最多 10）。
查询已同步的纯图片、图文、多图片精华：文字摘要先到，随后图片标注对应记录和图片序号。
云端统一路由继续使用 `/ge recent 5` 或 `/ge search 关键词`；私聊查询另一授权群时显式带群号。
重复查询应命中缓存；失效来源或超限图片必须给出脱敏提示，不能吞掉文字、阻塞事件循环
或进入 LLM。普通成员和未授权群不得触发下载，群内查询仍不能跨群。
图片缓存位于原插件数据目录 `reply_images/<群号>/`，限制为 128 MiB / 256 个文件，
不在仓库中保存图片，也不新增 Docker 挂载、HTTP 服务或图片识别依赖。
