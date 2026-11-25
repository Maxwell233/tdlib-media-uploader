# TDLib Media Uploader

**V1.9.7 · Windows x64 + macOS arm64 桌面应用**

把本地或网络目录中的视频、图片批量上传到 Telegram 群组话题或频道。Windows x64 与 macOS Apple Silicon arm64 使用同一套 GUI、上传核心和功能配置；支持上传预览、标题编辑、断点恢复和独立代理，上传时无需打开 Telegram Desktop。

## 开始使用

已发布的便携包在 [Releases](https://github.com/Maxwell233/tdlib-media-uploader/releases)。Windows 下载 x64 ZIP，完整解压后运行 `TDLib Media Uploader.exe`；macOS 下载 Apple Silicon arm64 DMG，打开后将 `TDLib Media Uploader.app` 拖到其中的 `Applications` 文件夹，再从“应用程序”启动；两者都无需安装 Python。下载后可用 Release 中的 `SHA256SUMS` 校验 ZIP 和 DMG；源码中的修改可能尚未打包发布。

macOS 首次打开若出现“无法验证开发者”等提示，请先确认下载地址和 SHA-256，再在 Finder 中右键点按应用并选择“打开”。当前包没有 Apple Developer 签名/公证，见下方[信任与风险](#信任与风险)。

1. 打开“设置与诊断”，在左侧各分类（常规、Telegram、上传参数、高级选项）直接内联编辑配置并点击“保存更改”。凭据可从 [Telegram 开发者页面](https://my.telegram.org/) 获取。
2. 进入“媒体上传”，在页面顶部选择视频、图片或混合上传模式，点击“选择目录”选择目录。
3. 点击“修改目标”，填写群组及话题 ID，或频道 ID。三种媒体可以使用不同目标。
4. 扫描目录，核对待上传文件；双击媒体组节点或右键选择“编辑标题”，或在工具栏/右键菜单执行“跳过该组 / 恢复该组”排除本次无需入队的 Album。
5. 点击“加入上传队列”，按需选择“加入并前往任务中心”、“加入并继续扫描”或“取消”。在“任务中心”查看队列、发送进度、速度、剩余时间和日志。

首次上传可能要求手机号、验证码、两步验证密码或其他已登录设备确认。登录数据和其他用户数据都保存在统一的 `data/` 目录，升级程序时保留该目录即可继续使用。

## 预览与编辑

- 图片直接显示“媒体组 → 文件”；视频显示“月份 → 媒体组 → 文件”；混合上传显示“文件夹 → 媒体组 → 文件”。展开媒体组查看完整文件列表，鼠标停留在标题或状态列上可查看完整说明。
- 支持通过“跳过该组 / 恢复该组”在入队前排除或恢复单个 Album。排除操作仅影响本次入队范围，保留真实投递状态（如 `失败 · 已排除`、`暂缓 · 已排除`、`待核对 · 已排除`、`部分完成 · 已排除`），不会修改断点、`InflightJournal` 保护记录或已经加入队列的任务清单；顶部与汇总栏区分显示实际已完成、本次待上传与本次跳过数量，当全部未完成 Album 均被排除时禁用入队按钮并提示可恢复。
- 搜索支持文件名、路径和标题；“只看待上传”隐藏已完成、已跳过与本次已排除的条目。**筛选只改变显示，不改变未排除项的上传范围。**
- 标题和追加文字在一个窗口内编辑，支持多行文字及实时预览。图片保留原有编号规则。
- 混合上传以目录下的一级子文件夹为组，递归收集其中的图片和视频；文件夹和文件名都使用同一个逐组件自然排序器，连续数字按数值从小到大排列（`x.41` 在 `x.410` 前），也可改为按修改时间从旧到新。每组可设置 1–10 个媒体，超过数量会拆成多个 Album，组标题默认使用文件夹名。
- 修改目录、目标或分组配置后，请重新扫描该草稿。扫描中可切换媒体页面，上传中可扫描另一个目录、编辑标题并加入新任务。同一时间可以运行一次扫描和一次上传。
- 预览里的待上传数量来自当次扫描与当前选择状态。任务执行前会重新读取断点，跳过已经确认的文件。

## 上传队列与恢复

入队时记录来源、目标、媒体设置、完整 Album 成员及顺序、文件快照与最终 Caption。后续编辑草稿、跳过预览或修改媒体设置只影响新任务。入队后的文件发生变化或缺失时，任务会提示重新扫描。相同媒体类型、来源及目标的重叠范围无法同时排队。

“任务中心”可上移、下移、移除等待中的任务，或通过“清理已结束任务”移除已完成与已跳过的终态任务（同时保留失败、中断、待核对及等待中的任务与其回执）。默认按入队顺序串行上传；严格顺序模式（`pause_on_error`）下，预检失败/暂缓、媒体构建失败、源文件快照变化、Telegram 提交失败或结果未知、以及日志持久化失败均会阻止提交当前或后续 Album，已确认成功的历史或成员消息仍保留断点。暂停只阻止下一项启动，正在上传的任务会继续。安全停止等正在发送的 Album 得到结果后停止，立即中断会保留无法确认的发送记录。

队列、上传断点、未确认记录与媒体元数据缓存统一保存在 `data/uploader.db`（旧版 `upload_queue.json`、`state/` 与 `upload_inflight/` 数据会在首次加载时自动导入），重启后队列默认暂停。异常退出时仍在运行的任务会核对本地断点和未确认记录，显示为已中断或待核对。任何待核对任务都会阻止队列继续。请先在“未确认上传”核对对应的 Telegram 目标和 Album；人工对账会严格校验媒体类型、Telegram 目标、来源目录及文件快照，部分成功选择“允许重新上传”时仅恢复未确认成员并保留已成功文件与其余保护记录；若旧记录缺少匹配依据，将保留已持久化对账结果并提示重新扫描。

- **重新验证并恢复**适用于失败、部分完成、已中断或已经核对的任务。程序核查原始目录、文件大小与修改时间、原始清单、发送记录和断点。通过后任务回到原位置，保留原目标、设置、Album 和 Caption。点击“继续队列”后，已确认的文件会被跳过。
- **跳过此任务**需要确认，适用于失败或已中断的任务。程序检查原始清单、Telegram 目标和未确认记录，并验证已确认文件的断点。源目录暂时不可用或已删除时仍可跳过。断点丢失、损坏或与清单不一致时，程序会保留任务并提示先核对或修复断点。成功后任务和历史记录显示“已跳过”。

恢复、跳过和核对在后台检查并保存，任务中心会显示当前步骤及已检查的源文件数量。检查时仍可切换页面和查看日志。检查可以取消，已开始的保存会安全完成。恢复或跳过后队列保持暂停，请点击“继续队列”开始上传。

损坏的队列文件会保留原样并在任务中心显示读取错误。

## Album 发送失败自动重试

可在“设置与诊断 → 高级选项”中启用。默认关闭，可配置首次发送之外的 1–10 次整组重试。默认额外 2 次，默认间隔 1 秒。设为 0 秒时仍会遵守 Telegram 要求的等待时间。

```toml
[upload_retry]
enabled = false
max_retries = 2
retry_delay_seconds = 1.0
```

开启后，明确可重试的拒绝与失败共用整组重试额度。额外次数设为 2 时，正常直传最多发送 3 次。重试前等待配置间隔与 Telegram 要求的较大值，然后重新核查停止状态、发送记录、源文件和预处理产物。次数用尽后按配置暂停或继续后续 Album。

关闭整组重试时，可通过 `flood_max_retries` 单独控制限流重试，默认为一次。续传时，只有本次提交的剩余项全部明确因限流失败，才会重试这些项。部分成功、待确认和未知结果进入人工核对。任务中心显示尝试次数、失败原因和等待时间。

## 界面主题与目标名称缓存

侧栏按钮切换深色或浅色，选择立即写入 `data/ui_preferences.json`，下次启动时恢复；首次启动默认深色。上传确认 Telegram 目标后，群组与 Topic 名称也会原子缓存到 `data/ui_preferences.json`，同步刷新所有匹配该目标的上传页面与任务中心队列行；若写入失败会回滚内存变更并记录警告日志，不影响主题设置与当前上传。

## 上传规则

“媒体组”即 Telegram Album；“标题”指媒体说明文字 Caption。

| 选项 | 视频 | 图片 | 混合 |
| --- | --- | --- | --- |
| 扫描范围 | 目录及所有子目录 | 目录及所有子目录 | 一级子文件夹及其子目录 |
| 默认分组 | 按月份，每组最多 10 个 | 按排序，每组最多 10 张 | 每个文件夹，每组最多 10 个 |
| 每组数量 | 可设为 1–10 | 可设为 1–10 | 可设为 1–10 |
| 扫描排序 | 修改时间或文件夹/文件名（自然数字升序）二选一 | 修改时间或文件夹/文件名（自然数字升序）二选一 | 文件夹/文件名（自然数字升序）或修改时间二选一 |
| 组标题 | 可显示/隐藏 Album 或月份标题 | 编号标题可开关 | 默认使用文件夹名 |
| 文件名 | 可显示/隐藏；序号可单独开关 | 可开启，默认关闭 | 可显示/隐藏；序号可单独开关 |
| 标题编辑 | 基础标题及追加文字 | 编号后追加文字 | 文件夹标题及追加文字 |
| 其他选项 | 日期读取、日期策略、媒体创建日期、视频封面 | 图片超限处理 | 图片/视频混合发送、独立断点 |

文件名排序会把每个路径组件中的连续数字转换为整数后升序比较，先比较上一级目录，再比较下一级目录，最后比较文件名。例如 `Day2/x.10.jpg`、`Day2/x.100.jpg`、`Day10/x.1.jpg` 会按这个顺序排列；`x.1`、`x.10`、`x.100` 也会保持自然顺序。修改时间模式仍按 mtime 从旧到新，mtime 相同时使用同一套路径规则稳定排序。

混合上传可在“媒体上传”分段导航中切换。`mixed_dir` 下每个一级子文件夹是一个独立组，组内可同时放入图片和视频（也支持更深层目录）；同组媒体保持统一顺序并使用 Telegram 的混合 Album。混合模式有自己的 `[telegram.mixed]` 目标、断点和标题文件，旧配置缺少这些段时会自动使用公共目标和默认设置。根目录直接放置的媒体会被忽略并提示移动到一级子文件夹，不会改变其他组的 Album 边界。

每组仅第一条消息显示统一标题。最后不足一组也会发送；只有一个文件时发送单条消息。

Telegram 限制在扫描和发送前都会检查：普通账号视频单文件上限约 2 GB，Premium 账号上限约 4 GB；超过约 2 GB 的视频会在登录后、发送前按账号状态跳过并明确提示，超过约 4 GB 的视频仍会完整出现在扫描结果中，但会在上传前安全跳过。程序按 Telegram 的精确字节边界检查（普通账号 2,097,152,000 字节，Premium 账号 4,194,304,000 字节）。图片会执行 Telegram Photo 几何规范与大小检查（长边建议不超过 2560、宽高之和 ≤ 10000、宽高比 ≤ 20、单文件 ≤ 10 MiB，压缩目标 ≤ 9.5 MiB）。程序会自动对超大尺寸、极端长宽比或非 JPEG/PNG 格式进行安全规范化（原图不修改，仅存入本地缓存后上传）；对于动画图片、损坏图片或无法修复的图片，会在预检阶段安全跳过，绝不静默降级为 Telegram Document。

视频选择“按扫描顺序固定分组”后会忽略月份，按照设置的 1–10 个连续分组。组标题、文件名列表和文件名序号可以分别开关，仍可逐组编辑标题。

可选文件名清单附加在统一标题后，例如：

```text
21-5 · 第一天
1. clip_a
2. clip_b
```

“带文件名”现在统一只显示文件 stem（去掉最后一个扩展名），视频、图片和混合上传均适用；例如 `clip_a.mp4` 显示为 `clip_a`，`archive.tar.gz` 显示为 `archive.tar`。

## 日期与视频工具

日期优先级为 **EXIF 信息 →（可选）媒体创建日期 → 文件修改日期**。在视频配置中开启 `read_media_creation_date`，或在“编辑视频目标与配置”中勾选“读取媒体创建日期”，即可读取 `MediaCreateDate`、`TrackCreateDate` 和 QuickTime 创建日期。ExifTool 按批次读取 Python 已发现的文件；每个已返回文件都会更新界面进度，停止时会明确显示扫描已取消。只有缺少可用 EXIF/内嵌日期的视频才会进入 FFmpeg 回退，每个文件只读取一次，最多同时处理 4 个文件。

如果只需要按文件名上传，可以关闭 `read_dates`，或在“编辑视频目标与配置”中关闭“读取视频日期信息”。此时程序跳过 EXIF、媒体创建日期和文件修改时间读取，自动按文件名排序并按固定数量分组；组标题和文件名清单等上传选项仍然有效，上传准备和发送阶段也不会因为缺少日期而崩溃。

默认配置开启媒体创建日期以保持旧版本行为；如果更重视扫描速度，可以关闭 `read_media_creation_date`，此时仍读取 EXIF，缺失时直接使用文件修改时间（`missing_date_policy = "mtime"`）。没有 ExifTool 时，只要开关保持开启，程序仍会尝试通过 FFmpeg 读取媒体创建日期。选择“停止并提示缺失日期”（`error`）时，找不到可用日期的视频会阻止上传。

从 [ExifTool 官网](https://exiftool.org/) 下载对应平台版本。Windows 将程序放到 `tools/exiftool.exe`；macOS 可将可执行文件放到 `tools/exiftool` 并执行 `chmod +x tools/exiftool`。若附带 `exiftool_files`，一起放入 `tools/`；也可在设置中指定工具路径。

视频封面默认开启，可以在视频配置中关闭。`thumbnail_timestamp_seconds` 控制视频缩略图从视频第几秒截图，支持精确到 0.01 秒，例如：

```toml
[video]
thumbnail_timestamp_seconds = 1.25
```

混合上传也可在 `[mixed]` 中设置同名字段；未填写时继承 `[video]` 的值。视频缩略图保存在本地缓存中。如果某个视频以前已经生成过封面，修改截图时间后可能仍会看到原来的图片；程序不会自动删除旧缓存。修改截图时间后，如需让已经缓存过的视频使用新的截图位置，请进入“设置 → 存储与缓存”，执行现有的“仅清理视频封面”功能，然后重新扫描或上传。无需清理上传状态、视频断点、混合状态、Telegram 登录数据或所有缓存。

便携包自带构建时校验的 LGPL FFmpeg 与 FFprobe；后台调用工具时会隐藏 Windows 控制台窗口。

## 断点、停止与缓存

“安全停止”会等待当前 Album 发送完成并保存断点，然后退出；已在后台准备的下一组不会发送。“立即中断”会取消当前上传，未确认的发送结果会保留在“未确认上传”中，核对后才能恢复。只有整组消息确认发送成功后才记录完成状态；重新扫描时，已完成的组自动跳过。

三种上传模式共享 `[upload]` 的预取设置。默认最多同时准备 3 个 Album，预取窗口按源文件大小预留 8 GiB。单组超过预算时，轮到它后再准备。关闭预取后，程序逐组准备和发送。暂停或中断会清理尚未发送的暂存副本。

文件路径、大小或修改时间变化后，会被视为新文件。上传断点、未确认记录、任务队列和媒体探测缓存保存在 `data/uploader.db`。程序首次读取旧版数据时导入原有 JSON，原文件保留供备份。

“设置与诊断”提供三类清理：

- **仅清理视频封面**：删除 `data/cache/thumbnails` 的生成文件，之后需要时重新生成。
- **仅清理已处理视频**：删除 `data/cache/video_processed` 中暂存的重新封装视频文件。
- **清理所有**：清空上传断点、标题、封面、任务历史、已处理视频、暂存副本和运行日志。保留未确认记录、配置与 Telegram 登录数据库。清理断点后重新上传可能产生重复消息。

各媒体上传页提供“重置本类型断点”：确认后仅一次性清空所选类型的全部断点（包括该类型不同来源目录和上传目标的记录），其他类型、标题、缓存、登录数据和未确认上传记录均保留。扫描或上传运行时不能重置；重置后需重新扫描，已上传文件会再次列为待上传，继续上传可能产生重复消息。未确认上传仍须先核对处理，重置断点不会允许重发未确认的内容。旧配置中的 `reset_state` 已忽略，即使保留 `true`，扫描和上传也不会自动清空断点。

历史记录保留最近 100 次任务。`data/` 同时保存配置、TDLib 登录数据、标题、缓存和运行日志。迁移支持 V1.9 的断点、未确认记录与任务队列。

### 迁移与备份 data

升级或更换电脑时，先**完全退出** TDLib Media Uploader，再直接复制整个 `data/` 目录即可保留 V1.9 的登录数据、任务历史、标题、断点和未确认记录。不要在上传、登录或 TDLib 数据库仍在写入时复制；`data/telegram/database` 是登录状态的关键数据，不建议只复制其中部分文件。Windows 便携版使用程序目录下的 `data/`；macOS 发布版使用 `~/Library/Application Support/TDLib Media Uploader/data/`。安装或解压新版本后，把完整的 `data/` 放到对应位置即可。复制时不要套多一层 `data/data`，并先保留一份备份。最简单可靠的迁移方式是整体复制 `data/`，不需要分别寻找配置、登录数据库、断点、标题、历史或日志文件。V1.9 不承诺读取 V1.8.x 或旧开发版的状态格式。

当前数据目录结构如下。旧版的 `state/`、`upload_inflight/` 和 `upload_queue.json` 在迁移后仍可作为备份保留。

```text
data/
├── config.toml
├── telegram/
│   ├── database/   # TDLib database_directory，包含登录状态
│   └── files/      # TDLib files_directory
├── uploader.db    # 上传断点、未确认记录、任务队列和媒体元数据
├── captions/      # Album 标题
├── ui_preferences.json
├── cache/          # 封面、压缩和暂存副本
├── logs/
└── history.json
```

Windows 便携版的实际位置是 `程序目录/data/telegram/`；macOS 冻结版是 `~/Library/Application Support/TDLib Media Uploader/data/telegram/`。复制整个 `data/` 后，TDLib 会继续使用其中的 `telegram/database` 和 `telegram/files`。

视频、图片或混合媒体无法被 FFmpeg/Pillow 读取时，程序会在预检阶段跳过该文件，继续上传其他文件；暂时不可读的项目会标记为 deferred，网络恢复后重新扫描即可重试。预检生成的 Album 计划会保留完整成员，deferred 文件不会让后面的文件向前补位。上传前还会再次检查文件存在、可读且大小/修改时间没有变化。坏文件不会写入断点，跳过文件的完整路径和原因会显示在任务日志中，并保存到应用数据目录的 `logs/app.log`。TDLib 原生诊断写入同目录的 `logs/tdlib.log`，可用于排查上传失败。

每个待发送 Album 在消息请求前先持久化为 `PREPARED`。随后记录 `SUBMITTED` 和 `CONFIRMED`，确认后的断点、任务进度和日志清理由 SQLite 事务保存。消息提交后超时、断线、中断或部分成功的 Album 会保留为 `UNKNOWN`，需要从侧栏的“未确认上传”进入核对。

在 Telegram 核对整组文件后，可以选择“标记为已发送”保存整组断点，或选择“允许重新上传”解除保护。已有明确成功结果的文件会先写入断点，下次上传时跳过。旧日志含已成功的消息且无法对应到源文件时，程序会保留保护记录。关闭 Telegram 数据库超时会单独提示，不会把已经完成的上传改报为失败。

`config.toml` 中的凭据以明文保存在本机，`data/telegram` 含登录数据；不要公开配置或整个 `data/` 目录。日志也可能包含本地路径和 Chat ID，分享排障资料前请先脱敏。POSIX 系统会将应用数据目录权限收紧为 `0700`，配置和新写入的断点文件使用 `0600`。Windows 保留目录继承权限，启动时只读取 data 目录的权限条目；“环境与许可”会显示其他账户的读取授权或“无法检查”。如需调整，可使用文件夹“属性 → 安全”，或将便携版放到个人用户目录。程序不会扫描、改写子文件权限或检查网络共享权限。

发送重试的等待可由“安全停止”或“立即中断”取消。只有整组消息均明确失败时，程序才会重试 TDLib 明确允许且无需额外操作的消息。部分成功或结果未知的 Album 需要人工核对。

`[upload_retry].queue_error_policy` 默认值 `pause_on_error` 会在 Album 失败或结果未确认时暂停。`skip_and_retry` 可以先处理后续 Album，再用自动重试额度中的最后一次尝试处理明确失败且可重试的组。`min_send_interval_seconds` 控制两次 Album 发送的最小间隔。等待 Telegram 限流恢复时，任务页显示倒计时。关闭普通 Album 重试时，明确的 FloodWait 仍可按 `flood_max_retries` 重试，默认为一次。

可在“设置与诊断 → Telegram”启用实验性的远程预上传。程序并发准备主照片和视频，再按 Album 顺序提交消息。缩略图仍使用本地文件。预上传停滞超过 `preliminary_wait_seconds` 或不可用时回退直传。提交时若文件引用被拒绝且本次提交的所有消息均明确未送达，程序可改用本地文件发送一次。在消息提交前停止预上传时，只取消文件传输。

发送前会递归校验混合 Album 中每个 Photo/Video 的 `InputFile` 结构，并检查临时本地文件是否仍可读取；空的 `photo`/`video` 不会被提交给 TDLib，也不会先写入发送 journal。源码构建固定使用 `tdjson 1.8.71`。

如果源文件位于 SMB/NAS，可在“设置与诊断”中选择本地暂存模式，或在配置中设置 `[staging] mode`：`off` 直接读取源文件，`network` 只暂存网络盘，`always` 暂存所有文件。程序会在 `data/cache/staging` 或你指定的受管目录中创建带 marker 的暂存副本，只清理自己创建的文件，不会递归删除 base directory 中的其他用户文件。

## 配置与代理

配置保存在统一数据目录的 `data/config.toml`，首次运行从包内的 `resources/default_config.toml` 创建。日常通过界面编辑；保存无效配置时恢复原文件。

- API、媒体目录、暂存和代理：在“设置与诊断”的常规、Telegram、存储与缓存面板编辑。
- 扫描稳定性、ExifTool 路径、FFmpeg/ExifTool 超时、批次大小和重试：在“设置与诊断 → 高级选项”编辑。
- 视频/图片/混合目标、分组、标题和处理选项：通过上传页“修改目标”或“编辑上传参数”编辑。
- 目标未单独配置时继承 `[telegram]`；单独目标位于 `[telegram.video]`、`[telegram.image]` 和 `[telegram.mixed]`。频道模式不使用 Topic。
- 代理支持 SOCKS5、HTTP、MTProto，默认关闭并使用直连。SOCKS5/HTTP 可填写用户名与密码；MTProto 需要 Secret。代理由 TDLib 配置，无需额外代理库。
- `[scan]` 集中控制网络目录扫描：`stability_checks_local/network` 与对应间隔分别控制本地和网络盘的连续稳定检查，旧的 `stability_checks`/`stability_interval_seconds` 仍作为回退；`discovery_attempts` 和两个 delay 控制目录发现阶段的有限重试，`readiness_attempts` 是暂时不可读时的重试次数，`read_probe_bytes` 是头尾读探针大小；`io_workers_local` 与 `io_workers_network` 分别限制本地和 SMB/NAS 的 I/O 并发。扫描会跳过符号链接和 Windows junction，按下“停止”可取消目录遍历；正在进行的系统文件调用会在返回后响应取消。
- `[process]` 集中设置 ExifTool、FFmpeg 日期/媒体信息、封面和图片压缩的单次超时，以及 ExifTool 批次大小和重试次数。ExifTool 只读取 Python 扫描确认的显式文件列表，不会再次递归扫描目录；空输出、非法 JSON、超时和不完整批次会自动重试并二分隔离，单个问题文件不会让整批失败。
- `[image].extensions` 与 `[video].extensions` 必须互不重复；发现冲突时程序会在启动时明确提示。新安装默认支持的视频扩展名包括 `.mp4`、`.mov`、`.m4v`、`.mkv`、`.avi`、`.ts`、`.mts`、`.m2ts`；扫描时也会通过文件头识别常见视频容器，不依赖扩展名判断实际格式。

  > **升级兼容性提示**：程序保留现有 `data/config.toml`。旧配置中的视频扩展名列表仍可更新；文件头能识别的常见视频容器即使未列出或后缀写错，也会进入视频扫描：
  > ```toml
  > [video]
  > extensions = [
  >   ".mp4",
  >   ".mov",
  >   ".m4v",
  >   ".mkv",
  >   ".avi",
  >   ".ts",
  >   ".mts",
  >   ".m2ts",
  > ]
  > ```

  图片的新格式也需要在现有 `[image]` 段中手动扩展：`extensions = [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"]`。未修改时继续使用旧配置中列出的图片格式；混合上传的视频同样按文件头优先识别。

  **视频兼容处理与原生直传说明**：
  - **原生格式直传（MP4 / MOV / M4V）**：当经 FFprobe 探测确认为原生兼容容器且视频编码为 H.264 或 HEVC/H.265 时，程序**直接使用原文件（或网络暂存路径）上传**，**绝不会**将原生视频复制或移动到 `data/cache/video_processed/`，也不会因为 MOV/M4V 不是 MP4 就重新封装。原生格式的音频不受限制（AAC、AC3、PCM 等均允许原生直传）。
  - **无损重新封装**：在 `[video].compatibility_policy = "remux"`（默认）下，程序根据 FFprobe 探测到的实际容器与编码决定处理方式。MPEG-TS 即使误命名为 `.mp4`、`.avi` 也会尝试转封装；MP4 中的视频不是 H.264/H.265 时也会尝试。FFmpeg 使用流复制（`-c copy`、`-movflags +faststart`）生成 MP4 副本，并验证输出容器及音视频编码未改变。若 MP4 无法承载这些编码，则跳过该文件并报告 FFmpeg 错误。**原文件永不修改，也不会重新编码或降级为 Document**。能封装为 MP4 不保证 Telegram 能播放原编码。若配置为 `"original"`，则跳过待封装文件。
  - **处理后文件命名规则**：处理后的 MP4 保留原始文件的文件名主体（如 `clip.ts` → `clip.mp4`，`movie.mkv` → `movie.mp4`），**文件名中绝不包含哈希**。若同组内存在同名冲突（如 `movie.ts` 与 `movie.avi`），将采用确定性后缀（如 `movie.ts.mp4` 与 `movie.avi.mp4`），避免覆盖。内部特征签名仅保存在内部 `manifest.json` 元数据中。
  - **A / A+1 流水线与文件生命周期**：上传采用严格的“当前组 A 上传 + 下一组 A+1 并发准备（Lookahead = 1）”流水线。A 组正在上传时，后台线程并发完成 A+1 组的探测、重新封装、MP4 校验与视频封面预生成；A+1 组 READY 后不会重复提取封面。当 A 组在 Telegram 确认且本地 `UploadState` 成功持久化后，A 组对应的 `data/cache/video_processed/<group_key>/` 临时目录将被删除；源文件始终保持不动，且缩略图缓存（与源文件逻辑身份绑定）独立保留。中断或异常时保留已验证的处理文件以支持断点续传。

  **路径行为示例**：
  ```text
  原始分组：
  001.mp4
  002.mov
  003.ts
  004.avi

  实际上传路径：
  001.mp4 → 原始文件路径（原生直传，无副本）
  002.mov → 原始文件路径（原生直传，无副本）
  003.ts  → data/cache/video_processed/<group>/003.mp4（临时无损封装副本）
  004.avi → data/cache/video_processed/<group>/004.mp4（临时无损封装副本）

  该组上传成功并保存断点后：
  data/cache/video_processed/<group>/ 立即清理
  原始文件 001.mp4、002.mov、003.ts、004.avi 完好无损
  ```

  默认支持的图片格式包括 `.jpg`、`.jpeg`、`.png`、`.webp`（仅静态）、`.bmp`、`.tif`、`.tiff`；BMP、TIFF 和静态 WebP 会在缓存中转为 JPEG，带透明通道的图片会自动与配置的背景色（默认 `#FFFFFF`）复合，长宽比超过 20 的长图默认进行居中画布填充（`pad`）。WMV、WebM、GIF、APNG 等格式不在默认媒体列表中。旧配置中的这些扩展名会被启动时过滤掉；如需发送它们，请使用 Telegram 的文件方式或先进行转换。

支持本地目录及 `\\server\share\...` 网络目录。扫描时不可读取的项目会跳过并提示；网络恢复后可重新扫描。扫描结果中的暂时不可读文件会标记为可重试的 deferred 项目；上传前会再次检查文件仍存在、可读且未在扫描后发生变化。

## 使用限制

应用仅支持从 [GitHub Releases](https://github.com/Maxwell233/tdlib-media-uploader/releases) 下载 Windows 便携包或 macOS arm64 DMG 后运行；仓库不再提供源码启动方式。源码仅用于开发和 CI 的离线回归验证，不作为用户运行入口。

构建使用 Python 3.13 和 `requirements-build-lock.txt` 中的固定版本。界面依赖 PySide6 Essentials，上传请求使用 TDLib 1.8.71 的媒体结构。

发布包支持 `--self-test` 离线诊断。全新包即使还没有 `data/config.toml` 也可以直接运行该命令，检查资源、可写数据目录、TDLib 路径和内置 FFmpeg/FFprobe。

## 构建 Windows 和 macOS 包

GitHub Actions 在 Windows x64 和 macOS arm64 runner 上执行完整构建与离线验证门禁：先安装 `requirements-build-lock.txt` 与 `requirements-test.txt` 并准备、校验 LGPL FFmpeg/FFprobe，随后运行 `compileall`、完整离线 `pytest` 回归套件（含 offscreen Qt 与实际 FFmpeg 样本测试）以及隔离的源码自检，全部通过后才读取 `tdlib_media_uploader.spec` 进行 PyInstaller 打包。`tdlib_media_uploader.spec` 显式排除 `imageio-ffmpeg` wheel 自带的 FFmpeg 二进制以及 `tests` / `pytest` 测试依赖；打包后继续执行包内 `--self-test`、许可/图标校验及无测试/数据残留检查。

输出为 Windows 的 `dist/TDLib Media Uploader/TDLib Media Uploader.exe` 及 Windows x64 ZIP，和 macOS 的 `dist/TDLib Media Uploader.app` 及带版本号的 macOS arm64 DMG。DMG 根目录包含应用和指向系统 `/Applications` 的 `Applications` 文件夹别名，便于拖放安装。Windows 构建同时校验 EXE 嵌入图标、Qt 图标资源和稳定的 AppUserModelID，避免打包后任务栏图标缺失或归组异常。构建包不包含个人配置、断点、封面缓存、媒体文件或登录数据；macOS 包另附 FFmpeg 构建信息。

核心文件位于 `src/tdlib_media_uploader/`：`app.py`（打包入口）、`gui/application.py`（GUI 启动与自检边界）、`gui/events.py` 与 `gui/workers.py`（GUI 事件和线程边界）、`gui/models.py`（预览模型适配）、`gui/main_window.py`（界面与生命周期）、`config/loader.py` 与 `config/paths.py`（配置和路径）、`media/video_pipeline.py`、`media/image_pipeline.py`、`media/mixed_pipeline.py`（媒体扫描、分组及内容构建）以及 `telegram/tdlib_common.py`（TDLib）。GUI 运行期间持有 `data/app.lock`，上传任务在同一应用实例内执行。构建配置集中在根目录 `tdlib_media_uploader.spec`，平台构建只在 GitHub Actions 中执行。

版本使用“主版本.功能版本.修订版本”：日常优化增加最后一位，较大功能更新增加中间一位。版本号唯一存放在根目录 `VERSION`，程序、界面和平台构建 workflow 会从该文件读取。此次修改见 [CHANGELOG.md](CHANGELOG.md)。推送 main 只构建并校验产物；发布需创建与 VERSION 一致的新版本标签，既有标签和 Release 不自动覆盖。下载资产与 SHA256SUMS 放在同一目录后，可执行 `shasum -a 256 -c SHA256SUMS` 校验。

## 断点与维护

SQLite 使用 WAL。退出应用后复制整个 `data/` 目录，可以同时备份上传队列、未确认记录和断点。可在“存储与缓存”设置三类缓存上限。媒体探测结果默认最多保留近期的 5 万条记录。封面和压缩图片在启动及任务结算后按最近使用时间清理，存在待核对的发送记录时保留任务制品。

清理缓存在后台执行，可以取消；取消保留尚未删除的文件，已经删除的缓存不会恢复。清理期间暂停启动扫描、上传、修改配置和处理未确认记录。关闭应用时会等待清理线程结束，以及 TDLib 返回最终数据库关闭状态；关闭超时会提示等待，稍后可重试退出。

发送前会分别校验扫描源文件和准备后的本地文件。准备期间变化、消失或无法读取的文件需重新扫描；同组最终检查失败时整组延期。转换图片和封装视频仅在源文件快照仍一致时发布可复用缓存。直接上传仍由 TDLib 异步读取源路径，因此上传期间应保持源目录不变；持续写入的目录可启用本地暂存来固定待发送副本。

## 开发与验证

核心上传、持久化、预览、队列恢复及回归测试位于 `tests/`（均使用离线模拟与合成媒体 fixtures，不访问真实 Telegram 账号或本地 `data/`）。本地运行离线验证需先安装 `requirements-build-lock.txt` 与 `requirements-test.txt`（并在需要验证视频工具时将 LGPL `ffmpeg` / `ffprobe` 加入 `PATH`）：

```bash
python -m compileall -q src tests
PYTHONPATH=src QT_QPA_PLATFORM=offscreen python -m pytest tests -v
```

> 说明：仅离线单元测试与发布包 `--self-test` 可在无 Telegram 登录态下运行；真实 Telegram 上传与对账交互仍需人工在打包应用中结合测试群组/频道完成端到端核验。

## 构建缓存

macOS CI 缓存完整 FFmpeg/FFprobe 工具目录，缓存键包括源码校验和、平台、架构和包含构建选项的 workflow 摘要。命中后仍验证工具可执行、架构、版本及许可配置；失效时重新构建。构建禁用可选外部库自动探测，避免把构建机偶然存在的动态依赖带入工具。Python 依赖继续按锁文件安装，并复用下载缓存。

## 信任与风险

建议只从本仓库的 [GitHub Releases](https://github.com/Maxwell233/tdlib-media-uploader/releases) 下载，并在运行前核对 `SHA256SUMS`。平台构建 workflow、项目许可、作者署名和第三方依赖清单都公开在仓库中；发布 ZIP 也包含许可/署名文件，便于检查来源和再分发条件。SHA-256 只能证明文件与发布者提供的摘要一致，不能替代代码审查或操作系统安全认证。

macOS 包未配置 Apple Developer 签名和公证，所以 Gatekeeper 可能显示“无法验证开发者”。请不要绕过来源核验后直接运行未知文件；确认仓库地址、标签和 SHA-256 后再按系统提示打开。Windows 版也不应被视为经过独立安全机构认证的程序。应用会调用随包提供或系统中的 FFmpeg/ExifTool 处理媒体；请确认这些工具来源和许可，并注意压缩失败、网络中断、Telegram 限制以及重复上传等运行风险。

程序需要 Telegram API ID/API Hash，并会在本机 `data/` 保存 Telegram 登录数据库、代理设置、上传断点和用户输入的标题；这些数据不会随发布包提供。不要把 `data/config.toml`、API Hash、登录数据库、代理密码或缓存发给他人。上传目标、代理、媒体内容和 Telegram 账号权限均由使用者自行确认；请遵守 Telegram 使用条款、版权要求和目标群组/频道规则。若使用 ExifTool 或自行替换 FFmpeg，还需遵守对应上游许可证。

## 项目许可与署名

本项目的原创代码、文档和界面资源采用 [GNU General Public License v3.0 only（GPL-3.0-only）](LICENSE) 发布。使用、修改和再分发时请遵守 `LICENSE` 中的版权与许可要求。

完整许可文本见根目录的 `LICENSE`，作者署名见 `ATTRIBUTION`，第三方组件清单见 `THIRD_PARTY_LICENSES.md`。本项目为独立社区项目，与 Telegram 官方无隶属关系；TDLib、PySide6、Pillow、imageio-ffmpeg、FFmpeg、PyInstaller、Python 及其他第三方组件分别遵循各自许可证。源码包和编译包均包含上述三个许可/署名文件，请勿将 `data/config.toml`、Telegram API 凭据、登录数据或本地断点状态随包分发。
