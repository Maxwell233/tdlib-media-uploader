# TDLib Media Uploader

**V1.6 · Windows**

一个用于向 **Telegram 超级群 / Forum Topic** 批量上传图片和视频的脚本项目。上传核心使用 [`tdjson`](https://pypi.org/project/tdjson/) 调用 TDLib 原生 C++ 网络栈，Python 主要负责文件扫描、分组、断点和终端界面。

仓库：<https://github.com/Maxwell233/tdlib-media-uploader>

## 功能

- **视频**：递归读取目录及全部子目录；按月份组成 Album；一个 Album 最多 10 个视频；每个 Album 仅第一条显示 `yy-m`，例如 `21-5`。
- **图片**：递归读取目录及全部子目录；每 10 张组成一个 Album；不添加月份、文件名或 Caption。
- **TDLib 原生上传**：无需在上传期间打开 Telegram Desktop。
- **断点续传**：只有完整发送成功的 Album 才写入断点；重启后自动跳过。
- **统一配置**：日常使用只修改 `config.toml`，无需编辑 Python 主脚本。
- **Rich 终端界面**：文件列表、上传摘要、目标信息、Album 状态和实时速度均使用格式化面板显示。
- **互斥保护**：图片和视频上传器共享同一个 TDLib 数据库，启动脚本会阻止二者同时运行。

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

当前 PowerShell 会话允许执行本地脚本：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

安装环境：

```powershell
.\setup.ps1
```

`setup.ps1` 会：

1. 创建 `.venv`；
2. 安装依赖；
3. 固定安装 `tdjson==1.8.64.post1`；
4. 如果本地还没有 `config.toml`，自动从 `config.example.toml` 创建。

已有 `config.toml` 时不会覆盖。

## 创建和修改配置文件

仓库只提交：

```text
config.example.toml
```

真实配置文件：

```text
config.toml
```

已加入 `.gitignore`，避免把 `api_hash` 等本地配置提交到 GitHub。

如果没有运行 `setup.ps1`，也可以手动创建：

```powershell
Copy-Item .\config.example.toml .\config.toml
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

## ExifTool（可选）

V1.6 默认：

```toml
missing_date_policy = "mtime"
```

因此 **不安装 ExifTool 也能上传视频**，此时视频按 Windows 文件修改时间处理。

如果希望优先使用视频内部的 EXIF / QuickTime 创建时间，可以安装 ExifTool：

- 官方网站 / 下载：<https://exiftool.org/>

将 Windows 版可执行文件放到：

```text
tools\exiftool.exe
```

如果安装包还有 `exiftool_files`，也一并放到 `tools\`。

当 ExifTool 存在且 `missing_date_policy = "mtime"` 时，日期规则为：

```text
EXIF / QuickTime 内部时间
        ↓ 缺失
Windows 修改时间 mtime
```

如果改成：

```toml
missing_date_policy = "error"
```

则必须安装 ExifTool，并且没有内部日期的视频会停止上传。

## 运行

以后通常只需要：

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

程序会先扫描并展示文件清单，然后在 **确认输入 `y` 之前** 显示最终上传摘要，包括待上传数量、总大小、Album 数量、目标 ID 和断点文件。

## 视频规则

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

图片没有月份 Caption：

```text
Album 1：图片 1 ~ 10
Album 2：图片 11 ~ 20
...
```

最后不足 10 张仍会正常发送；只有 1 张时自动使用单条消息发送。

默认排序：

```toml
sort_mode = "mtime"
```

也可以改为：

```toml
sort_mode = "path"
```

## 断点续传

视频和图片分别使用：

```text
.state\
.image_state\
```

一个 Album 只有在 TDLib 确认全部消息发送成功后才会写入断点。

因此中途 `Ctrl + C` 后重新运行：

- 已成功并写入断点的 Album：自动跳过；
- 当前尚未完成的 Album：重新处理。

如果文件的路径、大小或修改时间发生变化，其文件签名会变化，脚本会将其视为新的待上传文件。

需要清空断点时，可临时在 `config.toml` 中设置：

```toml
reset_state = true
```

运行一次后请改回 `false`。

## TDLib 登录

第一次运行时 TDLib 可能要求：

- Telegram 手机号；
- 登录验证码；
- 两步验证密码；
- 在另一台已登录设备上确认。

登录信息保存在本地：

```text
tdlib_data\
tdlib_files\
```

这些目录均被 `.gitignore` 排除。

正常登录完成后，后续上传 **不需要打开 Telegram Desktop 或手机 App**。

## 目录结构

```text
tdlib-media-uploader\
├─ config.example.toml
├─ config.toml                # 本地生成，不提交 Git
├─ app_config.py
├─ pretty_ui.py
├─ tdlib_common.py
├─ tdlib_video_album_uploader.py
├─ tdlib_image_album_uploader.py
├─ run.ps1
├─ run_video.ps1
├─ run_image.ps1
├─ setup.ps1
├─ requirements.txt
├─ tools\
│  └─ exiftool.exe            # 可选，不提交 Git
├─ tdlib_data\                # 本地登录数据，不提交 Git
├─ tdlib_files\
├─ .state\
├─ .image_state\
└─ .thumb_cache\
```

## 注意事项

- 不要同时运行图片和视频上传任务；两个 `run_*.ps1` 已使用 Windows Named Mutex 防止并发启动。
- 不要公开 `config.toml` 中的 `api_hash`。
- 不建议手动升级 `tdjson`，除非确认新版本兼容。
- 本项目用于个人 Telegram 媒体整理和批量上传；请遵守 Telegram 的使用条款和目标群组规则。
- 本项目与 Telegram 官方无隶属关系。
