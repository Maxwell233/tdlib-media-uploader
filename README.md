# TDLib Media Uploader

**V1.9.5 · Windows x64 + macOS arm64 桌面应用**

把本地或网络目录中的视频、图片批量上传到 Telegram 群组话题或频道。Windows x64 与 macOS Apple Silicon arm64 使用同一套 GUI、上传核心和功能配置；支持上传预览、标题编辑、断点恢复和独立代理，上传时无需打开 Telegram Desktop。

## 开始使用

已发布的便携包在 [Releases](https://github.com/Maxwell233/tdlib-media-uploader/releases)。Windows 下载 x64 ZIP，完整解压后运行 `TDLib Media Uploader.exe`；macOS 下载 Apple Silicon arm64 DMG，打开后将 `TDLib Media Uploader.app` 拖到其中的 `Applications` 文件夹，再从“应用程序”启动；两者都无需安装 Python。下载后可用 Release 中的 `SHA256SUMS` 校验 ZIP 和 DMG；源码中的修改可能尚未打包发布。

macOS 首次打开若出现“无法验证开发者”等提示，请先确认下载地址和 SHA-256，再在 Finder 中右键点按应用并选择“打开”。当前包没有 Apple Developer 签名/公证，见下方[信任与风险](#信任与风险)。

1. 打开“设置与诊断”，在左侧各分类（常规、Telegram、上传参数、高级选项）直接内联编辑配置并点击“保存更改”。凭据可从 [Telegram 开发者页面](https://my.telegram.org/) 获取。
2. 进入“媒体上传”，在页面顶部选择视频、图片或混合上传模式，点击“选择目录”选择目录。
3. 点击“修改目标”，填写群组及话题 ID，或频道 ID。三种媒体可以使用不同目标。
4. 扫描目录，核对待上传文件；双击媒体组节点或右键选择“编辑标题”进行编辑。
5. 点击“开始上传”并确认。在“任务中心”查看进度、速度、剩余时间和日志。

首次上传可能要求手机号、验证码、两步验证密码或其他已登录设备确认。登录数据和其他用户数据都保存在统一的 `data/` 目录，升级程序时保留该目录即可继续使用。

## 预览与编辑

- 图片直接显示“媒体组 → 文件”；视频显示“月份 → 媒体组 → 文件”；混合上传显示“文件夹 → 媒体组 → 文件”。展开媒体组查看完整文件列表，鼠标停留在标题上可查看完整说明。
- 搜索支持文件名、路径和标题；“只看待上传”隐藏已完成文件。**筛选只改变显示，不改变上传范围。**
- 标题和追加文字在一个窗口内编辑，支持多行文字及实时预览。图片保留原有编号规则。
- 混合上传以目录下的一级子文件夹为组，递归收集其中的图片和视频；文件夹和文件名都使用同一个逐组件自然排序器，连续数字按数值从小到大排列（`x.41` 在 `x.410` 前），也可改为按修改时间从旧到新。每组可设置 1–10 个媒体，超过数量会拆成多个 Album，组标题默认使用文件夹名。
- 修改目录、目标或分组配置后，需要重新扫描。任务结束后也需重新扫描，以读取最新断点。
- 扫描或上传期间不能修改配置；同一时间只运行一个上传任务。

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

默认开启 `[video].prefetch_next_album = true`，视频、图片和混合上传都可在发送当前组时提前准备下一组。视频准备包含文件就绪检查、必要的无损封装、输出验证与封面生成；最多只提前一组。需要节省本地读取或处理资源时，可关闭此选项，改为逐组准备和发送。

文件路径、大小或修改时间变化后，会被视为新文件。V1.9.1 将状态、标题、登录数据库、缓存、历史和日志统一保存到 `data/`，三种模式分别使用 `data/state/video`、`data/state/image`、`data/state/mixed`。

“设置与诊断”提供三类清理：

- **仅清理视频封面**：删除 `data/cache/thumbnails` 的生成文件，之后需要时重新生成。
- **仅清理已处理视频**：删除 `data/cache/video_processed` 中暂存的重新封装视频文件。
- **清理所有**：清空状态、标题、封面、任务历史、未确认记录、已处理视频、暂存副本和运行日志。保留 `data/config.toml` 及 Telegram 登录数据库。清理断点后重新上传可能产生重复消息。

也可在相应媒体配置中临时设置 `reset_state = true`；运行一次后务必改回 `false`。

历史记录保留最近 100 次任务。所有用户数据都位于 `data/`：`telegram/database` 和 `telegram/files` 保存 TDLib 登录数据库及文件缓存，`captions` 保存标题，`state` 保存断点，`upload_inflight` 保存未确认记录，`cache` 保存封面、压缩和暂存副本，`logs` 保存日志，`history.json` 保存任务历史。V1.9.1 按全量更新处理，不读取 V1.8.x 或旧开发版的散落状态文件。

### 迁移与备份 data

升级或更换电脑时，先**完全退出** TDLib Media Uploader，再直接复制整个 `data/` 目录即可保留 V1.9 的登录数据、任务历史、标题、断点和未确认记录。不要在上传、登录或 TDLib 数据库仍在写入时复制；`data/telegram/database` 是登录状态的关键数据，不建议只复制其中部分文件。Windows 便携版使用程序目录下的 `data/`；macOS 发布版使用 `~/Library/Application Support/TDLib Media Uploader/data/`。安装或解压新版本后，把完整的 `data/` 放到对应位置即可。复制时不要套多一层 `data/data`，并先保留一份备份。最简单可靠的迁移方式是整体复制 `data/`，不需要分别寻找配置、登录数据库、断点、标题、历史或日志文件。V1.9 不承诺读取 V1.8.x 或旧开发版的状态格式。

V1.9.1 的目录职责固定如下；程序资源和 PyInstaller `_internal` 目录只读，不会用作登录或其他用户数据目录：

```text
data/
├── config.toml
├── telegram/
│   ├── database/   # TDLib database_directory，包含登录状态
│   └── files/      # TDLib files_directory
├── state/video/    # 视频断点
├── state/image/    # 图片断点
├── state/mixed/    # 混合断点
├── captions/      # Album 标题
├── upload_inflight/
├── cache/          # 封面、压缩和暂存副本
├── logs/
└── history.json
```

Windows 便携版的实际位置是 `程序目录/data/telegram/`；macOS 冻结版是 `~/Library/Application Support/TDLib Media Uploader/data/telegram/`。复制整个 `data/` 后，TDLib 会继续使用其中的 `telegram/database` 和 `telegram/files`。

视频、图片或混合媒体无法被 FFmpeg/Pillow 读取时，程序会在预检阶段跳过该文件，继续上传其他文件；暂时不可读的项目会标记为 deferred，网络恢复后重新扫描即可重试。预检生成的 Album 计划会保留完整成员，deferred 文件不会让后面的文件向前补位。上传前还会再次检查文件存在、可读且大小/修改时间没有变化。坏文件不会写入断点，跳过文件的完整路径和原因会显示在任务日志中，并保存到应用数据目录的 `logs/app.log`。TDLib 原生诊断写入同目录的 `logs/tdlib.log`，可用于排查上传失败。

每个待发送 Album 在请求前会写入 `data/upload_inflight/<hash>.json`。状态依次记录为 `PREPARED`、`SUBMITTED`、`CONFIRMED`，正常断点写入后才删除；超时、断线、取消或部分成功会记录为 `UNKNOWN`，下一次不会自动重发可能已经提交的 Album。请打开侧栏的“未确认上传”，先在 Telegram 中核对对应目标，再手动处理。

发送前会递归校验混合 Album 中每个 Photo/Video 的 `InputFile` 结构，并检查临时本地文件是否仍可读取；空的 `photo`/`video` 不会被提交给 TDLib，也不会先写入发送 journal。当前正式版固定使用 `tdjson 1.8.64.post1`。

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

依赖由 `requirements-build-lock.txt` 固定版本，其中包含 `tdjson==1.8.64.post1`、Pillow、imageio-ffmpeg、PySide6 和 PyInstaller。固定 TDLib 版本是为了兼容现有上传实现，请勿随意升级。

离线回归测试（安装依赖后运行）：

```powershell
$env:PYTHONPATH = "src"
$env:QT_QPA_PLATFORM = "offscreen"
python -m unittest discover -s tests -v
```

测试按功能组织，`test_regression_*` 保留跨模块行为回归；优先为实际故障增加最小复现，避免重复的测试名称索引。

便携包支持 `--self-test` 离线检查。全新包即使还没有 `data/config.toml` 也可以直接运行；检查只验证资源、可写数据目录、TDLib 路径和可选 FFmpeg，不会连接 Telegram 或修改正式配置。

## 构建 Windows 和 macOS 包

仓库不再提供本地源码构建脚本。GitHub Actions 在 Windows x64 和 macOS arm64 原生 runner 上直接读取 `tdlib_media_uploader.spec`，先执行离线回归测试，再构建并校验发布包。构建流程会排除 `imageio-ffmpeg` wheel 自带的 FFmpeg 二进制，分别准备经过许可和构建标志检查的 LGPL FFmpeg，并验证应用架构、图标、许可文件、DMG/ZIP 和打包后自测结果。

输出为 Windows 的 `dist/TDLib Media Uploader/TDLib Media Uploader.exe` 及 Windows x64 ZIP，和 macOS 的 `dist/TDLib Media Uploader.app` 及带版本号的 macOS arm64 DMG。DMG 根目录包含应用和指向系统 `/Applications` 的 `Applications` 文件夹别名，便于拖放安装。Windows 构建同时校验 EXE 嵌入图标、Qt 图标资源和稳定的 AppUserModelID，避免打包后任务栏图标缺失或归组异常。构建包不包含个人配置、断点、封面缓存、媒体文件或登录数据；macOS 包另附 FFmpeg 构建信息。

核心文件位于 `src/tdlib_media_uploader/`：`app.py`（打包入口）、`gui/application.py`（GUI 启动与自检边界）、`gui/events.py` 与 `gui/workers.py`（GUI 事件和线程边界）、`gui/models.py`（预览模型适配）、`gui/main_window.py`（界面与生命周期）、`config/loader.py` 与 `config/paths.py`（配置和路径）、`media/legacy_video.py`、`media/legacy_image.py`、`media/legacy_mixed.py`（由 V2 策略调用的媒体实现）以及 `telegram/tdlib_common.py`（TDLib）。上传流程入口也会复用 `data/app.lock`；内部模块不提供独立启动入口。构建配置集中在根目录 `tdlib_media_uploader.spec`，平台构建只在 GitHub Actions 中执行。

版本使用“主版本.功能版本.修订版本”：日常优化增加最后一位，较大功能更新增加中间一位。版本号唯一存放在根目录 `VERSION`，程序、界面和平台构建 workflow 会从该文件读取。此次修改见 [CHANGELOG.md](CHANGELOG.md)。推送 main 只构建和测试；发布需创建与 VERSION 一致的新版本标签，既有标签和 Release 不自动覆盖。下载资产与 SHA256SUMS 放在同一目录后，可执行 `shasum -a 256 -c SHA256SUMS` 校验。

## 断点与维护

已确认的上传记录继续使用原有 JSON 格式，无需迁移。每个 Album 确认后，通过写入临时文件并原子替换保存断点；保存成功后才更新内存中的完成状态，失败时保留原记录。重置断点也使用原子替换，避免写入失败时丢失旧记录。备份或迁移 `data/state` 时先停止任务并退出应用，再复制整个目录。

清理缓存在后台执行，可以取消；取消保留尚未删除的文件，已经删除的缓存不会恢复。清理期间暂停启动扫描、上传、修改配置和处理未确认记录。关闭应用时会等待清理线程结束，以及 TDLib 返回最终数据库关闭状态；关闭超时会提示等待，稍后可重试退出。

发送前会分别校验扫描源文件和准备后的本地文件。准备期间变化、消失或无法读取的文件需重新扫描；同组最终检查失败时整组延期。转换图片和封装视频仅在源文件快照仍一致时发布可复用缓存。直接上传仍由 TDLib 异步读取源路径，因此上传期间应保持源目录不变；持续写入的目录可启用本地暂存来固定待发送副本。

## 测试与构建缓存

快速 CI 在 Linux 上执行离线行为回归和结构边界检查，不创建 Telegram 会话。平台 CI 在 Windows/macOS 上执行同一套测试、真实 FFmpeg 封装与原生路径测试、打包自测及 ZIP/DMG 校验；两类 CI 都需通过后再合并。

离线测试命令、覆盖范围及模块职责见 [开发与验证说明](docs/DEVELOPMENT.md)。

macOS CI 缓存完整 FFmpeg/FFprobe 工具目录，缓存键包括源码校验和、平台、架构和包含构建选项的 workflow 摘要。命中后仍验证工具可执行、架构、版本及许可配置；失效时重新构建。构建禁用可选外部库自动探测，避免把构建机偶然存在的动态依赖带入工具。Python 依赖继续按锁文件安装，并复用下载缓存。

## 信任与风险

建议只从本仓库的 [GitHub Releases](https://github.com/Maxwell233/tdlib-media-uploader/releases) 下载，并在运行前核对 `SHA256SUMS`。平台构建 workflow、项目许可、作者署名和第三方依赖清单都公开在仓库中；发布 ZIP 也包含许可/署名文件，便于检查来源和再分发条件。SHA-256 只能证明文件与发布者提供的摘要一致，不能替代代码审查或操作系统安全认证。

macOS 包未配置 Apple Developer 签名和公证，所以 Gatekeeper 可能显示“无法验证开发者”。请不要绕过来源核验后直接运行未知文件；确认仓库地址、标签和 SHA-256 后再按系统提示打开。Windows 版也不应被视为经过独立安全机构认证的程序。应用会调用随包提供或系统中的 FFmpeg/ExifTool 处理媒体；请确认这些工具来源和许可，并注意压缩失败、网络中断、Telegram 限制以及重复上传等运行风险。

程序需要 Telegram API ID/API Hash，并会在本机 `data/` 保存 Telegram 登录数据库、代理设置、上传断点和用户输入的标题；这些数据不会随发布包提供。不要把 `data/config.toml`、API Hash、登录数据库、代理密码或缓存发给他人。上传目标、代理、媒体内容和 Telegram 账号权限均由使用者自行确认；请遵守 Telegram 使用条款、版权要求和目标群组/频道规则。若使用 ExifTool 或自行替换 FFmpeg，还需遵守对应上游许可证。

## 项目许可与署名

本项目的原创代码、文档和界面资源采用 [GNU General Public License v3.0 only（GPL-3.0-only）](LICENSE) 发布。使用、修改和再分发时请遵守 `LICENSE` 中的版权与许可要求。

完整许可文本见根目录的 `LICENSE`，作者署名见 `ATTRIBUTION`，第三方组件清单见 `THIRD_PARTY_LICENSES.md`。本项目为独立社区项目，与 Telegram 官方无隶属关系；TDLib、PySide6、Pillow、imageio-ffmpeg、FFmpeg、PyInstaller、Python 及其他第三方组件分别遵循各自许可证。源码包和编译包均包含上述三个许可/署名文件，请勿将 `data/config.toml`、Telegram API 凭据、登录数据或本地断点状态随包分发。
