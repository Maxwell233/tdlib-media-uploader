# TDLib Media Uploader

**V1.6 · Windows**

一个用于向 **Telegram 超级群 / Forum Topic** 批量上传图片和视频的脚本项目。上传核心使用 [`tdjson`](https://pypi.org/project/tdjson/) 调用 TDLib 原生 C++ 网络栈，Python 负责文件扫描、分组、断点、缩略图和终端界面。

仓库：<https://github.com/Maxwell233/tdlib-media-uploader>

## 功能

- **视频**：递归读取目录及全部子目录；按月份组成 Album；一个 Album 最多 10 个视频；每个 Album 仅第一条显示 `yy-m`，例如 `21-5`。
- **图片**：递归读取目录及全部子目录；每 10 张组成一个 Album；不添加月份、文件名或 Caption。
- **TDLib 原生上传**：上传期间不需要打开 Telegram Desktop。
- **断点续传**：只有完整发送成功的 Album 才写入断点；重启后自动跳过已完成项。
- **统一配置**：日常只修改 `config.toml`，无需编辑 Python 主脚本。
- **Rich 终端界面**：文件列表、上传计划、确认摘要、实时速度和 Album 状态均使用格式化面板显示。
- **互斥保护**：图片和视频上传器共享同一份 TDLib 登录数据库，不允许同时运行。

## 环境

推荐：

- Windows 10 / 11 x64
- Python 3.13 x64
- PowerShell
- `tdjson==1.8.64.post1`
- Pillow
- imageio-ffmpeg
- Rich

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

安装成功或失败后都会等待 Enter，不会一闪而过。自动化环境可使用：

```powershell
.\setup.ps1 -NoPause
```

### 2. 配置

真实配置文件是：

```text
config.toml
```

它不会提交到 GitHub。首次安装后编辑：

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

菜单：

```text
[1] 上传视频
[2] 上传图片
[3] 编辑 config.toml
[4] 退出
```

V1.6 的启动器会一直保留主菜单。视频 / 图片上传器在独立 PowerShell 子进程中运行，因此子脚本中的 `exit` 或 Python 异常不会再把主菜单窗口一起关闭。上传器结束后按 Enter 返回主菜单。

如果主启动器本身异常退出，`run.cmd` 会保留窗口并显示退出代码，便于查看报错。

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

程序会先显示完整文件列表和月份 / Album 计划，最后在输入 `y` 之前显示上传摘要，包括待上传数量、总大小、Album 数量、目标 Chat / Topic 和断点文件。

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

视频和图片现在分别使用：

```text
.video_state\
.image_state\
```

V1.6 之前的视频断点目录是：

```text
.state\
```

第一次使用新版视频入口时，程序会自动把旧 `.state\upload_state_*.json` 中尚未迁移的断点复制到 `.video_state\`，因此无需重新上传已经完成的视频。旧 `.state` 会保留作为兼容备份。

一个 Album 只有在 TDLib 确认全部消息发送成功后才写入断点。因此中途 `Ctrl + C` 后重新运行：

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

## 目录结构

```text
tdlib-media-uploader\
├─ config.example.toml
├─ config.toml                    # 本地生成，不提交 Git
├─ app_config.py
├─ pretty_ui.py
├─ tdlib_common.py
├─ tdlib_video_app.py             # V1.6 视频 UI / 流程
├─ tdlib_video_album_uploader.py  # 视频上传核心
├─ video_bootstrap.py             # .video_state / 旧断点迁移入口
├─ tdlib_image_album_uploader.py
├─ run.cmd                        # 推荐双击运行
├─ run.ps1
├─ run_video.ps1
├─ run_image.ps1
├─ setup.cmd                      # 推荐双击安装
├─ setup.ps1
├─ requirements.txt
├─ tools\
│  └─ exiftool.exe                # 可选，不提交 Git
├─ tdlib_data\                    # 本地登录数据，不提交 Git
├─ tdlib_files\
├─ .video_state\                 # 视频断点
├─ .image_state\                 # 图片断点
├─ .state\                       # 旧版视频断点，仅兼容迁移
└─ .thumb_cache\
```

## 常见问题

### run.cmd 一闪而过

当前 V1.6 已修正启动流程：

- `run.ps1` 使用循环主菜单，不会在一个上传任务返回后直接结束；
- 视频 / 图片脚本使用独立 PowerShell 子进程；
- Python 非零退出代码会向上传启动器传播；
- `run.cmd` 在主启动器异常退出时会自动 `pause`，错误不会消失。

如果仍有问题，可在已经打开的 PowerShell 中运行：

```powershell
.\run.ps1
```

这样可以完整查看错误。

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
- `tdlib_data`、`tdlib_files`、`.video_state` 和 `.image_state` 都是本地运行数据，不应提交到 Git。
- 本项目用于个人 Telegram 媒体整理和批量上传，请遵守 Telegram 使用条款和目标群组规则。
- 本项目与 Telegram 官方无隶属关系。
