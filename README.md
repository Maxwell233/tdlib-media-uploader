# TDLib Media Uploader

**V1.7.1 · Windows**

一个用于向 **Telegram 超级群 / Forum Topic** 批量上传图片和视频的 Windows GUI 工具。上传核心使用 [`tdjson`](https://pypi.org/project/tdjson/) 调用 TDLib 原生 C++ 网络栈，Python 负责文件扫描、分组、断点和缩略图；V1.7.1 仅保留 PySide6 桌面 GUI，并继续提供缓存清理与任务管理。

## 功能

- **视频**：递归读取目录及全部子目录；按月份组成 Album；一个 Album 最多 10 个视频；每个 Album 仅第一条显示 `yy-m`，例如 `21-5`。视频封面生成默认开启，可在配置中关闭。
- **图片**：递归读取目录及全部子目录；每 10 张组成一个 Album；不添加月份、文件名或 Caption。
- **TDLib 原生上传**：上传期间不需要打开 Telegram Desktop。
- **断点续传**：只有完整发送成功的 Album 才写入断点；重启后自动跳过已完成项。
- **统一配置**：日常只修改 `config.toml`，无需编辑 Python 主脚本。
- **PySide6 GUI**：提供概览、目录扫描、Album 预览、任务中心、历史记录、配置编辑、Telegram 目标编辑、缓存清理和环境诊断。
- **互斥保护**：图片和视频上传器共享同一份 TDLib 登录数据库，不允许同时运行。

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

克隆仓库：

```powershell
git clone https://github.com/Maxwell233/tdlib-media-uploader.git
cd tdlib-media-uploader
```

### 1. 安装

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

真实配置文件是：

```text
config.toml
```

首次安装后编辑：

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

[paths]
video_dir = 'F:\Videos'
image_dir = 'F:\Images'
```

`api_id` / `api_hash` 可在 <https://my.telegram.org/> 获取。

### 3. 运行

推荐直接双击：

```text
run.cmd
```

也可以：

```powershell
.\run.ps1
```

`run.cmd` / `run.ps1` 会直接启动 GUI。GUI 与上传核心使用相同的 `config.toml`、TDLib 登录数据和断点文件。GUI 中的“安全停止”会立即取消正在上传的文件，不会删除断点；重新扫描后，完整发送成功的 Album 会自动跳过。

## V1.7.1 GUI

GUI 使用 PySide6 构建，包含：

- 概览页：连接状态、扫描统计和快速开始；
- 视频 / 图片上传页：目录扫描、文件清单和 Album 预览；
- 任务中心：当前文件、Album、速度、Mbps、ETA 和运行日志；
- 历史记录：本地保存最近任务的结果；
- 设置与诊断：编辑主要 `config.toml` 参数、检查运行依赖并清理应用缓存；视频 / 图片页面也可直接编辑群组 Chat ID 和 Forum Topic ID。

GUI 仍然只运行一个上传任务，以保护共享的 TDLib 登录数据库。当前版本使用“立即停止 + 断点恢复”，暂不提供多账号、定时任务和并发上传。

缓存管理中的“清理所有”会清空 `.video_state`、旧版 `.state`、`.image_state`、`.thumb_cache` 和 `.gui_history.json` 的内容，但保留缓存目录；“仅清理视频封面”只清空 `.thumb_cache` 内容。两项操作都不会删除 `config.toml` 或 `tdlib_data` / `tdlib_files` 登录数据库。

## Windows EXE 构建

项目提供 PyInstaller one-folder 构建方案。推荐使用 GitHub Actions 的 Windows runner：每次推送 `main`、推送 `v*` 标签，或在仓库的 **Actions → Build Windows EXE → Run workflow** 手动运行，都会生成 Windows x64 构建产物。完成后可在对应工作流页面的 **Artifacts** 下载 ZIP；推送版本标签时，工作流还会自动创建 GitHub Release 并附加同一个 ZIP。

构建产物包含 `TDLib Media Uploader.exe` 及其依赖 DLL / Qt 插件。采用 one-folder 形式是为了让 TDLib 原生库和 FFmpeg 依赖稳定工作；首次启动时 GUI 会在 EXE 所在目录创建 `config.toml`，不会把你的配置或 Telegram 登录数据打进包里。

如果需要在本机使用同一套构建流程，需安装 Python 3.13 x64，然后运行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\build_exe.ps1 -Clean
```

输出位置：

```text
dist\TDLib Media Uploader\TDLib Media Uploader.exe
dist\TDLib Media Uploader-v1.7.1-windows-x64.zip
```

本地构建会使用独立的 `.build_venv`，不会修改日常运行的 `.venv`。EXE 构建不需要上传 `config.toml`，也不会包含 `.video_state`、`.image_state`、`.thumb_cache` 或 `tdlib_data`。

## 版权

Copyright © 2026 Maximum. All rights reserved.

本项目为独立社区项目，与 Telegram 官方无隶属关系。TDLib、PySide6、Pillow、imageio-ffmpeg 及其他第三方组件分别遵循各自许可证。源码包和编译包均包含根目录的 `COPYRIGHT` 声明；请勿将 `config.toml`、Telegram API 凭据、登录数据或本地断点状态随包分发。

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
├─ 视频 1   Caption: 21-5
├─ 视频 2
└─ ...最多 10 个

Album 2
├─ 视频 11  Caption: 21-5
└─ ...剩余视频
```

Telegram 一个媒体组最多 10 个媒体，因此超过 10 个会自动拆组。

## 图片规则

图片不添加月份或 Caption：

```text
Album 1：图片 1 ~ 10
Album 2：图片 11 ~ 20
...
```

最后不足 10 张仍正常发送；只有 1 张时自动使用单条消息。

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
├─ COPYRIGHT                      # 版权声明
├─ VERSION                        # 当前版本号
├─ config.example.toml
├─ config.toml                    # 本地生成，不提交 Git
├─ app_config.py
├─ tdlib_common.py
├─ tdlib_video_app.py             # 视频扫描与上传流程
├─ tdlib_video_album_uploader.py  # 视频上传核心
├─ tdlib_image_album_uploader.py
├─ gui_app.py                     # PySide6 GUI
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
