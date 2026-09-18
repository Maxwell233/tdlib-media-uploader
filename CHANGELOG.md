# 更新记录

## 1.9.3-beta3

### GUI 架构全面重构与视觉现代化

- 全面重构 PySide6 前端体系，建立清晰的分层架构（`theme` 设计系统、`components` 可复用组件、`dialogs` 模块化对话框、`pages` 独立功能页面）。
- 引入现代设计令牌系统（Palette）与自适应 QSS 样式表，提供统一配色、圆角卡片、呼吸状态徽章与清晰视觉层次。
- 侧边栏重构为品牌与状态集成的 `NavigationSidebar`，包含版本标识、活跃路由高亮以及 Telegram 底栏实时连接状态指示。
- 彻底移除 `main_window.py` 中历史遗留的重复代码与废弃实现（包括 `_LegacyUploadPage`、内嵌对话框及死代码），代码量精简收敛，职责聚焦于主窗口生命周期调度与工作线程协调。

### 页面与对话框模块化

- **概览看板（HomePage）**：全面升级为仪表盘架构，包含状态统计卡片、任务快捷入口与操作提示卡片。
- **任务中心（TaskPage）**：优化双进度条呈现（总进度与当前媒体组进度），实时展示动态上传速度、预估剩余时间（ETA）、格式化终端日志及安全停止/立即中断操作。
- **媒体上传页面（UploadPage / Video / Image / Mixed）**：统一源目录选择条、Telegram 目标卡片、搜索筛选工具栏、媒体组预览树与标题编辑交互。
- **未确认上传管理（InflightPage）**：9 列完整状态表格展示、对齐状态中文解析、详情抽屉与安全断点修复 / 确认未发送对账操作。
- **历史记录中心（HistoryPage）**：统计指标卡片、多字段搜索过滤、分页表格与清空历史操作。
- **系统设置与诊断（SettingsPage）**：运行环境依赖状态卡片、多组配置直达入口、数据与日志诊断路径、缓存与断点维护操作。
- **对话框体系解耦**：独立提取 `CaptionEditDialog`（多行编辑与实时字数计数）、`TargetDialog`（目标参数配置与分组规则）、`ConfigDialog`（基础配置与代理）、`ScanToolsDialog`（ExifTool、并发与超时配置）。

### 兼容性与质量保障

- 严格保持 Beta 2 底层数据结构与上传引擎完全兼容（`config.toml`、`history.json`、断点状态、`InflightJournal` 及缓存目录无缝读取）。
- PyInstaller 构建配置（`tdlib_media_uploader.spec`）同步覆盖所有新增 GUI 模块与组件。
- 新增针对 Beta 3 GUI 组件、设计主题、对话框及架构边界的自动化测试，全量 254 项基线测试 100% 保持通过。

## 1.9.3-beta2

### 上传安全与停止语义

- 未确认上传页面改为显示文件名/Album 摘要、源目录、文件数量、Telegram 目标、更新时间和错误；详情对话框保留全部源路径，旧版缺少 `items` 的记录显示明确 fallback。
- `CONFIRMED` journal 只能执行本地断点修复，禁止人工标记为“未发送”；启动任务时会优先使用 journal 快照恢复 UploadState，只有 checkpoint 成功后才清理保护记录。
- `CONFIRMED` checkpoint 恢复失败现在按“Telegram 已确认、本地断点待修复”的部分完成结果汇总，GUI 不再把它提示为普通上传失败或暗示可直接重传。
- 损坏或不可读的 journal 现在 fail-closed，保留文件路径和错误信息，不会被静默当作不存在而触发重复上传。
- GUI 区分“安全停止”和“立即中断”：安全停止等待当前 Album 正常完成，立即中断保留 UNKNOWN 保护并提示人工核对。

### 性能与测试

- InflightJournal 保留 direct-path 快速查找，并为 legacy/filename fallback 增加进程内 lazy index；写入、更新、人工处理和 finalize 会失效缓存。
- Image/Video V2 planner 和 state bridge 使用预构建 MediaItem identity lookup，检测未知项、快照不匹配和 duplicate identity，避免大任务中的重复线性匹配。
- Album 内 UploadState `sent_at` 统一使用一次 UTC 时间戳；移除确认无内部调用的 legacy image `format_duration` helper。
- 新增独立 journal、reconciliation、stop semantics、media lookup 和 inflight GUI 回归测试；保留 unittest/offline 测试结构，未新增上传速度限制。
- Windows 路径断言改用项目稳定路径语义；journal/reconciliation/Inflight GUI 历史 regression 已迁入对应 ownership 文件，未降低覆盖。

## 1.9.3-beta1

### TDLib 与混合上传修复

- 回退 `tdjson`/TDLib Python 绑定到 `1.8.64.post1`，避免 `1.8.67` 在当前上传实现中的兼容性问题。
- 混合上传的视频内容复用统一的视频 payload builder，确保 Photo + Video Album 始终带有有效的 `InputFile`。
- 在写入上传 journal 或发送 TDLib 请求前校验 Photo、Video、缩略图和 cover 的 `InputFile` 结构；空 payload 会明确失败，不再以 TDLib `InputFile is not specified` 的泛化错误结束任务。
- 上传失败诊断逐项记录内容类型、InputFile 类型、源路径存在性和大小，便于定位临时文件消失或构建异常。

### 回归验证

- 新增混合 Photo + Video 序列化、空 InputFile 发送前拦截、journal 不落 PREPARED 以及有效 Album 请求回归测试。
- 通过完整离线测试、PyInstaller Windows x64 与 macOS arm64 构建验证后发布 beta1。

## 1.9.1

### 本次重新发布修复

- ExifTool 批处理按已返回的 `SourceFile` 更新 GUI 进度；warning 不再让完整批次重复二分，只重试真正缺失的文件。
- 扫描取消会以明确的 cancelled 状态返回并显示“扫描已取消”，不会再把部分扫描伪装成 0 个文件的正常结果。
- 修复 `read_dates = false` 时登录成功后上传准备阶段调用 `None.strftime()` 的崩溃；未读取日期的视频继续保持 `capture_time = None`。
- ExifTool 路径保存前增加短时 `-ver` 校验，并拒绝后台不可用的 `exiftool(-k).exe`。
- 增加 623 文件进度、warning/缺失文件隔离、取消、FFmpeg 零调用和 63 个 Album 上传准备回归测试。

### 界面

- 设置与诊断页面支持滚动，用户数据目录和运行日志并排显示，避免小窗口中的内容重叠或截断。
- 编辑配置与扫描/外部工具设置分为两个入口，扫描稳定性、ExifTool、FFmpeg 超时和批次设置单独编辑。

### 数据与状态可靠性

- 用户数据统一收敛到 `data/`；Telegram 登录数据库和文件统一进入 `data/telegram/database` 与 `data/telegram/files`。
- video、image、mixed 共用媒体 identity 和 UploadState；Album identity 使用扫描 snapshot 并包含 source-root scope。
- Telegram target identity 统一 canonicalize；UNKNOWN journal 会阻止不确定发送结果自动重复上传。

### 网络盘和扫描

- SMB/NAS 扫描支持有界 retry、`scandir` 枚举中断恢复和多次 readiness 稳定快照检查。
- symlink/junction 不跟随；staging 使用带 marker 的受管目录，cleanup 不会删除非程序文件，并对 symlink、junction 和其他 reparse point fail closed。

### Telegram 上传

- Album 部分成功、失败、取消或超时进入 UNKNOWN，不自动整组重发。
- 普通账号视频精确限制为 `4000 × 524288 bytes`，Premium 精确限制为 `8000 × 524288 bytes`。
- Caption 长度按 TDLib 当前 `message_caption_length_max` 检查；single-instance 防止多个进程同时访问 TDLib、state 和 journal。

### 排序与 Album

- video、image、mixed 共用自然数字排序；数字按整数升序，多级路径逐 component 比较。
- mtime 模式按修改时间从旧到新，mtime 相同时使用同一自然路径排序作为稳定 tie-breaker。
- 视频日期只用于月份分组，不覆盖组内排序；完整 Album plan 在 preflight 前固定，deferred 文件不会导致后续文件补位或重新分组。

### 构建与验证

- Windows x64 与 macOS arm64 均通过 offline regression、PyInstaller 构建、打包 `--self-test`、包验证和制品上传。
- Windows 自检配置 UTF-8 输出并安全处理不可表示字符；macOS DMG 针对偶发的 `hdiutil Resource busy` 增加有界重试。
- `VERSION` 作为唯一版本来源，构建包文件名、DMG 卷标和发布标题均从该文件读取。

## 1.9.0

### 本次可靠性与排序修复

- 文件名和目录排序统一按自然数字比较并按数值从小到大排列；例如 `x.41` 会排在 `x.409` 和 `x.410` 前，视频、图片和混合模式使用同一规则。
- 混合模式只把一级子文件夹作为组，根目录直接放置的媒体会忽略并提示；图片和视频扩展名直接继承各自配置。
- 混合、视频和图片的 Album 计划先基于完整扫描建立，预检或上传前暂时不可读的文件不会让后续文件向前补位。
- 新增文件快照、稳定性检查、读探针和有限重试；ExifTool 明确读取扫描所得文件列表，异常批次会重试并二分隔离，FFmpeg 媒体信息、封面和图片压缩增加超时。
- 可选启用本地暂存，把 SMB/NAS 源文件在发送前复制到 `data/cache/staging` 的受管目录；只清理程序自己的暂存副本。
- 目录发现阶段新增可取消的 `scandir/stat` 重试与退避；本地和网络盘可分别配置稳定性检查次数、间隔及 I/O 并发。
- 外部进程统一使用可取消通信封装，持续排空 stdout/stderr，支持超时、优雅终止和强制回收，避免大批量 ExifTool/FFmpeg 管道阻塞。
- 新增 `data/upload_inflight` 发送日志；Album 在断点持久化前保持 `CONFIRMED` 记录，超时、断线、取消或部分成功进入 `UNKNOWN`，避免下次静默重复上传。
- 未确认上传记录现在保存源文件快照并绑定 Telegram 目标；人工确认“已发送”会先写入对应断点，成功后才清理日志，GUI 侧栏提供带二次确认的处理入口。
- 扫描根目录和 `scandir` 枚举中途的网络错误会按瞬时/永久错误分类并有限重试；稳定性检查严格遵守配置的间隔，并发预检在保持输出顺序的同时继续补充后续任务。
- 本地暂存支持 `off`、`network`、`always` 三种模式，成功后清理与旧版 `enabled` 开关均可配置；新增 ExifTool 批次大小和重试次数设置。
- Windows 打包自检会在输出中文路径前配置 UTF-8 编码并用替换策略处理不兼容字符，避免控制台代码页导致自检失败。
- macOS DMG 创建针对 runner 偶发的 `hdiutil Resource busy` 做有限重试；其他磁盘映像错误仍会立即失败并保留诊断信息。

### 混合上传

- 新增独立“混合上传”页面：按 `mixed_dir` 下的一级子文件夹建立组，递归收集图片和视频，并在同一个 Telegram Album 中混合发送。
- 每组数量可设为 1–10；超过上限会拆分为多个 Album，默认使用文件夹名称作为组标题。
- 新增 `[telegram.mixed]`、`[mixed]` 配置，以及独立的 `data/state/mixed` 断点和 `data/captions/mixed.json` 标题存储。
- 混合模式支持文件名与文件名序号开关，文件名 Caption 统一只保留 stem，不显示最后的扩展名。

### 扫描稳定性

- ExifTool 现在读取 Python 已确认的文件列表，使用 UTF-8 参数流和有界批次；空输出、超时、异常 JSON 或网络盘瞬时失败会重试并隔离到单文件，不再让整次扫描失败。
- 视频、图片和混合上传在预检及实际上传前都会重新验证文件可读性和扫描后的大小/修改时间变化；暂时不可读项目标记为可重试的 deferred 状态。
- GUI、任务中心、历史记录、缓存清理、目标配置和预览全部改为显式支持视频/图片/混合三种类型。

### 兼容性与验证

- V1.9 使用全新的 `data/` 用户数据布局；复制整个 `data/` 即可备份或迁移 V1.9 数据，不读取旧版散落状态文件。
- TDLib 登录数据库和文件缓存统一放在 `data/telegram/database`、`data/telegram/files`，不再写入程序资源目录或 PyInstaller `_internal`。
- 增加混合扫描、混合 Photo + Video 内容、stem 文件名、ExifTool 空输出和文件稳定性回归测试。
- 增加自然路径排序、扫描重试、外部进程大输出/取消回收、暂存生命周期和发送日志回归测试；排序规则保持目录优先、数字升序。
- 修复 GUI 中“文件名带序号”选项在关闭文件名列表时被禁用、无法取消选中的问题；现在视频和混合上传都可独立保存该格式设置。

## 1.8.11

### 扫描与预检性能

- 合并 PR #11：mtime 排序改用 `os.stat`，减少大量文件排序时的路径对象创建开销。
- 目录扫描改为有界的迭代式 `os.scandir` 遍历，保留扩展名筛选和网络目录容错，不受嵌套目录深度影响。
- 图片和视频预检保持并发处理，同时限制排队任务数量，避免大目录一次性创建大量 Future。
- 并发预检使用线程安全的媒体信息缓存，减少重复的 Pillow/FFmpeg 读取。
- 缓存统计只读取一次目录项属性，并跳过符号链接和 Windows junction，减少 I/O 并避免意外遍历外部目录。

### 兼容性与验证

- 保留 `foo.`、`foo..` 和 `.hidden` 等文件名与旧版 `Path.suffix` 一致的扩展名行为。
- 增加 mtime 成功/回退路径及扩展名边界的离线回归测试。
- ExifTool 批量读取通过 UTF-8 参数流传递扫描目录，并启用 Unicode 文件名处理，修复 Windows 中文/特殊字符目录导致的日期扫描失败。
- 新增“读取视频日期信息”开关；关闭后跳过 EXIF、媒体创建日期和 mtime，按文件名排序并固定分组，只读取文件名和文件大小。
- 发布版本统一更新为 1.8.11，Windows/macOS GitHub Actions 继续使用同一套源码构建。

## 1.8.10

### 视频分组与标题

- 视频固定分组改为可选择 1–10 个一组，保留按日期分组模式并兼容旧版 `force_ten_per_album` 配置。
- 新增组标题、文件名列表、文件名序号三个独立选项，集中放入“视频分组与标题”设置区。
- 新增视频扫描排序，可在修改时间和文件名之间二选一；固定分组会按所选顺序取视频。
- 重排视频目标与配置窗口，按上传目标、日期读取、视频分组与标题、上传前处理分区。

## 1.8.9

### 日期读取与扫描速度

- 日期优先级固定为 EXIF → 可选的媒体创建日期 → 文件修改日期，修复媒体日期可能抢在 EXIF 前面的情况。
- 新增 `read_media_creation_date` 配置和 GUI 开关；关闭后跳过媒体创建日期字段，保留 EXIF 和 mtime 逻辑。
- 保留 ExifTool 一次批量读取；已有 EXIF/内嵌日期的视频直接使用日期，不启动 FFmpeg。
- 缺少可用日期的视频最多同时读取 4 个文件，每个文件只启动一次 FFmpeg，并显示媒体日期扫描进度。
- 关闭 `read_media_creation_date` 后跳过 FFmpeg 日期回退，直接按缺失日期策略处理。
- 预览文件的日期列提示会显示实际日期来源，便于确认使用的是 EXIF、媒体日期还是 mtime。

## 1.8.8

### 跨平台发布与上传限制

- macOS Apple Silicon 改为构建 `.app` 后制作 DMG；DMG 内含 `Applications` 文件夹别名，方便拖放安装。
- 视频超过约 4 GB、图片超过 10 MiB 时在扫描阶段提示并记录日志；视频上限按 Telegram 的精确字节边界计算，超限视频跳过，超限图片默认跳过。
- 可选使用已有 LGPL FFmpeg 压缩超限图片；压缩延迟到用户确认上传且实际处理图片时，原文件不修改，失败按单图记录并跳过。
- Windows 构建校验 EXE 嵌入图标、Qt 图标资源和稳定 AppUserModelID，降低打包后任务栏图标缺失风险。
- README 补充 macOS DMG 使用方式、下载校验、未签名/未公证提示和媒体处理风险。

## 1.8.7

### 稳定性与诊断

- 上传前逐个检查待上传的视频媒体信息和封面；无法读取的视频会被跳过，其他视频继续上传。
- 上传过程中才变得不可读的媒体也会从当前 Album 隔离，避免单个坏文件中断整批任务；图片上传采用同样的处理。
- 跳过文件的完整路径、异常原因和任务结果会写入持久化 `logs/app.log`；TDLib 原生日志写入 `logs/tdlib.log`。
- “清理所有缓存”现在同时删除持久化日志；日志目录和应用数据目录结构会保留。
- 修复损坏或未完整写入的封面缓存自动阻塞后续上传的问题，并保留 FFmpeg 错误摘要。

### 发布与许可

- Windows 构建验证打包后的 Qt/任务栏图标仍存在，并保留稳定的 Windows AppUserModelID。
- 项目原创代码、文档和界面资源改用 GNU GPL v3.0 only（GPL-3.0-only）；第三方组件仍遵循各自上游许可证。

## 1.8.5

### 跨平台发布

- Windows x64 与 macOS Apple Silicon arm64 使用同一套 GUI、上传核心和配置功能，并在 GitHub Actions 中并行构建。
- 新增 macOS `setup.sh` / `run.sh` / `.command` 入口和 `TDLib Media Uploader.app` 打包流程。
- macOS 发布版把配置、TDLib 登录数据库、断点、标题和缓存放到用户 Application Support 目录，避免写入只读 `.app` 包。
- macOS 构建从 FFmpeg 7.1.1 官方源码生成 LGPL arm64 FFmpeg，排除 `imageio-ffmpeg` wheel 自带二进制，并检查 GPL/nonfree 构建标志。
- Release 同时提供 Windows x64 ZIP、macOS arm64 ZIP 和 `SHA256SUMS`，并附带项目许可、署名与第三方组件清单。
- README 增加官方下载核验、未签名/未公证提示、个人凭据保护和 Telegram 使用风险说明。

### 兼容性

- TDLib `system_version` 按当前操作系统设置，不再把 macOS 设备报告为 Windows。
- 配置路径支持 `~` 和环境变量；示例路径与 ExifTool 提示改为跨平台写法。
- macOS 启动前清除 Qt 插件可能携带的隐藏文件标记，避免 `cocoa` 平台插件无法加载。
- Windows 启动前设置稳定的 AppUserModelID，并继续写入 EXE/Qt 图标，改善任务栏图标显示与窗口归组。

## 1.8.4

### 操作与文案

- 图片预览移除重复的媒体组父行；视频保留月份分组。
- 新增文件/路径/标题搜索和“只看待上传”。筛选仅影响显示。
- 标题编辑合并为一个窗口，支持多行文字和实时预览；增加明确的编辑按钮。
- 目录支持粘贴后按 Enter 保存，选择目录后直接保存。
- 日期与排序选项使用中文说明，保留原配置值。
- README 按首次使用、上传规则、配置和源码运行重新整理，移除旧终端操作及重复说明。

### 效率

- 每次构建上传计划只解析一次标题 JSON；保存时仍重新读取磁盘以保留其他已有记录。
- 视频预览一次构建所有月份计划，减少重复读取标题数据。
- 目录枚举对每个匹配文件只读取一次属性；预览内复用断点检查结果。
- 保存标题只更新当前行，保留展开状态，避免重建整个文件列表。

### 正确性

- 修改配置、重新扫描或完成任务后清除旧预览，防止旧计划继续启动。
- 扫描期间禁止启动上传；扫描/上传期间禁止修改配置及相关标题。
- 配置使用临时文件替换；加载失败时恢复原配置。
- 非法 API ID 显示错误，不再静默替换成示例值。
- 无媒体文件时显示明确提示，标题保存失败时保留错误信息。

保留原有视频/图片上传、月份/十个一组、标题与文件名清单、封面、独立目标、代理、断点、缓存管理和历史记录。

测试：`python -m unittest discover -s tests -v`。测试使用临时数据，不登录 Telegram，也不向群组发送消息。
