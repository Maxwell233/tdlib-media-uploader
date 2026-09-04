# TDLib Media Uploader

**V1.8.2 · Windows**

一个用于向 **Telegram 超级群 / Forum Topic 或 Channel** 批量上传图片和视频的 Windows GUI 工具。上传核心使用 [`tdjson`](https://pypi.org/project/tdjson/) 调用 TDLib 原生 C++ 网络栈，Python 负责文件扫描、分组、断点和缩略图；V1.8.2 仅保留 PySide6 桌面 GUI，并继续提供缓存清理、任务管理和独立代理设置。

## 功能

- **视频**：递归读取目录及全部子目录；默认按月份组成 Album，一个 Album 最多 10 个视频；也可在“编辑视频目标与配置”中开启严格模式，忽略日期并按扫描顺序每 10 个视频组成一个 Album（最后一组可少于 10 个）。每个 Album 仅第一条显示标题，可在预览中编辑基础标题并追加自定义文本。视频封面生成默认开启，可在配置中关闭。
- **图片**：递归读取目录及全部子目录；每 10 张组成一个 Album；每个 Album 的统一 Caption 默认使用 `1`、`2`、`3`…编号，并可在预览中追加自定义文本。
- **文件名描述（可选）**：可分别为视频/图片开启，在每个 Album 的统一 Caption（日期或编号）后追加当前 Album 的 `1. 文件名`、`2. 文件名`…清单；日期/编号只出现一次，默认关闭。
- **独立上传目标**：视频上传和图片上传可以分别选择超级群组 Forum Topic 或 Channel 频道，并分别填写 Chat ID / Topic；未单独配置时继承公共目标。频道模式不使用 Topic。
- **TDLib 原生上传**：上传期间不需要打开 Telegram Desktop。
- **断点续传**：只有完整发送成功的 Album 才写入断点；重启后自动跳过已完成项。
- **统一配置**：日常优先在 GUI“设置与诊断 → 编辑配置”中修改 `config.toml`，无需编辑 Python 主脚本。
- **PySide6 GUI**：提供概览、目录扫描、Album 预览、任务中心、历史记录、配置编辑、Telegram 目标编辑、缓存清理和环境诊断。
- **后台命令无弹窗**：扫描、视频预检和封面生成调用 ExifTool / FFmpeg 时隐藏 Windows 控制台窗口，不打断 GUI 操作。
- **Windows 网络目录**：支持本地路径和 `\\server\share\...` SMB/UNC 目录；扫描期间短暂不可读的文件会跳过并在界面提示，不会因为路径解析失败直接退出。
- **互斥保护**：图片和视频上传器共享同一份 TDLib 登录数据库，不允许同时运行。
- **独立代理**：可在设置中启用 SOCKS5、HTTP 或 MTProto 代理；默认关闭，关闭时明确使用直连。

## 环境

推荐：

- Windows 10 / 11 x64
- Python 3.13 x64
- PowerShell
- `tdjson==1.8.64.post1`
- Pillow
- imageio-ffmpeg
- PySide6

> 项目固定 `tdjson==1.8.64.post1`，用于规避部分较新 TDLib Python 构建中出现过的 `InputFile is not specified` 问题。

## 初次使用

### 推荐：直接下载 Release ZIP（普通使用）

Windows 用户建议直接使用已经编译好的稳定版：[TDLib Media Uploader v1.8.2 Release](https://github.com/Maxwell233/tdlib-media-uploader/releases/tag/v1.8.2)，或直接下载 [Windows x64 ZIP](https://github.com/Maxwell233/tdlib-media-uploader/releases/download/v1.8.2/TDLib.Media.Uploader-v1.8.2-windows-x64.zip)。解压后即可使用。

1. 解压整个 ZIP 文件夹，不要只复制或移动其中的 EXE；
2. 双击 `TDLib Media Uploader.exe`；
3. 首次启动后，在 GUI 的“设置与诊断”中填写 Telegram API 和本地媒体目录，再分别进入“视频上传”和“图片上传”页面编辑各自的 Telegram 目标；
4. 配置会保存在 EXE 所在目录的 `config.toml`，不需要安装 Python 或运行 `setup.cmd`。

### 可选：使用 Git 克隆源码

如果需要修改代码、查看最新提交，或希望从源码运行，可以克隆仓库：

```powershell
git clone https://github.com/Maxwell233/tdlib-media-uploader.git
cd tdlib-media-uploader
```

然后按下面的源码安装步骤创建 Python 环境。普通用户无需执行此步骤。

### 1. 源码方式安装运行环境

Windows 下推荐直接双击：

```text
setup.cmd
```

也可以在 PowerShell 中运行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\setup.ps1
```

`setup.ps1` 会创建 `.venv`、安装依赖、固定 `tdjson==1.8.64.post1`，并在本地不存在 `config.toml` 时从 `config.example.toml` 自动创建。

安装完成后，双击 `run.cmd`，或运行 `.\run.ps1` 即可打开 PySide6 GUI。项目不再提供独立的终端上传界面。

安装成功或失败后都会等待 Enter，不会一闪而过。自动化环境可使用：

```powershell
.\setup.ps1 -NoPause
```

### 2. 配置

普通使用不需要手动打开配置文件。启动 GUI 后，在左侧进入“设置与诊断”，点击“配置入口”中的“编辑配置”，填写并保存 Telegram API、视频/图片目录和代理选项即可。视频日期策略、Album 大小、严格每 10 个视频一组、Caption、文件名清单、图片排序和视频缩略图等媒体选项，分别在“视频上传”或“图片上传”页面的“编辑目标”窗口中设置。

视频 / 图片上传页面中的“编辑目标与配置”按钮可以直接修改当前上传类型的群组/频道 Chat ID、Forum Topic ID 以及该类型的 Album、Caption 和处理选项；双击扫描预览中的 Album 行可修改每个 Album 的标题。

配置实际保存在：

```text
config.toml
```

只有在 GUI 无法启动或需要自动化部署时，才需要手动编辑（源码方式示例）：

```powershell
notepad .\config.toml
```

至少填写：

```toml
[telegram]
api_id = 12345678
api_hash = "YOUR_API_HASH"
chat_id = -1001234567890
forum_topic_id = 12345
target_mode = "forum_topic" # forum_topic 或 channel
channel_chat_id = 0           # target_mode = "channel" 时填写频道 Chat ID

# 可选：按上传类型覆盖公共目标；未填写时继承 [telegram]。
# [telegram.video]
# target_mode = "forum_topic"
# chat_id = -1001234567890
# channel_chat_id = 0
# forum_topic_id = 12345
#
# [telegram.image]
# target_mode = "channel"
# chat_id = -1001234567890
# channel_chat_id = -1009876543210
# forum_topic_id = 0

[video]
force_ten_per_album = false # 开启后忽略日期，按扫描顺序每 10 个视频一组
caption_include_filenames = false

[image]
caption_include_filenames = false

[paths]
video_dir = 'F:\Videos'
image_dir = 'F:\Images'

[proxy]
enabled = false
type = "socks5" # socks5、http 或 mtproto
server = ""
port = 1080
username = ""
password = ""
secret = ""
http_only = false
```

`api_id` / `api_hash` 可在 <https://my.telegram.org/> 获取。

代理是 Telegram 连接的独立选项，由 TDLib 原生网络栈配置，不需要额外安装 Python 代理库。`enabled = false` 时程序启动前会关闭 TDLib 中当前启用的代理，确保走直连；启用后按所选类型填写服务器和端口。SOCKS5 / HTTP 可选填写用户名和密码，MTProto 需要填写十六进制 `secret`。

### 3. 运行

源码方式推荐直接双击：

```text
run.cmd
```

也可以：

```powershell
.\run.ps1
```

`run.cmd` / `run.ps1` 会直接启动 GUI。GUI 与上传核心使用相同的 `config.toml`、TDLib 登录数据和断点文件。GUI 中的“安全停止”会立即取消正在上传的文件，不会删除断点；重新扫描后，完整发送成功的 Album 会自动跳过。

## V1.8.2 GUI

GUI 使用 PySide6 构建，包含：

- 概览页：连接状态、扫描统计和快速开始；
- 视频 / 图片上传页：目录扫描、文件清单和 Album 预览；双击 Album 可编辑视频基础标题/图片编号追加文本；
- 视频 / 图片上传页面的“编辑目标与配置”窗口分别设置 Album 大小、Caption、文件名清单、排序和视频缩略图；视频页可选开启“强制每 10 个视频组成一个 Album（忽略日期）”；
- 任务中心：当前文件、Album、速度、Mbps、ETA 和运行日志；
- 历史记录：本地保存最近任务的结果；
- 设置与诊断：编辑 API、目录和独立代理、检查运行依赖并清理应用缓存；视频 / 图片页面的“编辑目标与配置”窗口分别编辑当前类型的 Telegram 目标、Album、Caption、排序和处理选项。

GUI 仍然只运行一个上传任务，以保护共享的 TDLib 登录数据库。当前版本使用“立即停止 + 断点恢复”，暂不提供多账号、定时任务和并发上传。

缓存管理中的“清理所有”会清空 `.video_state`、旧版 `.state`、`.image_state`、`.thumb_cache`、Album 标题文件和 `.gui_history.json` 的内容，但保留缓存目录；“仅清理视频封面”只清空 `.thumb_cache` 内容。两项操作都不会删除 `config.toml` 或 `tdlib_data` / `tdlib_files` 登录数据库。

## Windows EXE 构建

项目提供 PyInstaller one-folder 构建方案。推荐使用 GitHub Actions 的 Windows runner：每次推送 `main`、推送 `v*` 标签，或在仓库的 **Actions → Build Windows EXE → Run workflow** 手动运行，都会生成 Windows x64 构建产物。完成后可在对应工作流页面的 **Artifacts** 下载 ZIP；推送版本标签时，工作流还会自动创建 GitHub Release 并附加同一个 ZIP。

构建产物包含 `TDLib Media Uploader.exe` 及其依赖 DLL / Qt 插件，并嵌入 `assets\tdlib_media_uploader_icon.ico` 应用图标。便携包还内置经过 SHA-256 和构建标志检查的 LGPL FFmpeg 及其 `LICENSE.txt`。采用 one-folder 形式是为了让 TDLib 原生库和 FFmpeg 依赖稳定工作；首次启动时 GUI 会在 EXE 所在目录创建 `config.toml`，不会把你的配置或 Telegram 登录数据打进包里。

如果需要在本机使用同一套构建流程，需安装 Python 3.13 x64，然后运行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\build_exe.ps1 -Clean
```

输出位置：

```text
dist\TDLib Media Uploader\TDLib Media Uploader.exe
dist\TDLib Media Uploader-v1.8.2-windows-x64.zip
```

本地构建会使用独立的 `.build_venv`，不会修改日常运行的 `.venv`。构建脚本会自动下载固定的 BtbN Windows x64 LGPL FFmpeg，校验 SHA-256 和 `-version` 构建标志，再交给 PyInstaller；发现 `--enable-gpl` 或 `--enable-nonfree` 时会停止发布，避免把 GPL/nonfree FFmpeg 混入便携包。源码运行时，安装脚本只安装 imageio-ffmpeg 的 Python wrapper；若不使用 Release 便携包，请自行提供 LGPL `ffmpeg.exe`（放入 `tools\ffmpeg\ffmpeg.exe` 或加入 PATH）。EXE 构建不需要上传 `config.toml`，也不会包含 `.video_state`、`.image_state`、`.thumb_cache` 或 `tdlib_data`。

## ExifTool（可选）

默认：

```toml
missing_date_policy = "mtime"
```

因此 **不安装 ExifTool 也可以上传视频**，此时使用 Windows 文件修改时间。

如果希望优先使用视频内部 EXIF / QuickTime 创建时间，可安装 ExifTool：

- 官方网站 / 下载：<https://exiftool.org/>

将 Windows 版可执行文件放到：

```text
tools\exiftool.exe
```

如果安装包包含 `exiftool_files`，也一起放入 `tools\`。

当 ExifTool 存在且策略为 `mtime` 时：

```text
EXIF / QuickTime 内部时间
        ↓ 缺失
Windows 修改时间 mtime
```

如果设置：

```toml
missing_date_policy = "error"
```

则必须安装 ExifTool，且遇到无内部日期的视频时停止上传。

## 视频规则

程序会先按月份分组显示视频文件，再显示月份 / Album 计划，最后在输入 `y` 之前显示上传摘要，包括待上传数量、总大小、Album 数量、目标 Chat / Topic 和断点文件。

例如某个月有 17 个视频：

```text
Album 1
├─ 视频 1   Caption: 21-5（可编辑，例如 21-5 · 第一天）
├─ 视频 2
└─ ...最多 10 个

Album 2
├─ 视频 11  Caption: 21-5（可编辑，例如 21-5 · 第二天）
└─ ...剩余视频
```

Telegram 一个媒体组最多 10 个媒体，因此超过 10 个会自动拆组。

如果在视频页的“编辑视频目标与配置”中开启“强制每 10 个视频组成一个 Album（忽略日期）”，程序会把扫描到的视频按当前排序连续分组，不再按月份拆分：

```text
全部视频（忽略日期）
├─ Album 1：视频 1 ~ 10
├─ Album 2：视频 11 ~ 20
└─ ...
```

该选项默认关闭。开启后普通模式的 `album_size` 不参与分组，默认标题为 `Album 1`、`Album 2`…，仍可在扫描预览中逐组修改标题和追加文本；最后不足 10 个视频的一组会照常发送。

打开 `[video].caption_include_filenames = true` 后，每个 Album 的 Caption 会变为：

```text
21-5 · 第一天
1. clip_a.mp4
2. clip_b.mp4
```

## 图片规则

图片默认按 Album 编号添加 Caption：

```text
Album 1：图片 1 ~ 10，Album Caption：1
Album 2：图片 11 ~ 20，Album Caption：2
...
```

最后不足 10 张仍正常发送；只有 1 张时自动使用单条消息。

打开 `[image].caption_include_filenames = true` 后，每个 Album 的 Caption 会变为：

```text
1
1. image_a.jpg
2. image_b.jpg
```

默认排序：

```toml
sort_mode = "mtime"
```

也可使用：

```toml
sort_mode = "path"
```

## 断点续传

视频和图片分别使用独立断点目录：

```text
.video_state\
.image_state\
```

一个 Album 只有在 TDLib 确认全部消息发送成功后才写入断点。因此在 GUI 中途点击“安全停止”后重新扫描：

- 已成功并写入断点的 Album：自动跳过；
- 当前尚未完成的 Album：重新处理。

如果文件路径、大小或修改时间发生变化，文件签名也会变化，脚本会把它视为新的待上传文件。

需要清空断点时，可临时在 `config.toml` 相应区域设置：

```toml
reset_state = true
```

运行一次后请改回 `false`。

## TDLib 登录

第一次运行时 TDLib 可能要求 Telegram 手机号、验证码、两步验证密码或另一台已登录设备确认。

登录数据保存在：

```text
tdlib_data\
tdlib_files\
```

正常登录后，后续上传无需打开 Telegram Desktop 或手机 App。

## 版本规则

项目采用 `主版本.功能版本.修订版本` 的三段式版本号。

- 一般修复、UI 调整、小功能修改或其他日常更新：最后一位加 1，例如 `1.6.4 → 1.6.5`。
- 较大的功能更新或结构性更新：中间一位加 1，并将最后一位重置为 0，例如 `1.6.4 → 1.7.0`。
- 第一位仅保留给项目整体发生重大不兼容变化时使用。

每次提交实际功能或行为更新时都必须同步更新 `VERSION`、`app_config.py` 和用户可见版本信息。

## 目录结构

```text
tdlib-media-uploader\
├─ LICENSE                        # CC BY-NC 4.0 许可
├─ ATTRIBUTION                    # 作者署名
├─ THIRD_PARTY_LICENSES.md        # 第三方许可清单
├─ VERSION                        # 当前版本号
├─ config.example.toml
├─ config.toml                    # 本地生成，不提交 Git
├─ app_config.py
├─ tdlib_common.py
├─ tdlib_video_app.py             # 视频扫描与上传流程
├─ tdlib_video_album_uploader.py  # 视频上传核心
├─ tdlib_image_album_uploader.py
├─ gui_app.py                     # PySide6 GUI
├─ assets\
│  ├─ tdlib_media_uploader_icon.ico # Windows 应用图标
│  └─ tdlib_media_uploader_icon.png # 图标源图
├─ run.cmd                        # 推荐双击运行
├─ run.ps1
├─ setup.cmd                      # 推荐双击安装
├─ setup.ps1
├─ requirements.txt
├─ requirements-build.txt         # PyInstaller 构建依赖
├─ tdlib_media_uploader.spec      # one-folder 打包配置
├─ build_exe.ps1                  # 本机构建脚本
├─ tools\
│  └─ exiftool.exe                # 可选，不提交 Git
├─ tdlib_data\                   # 本地登录数据，不提交 Git
├─ tdlib_files\
├─ .video_state\                 # 视频断点
├─ .image_state\                 # 图片断点
└─ .thumb_cache\
```

### .venv 损坏或 Python 路径异常

确认已安装 Python 3.13 x64。需要重建环境时：

```powershell
Remove-Item -Recurse -Force .\.venv
.\setup.ps1
```

## 注意事项

- 不要同时运行图片和视频上传任务。
- 不要公开 `config.toml` 中的 `api_hash`。
- 不建议手动升级 `tdjson`，除非确认新版本兼容。
- 本项目用于个人 Telegram 媒体整理和批量上传，请遵守 Telegram 使用条款和目标群组规则。
- 本项目与 Telegram 官方无隶属关系。

## 项目许可与署名

本项目的原创代码、文档和界面资源采用 [Creative Commons BY-NC 4.0 国际许可](https://creativecommons.org/licenses/by-nc/4.0/) 发布：允许分享、复制、修改和再创作，但必须保留作者署名 Maximum、提供许可链接、说明修改内容，并且禁止商业用途。CC BY-NC 4.0 不是 OSI 定义的软件开源许可证，本项目采用它是为了明确非商业使用条件。

完整许可文本见根目录的 `LICENSE`，作者署名见 `ATTRIBUTION`，第三方组件清单见 `THIRD_PARTY_LICENSES.md`。本项目为独立社区项目，与 Telegram 官方无隶属关系；TDLib、PySide6、Pillow、imageio-ffmpeg、FFmpeg、PyInstaller、Python 及其他第三方组件分别遵循各自许可证，根目录许可中的非商业条件不会限制这些上游许可证授予的权利。源码包和编译包均包含上述三个许可/署名文件，请勿将 `config.toml`、Telegram API 凭据、登录数据或本地断点状态随包分发。
