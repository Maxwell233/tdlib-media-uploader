# 遗留代码、冗余测试与防御逻辑复审

基线：`3df491936472c62b42bcbcb6f8272921cfc01bc9`。
工作分支：`audit/cleanup-followup-2026-10-22`。本文件先登记证据，再执行删除。

## 本地、远端及上次审计

- GitHub 连接器与 `git fetch origin` 核对：远端默认分支 main 为 `b3184be`。
- 开始时本地工作区干净，HEAD 与远端 `fix/deepseek-review-2026-10-22` 相同，领先 main 两个提交，没有落后；两提交共改动 55 个文件（+2143/-924）。
- 已读取用户指定聊天 `01a150cb-a634-71c2-bead-eb939fe0b324`，并核查其中的 `1983659` 是当前基线的祖先；上次删除证据和后续精简报告均已纳入当前仓库。本轮不重复计算旧成果。
- 不修改同步的 `sources/`、既有用户数据或其他本地仓库。
- 基线全量：575 项，573 通过、2 项媒体工具测试跳过；同次运行记录分支覆盖及逐测试动态上下文，位于本地临时审计目录。

## Telegram 与 UI 删除前证据

逐符号检查完整 src/tests、隐藏文件、构建 spec、workflow、README、开发文档和 benchmark；检查字符串导入、getattr 分派、导出与 Qt 连接。静态工具或未覆盖本身不作为删除理由。以下行号对应基线。

| 项目 | 删除前证据 | 保留行为 |
| --- | --- | --- |
| `tdlib_common.topic_object`（489） | 全库仅函数定义。真实发送在 `_send_contents` 根据经过校验的 send_target 构造 topic，根本不调用此函数。 | forum/channel 的真实请求构造与测试保留。 |
| `TDJsonClient._journal_target/_journal_items`（1465/1471） | 类的两个私有包装仅定义，无调用、patch 或动态访问。实际人工对账使用 ReconciliationService 自己的同名实现；客户端的 reconcile_inflight 仍实际被调用，保留。 | ReconciliationService 及客户端仍使用的对账接口、状态构造和断点测试保留。只随孤儿包装删除其独占 `_record_target` 导入。 |
| `SendResult.is_resolved`（69） | 全库仅属性定义，无动态读取、测试、文档接口。生产代码直接使用 status，未知结果规则由 map_send_result/engine 执行。 | SendResult 的实际状态、ID 映射及序列化不变。 |
| HeadlessUI 与 GuiConsoleUI 的 `banner/summary/files/groups/cancelled/confirm_upload` | 旧独立控制台上传流程已在此前审计删除。两类方法全库只有定义；实际 GUI 日志/鉴权/进度分派只使用 log/info/success/warning/error/prompt/target/album/progress/finish 等现役方法。无 Qt connect 或字符串分派访问这六项。 | 保留实际日志、目标、媒体组、进度、结束与鉴权方法；GuiConsoleUI._message 中只被孤儿 banner/summary 产生的两种等级映射随之移除。 |
| `TDJsonClient._validate_input_message_contents`（1381） | 唯一调用是同一类 `_send_contents`；正文只转发至现役模块函数 validate_input_message_contents，没有重写者或测试 patch。 | 改为直接调用同一模块函数；预提交验证及全部断言保留。 |
| 原生等待调用的 `call_supported`（1644） | 真实对象唯一实现 wait_for_send_results 已接受 on_result/chat_id/on_messages。此处过滤参数只服务 3 处较窄测试替身；它不是外部插件入口。 | 直接调用完整签名；3 处替身接受额外关键字，原输入、结果和断言不变。其他仍有注入调用者的 core.compat 保留。 |

`validate_input_message_contents` 内 photo/video 两分支的 thumbnail 检查完全相同，均先检查主文件、再检查可选缩略图。合并为一个公共检查，video 的 cover 检查保持在后，接受/拒绝规则和异常类型不变。

## 并行审计证据

- [配置、GUI、通用工具](AUDIT_FOLLOWUP_GUI_2026-10-22.md)
- [媒体、上传与文件系统](AUDIT_FOLLOWUP_MEDIA_2026-10-22.md)
- [测试合并及断言映射](AUDIT_FOLLOWUP_TESTS_2026-10-22.md)

## 最终结果

### 清理结果

本轮生产 Python 源码净减 **271 行**，测试代码净减 **81 行**，配置模板净减 3 行；不把先前审计成果计入本轮。

- 删除无调用的就绪/并发/文件重试包装、孤儿扫描别名、旧停止转发和信号、卡片 setter、Telegram 包装及旧控制台展示方法。
- 删除无效的文件列表开关、“FFmpeg 压缩超时”设置及图片目标常量；用户现有配置文件不被改写。
- 去掉不存在实现的 stage_plan 候补、缺失固定依赖 API 的猴补、图片双参数别名及编码后不可达的重复限额检查；缩略图验证和图片格式分类合并等价分支。
- 已构造属性改为直接读取，实际任务互斥、取消、源快照、缓存所有权、UNKNOWN 防重复和持久化保护全部保留。
- 仅合并 1 个同一生产函数的重复测试方法：6 项重复断言保留一份，7 项独有断言全部迁入保留方法。其他 574 个原方法保留，未新增测试方法。
- 8 个测试文件去掉 84 个闲置导入绑定，除顶层导入之外 AST 完全不变；Qt 初始化副作用所需导入与 QApplication fixture 保留。
- 本轮未发现并复现新的产品 Bug，没有通过删除失败测试处理问题。

### 全量验证及覆盖比较

基线与最终都从跟踪文件导出独立临时副本，排除本地用户配置和忽略文件，复用同一 Python 3.13 环境、同版覆盖工具和 offscreen Qt。

| 检查 | 结果 |
| --- | --- |
| 基线全量 | 575 项：573 通过、2 项媒体工具测试跳过 |
| 最终全量 | 574 项：572 通过、相同 2 项跳过 |
| 显式提供现有真实 FFmpeg/FFprobe | 上述 2 项媒体集成测试全部通过，无跳过 |
| 源码核心自检 | `SELF-TEST OK`，包含真实媒体工具和 QtSvg 导入检查 |
| 编译、Ruff F821/F822/F823/E9、pip check、diff-check | 全部通过；未引入新的检查门禁 |

| 源码覆盖指标 | 基线 | 最终 |
| --- | ---: | ---: |
| 语句 | 79.48%（11,387 / 14,326） | 79.75%（11,328 / 14,205） |
| 分支 | 62.63%（2,791 / 4,456） | 62.86%（2,777 / 4,418） |

比例提升部分来自删除代码，不单独证明测试更强。逐行映射后，保留代码原先执行过的
**11,310 行和 2,758 条可映射分支全部继续执行，未丢失覆盖**。
测试清单差异只有已登记的一个合并方法；没有删除媒体、取消、断点或失败路径套件。

原始覆盖数据库、JSON、比较结果、全量及真实工具日志保存在仓库外的
`../audit-reports/cleanup-followup-2026-10-22/`，不加入运行依赖和发布内容。

### 保留项及未解决的验证范围

- `core.compat`、实际策略注入、预览 fallback、TDJsonClient 的人工对账兼容入口仍有真实消费者，保留；不按 legacy/phase/V2 命名批量删除。
- 本轮审计没有确认后遗留未修的 Bug。核心自检通过纯离线入口运行；原有发布包启动策略保留。
- 按用户此前要求，不尝试真实 Telegram 登录/发送。持续改写网络盘的实测、交互式桌面验收未执行。
- 本轮未重新执行 Windows/macOS 原生打包。基线 `3df4919` 的两平台构建已通过，但该结果不冒充当前清理后版本的原生包验证。
- 结果保存为本地分支及提交，不推送、合并或发布远端。账号额度检查到五小时已用 84% 时停止扩大范围，未购买额度或使用重置。
