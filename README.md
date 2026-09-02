# TDLib Media Uploader v5

## 现在只需要改 config.toml

以后不要再打开两个大型 Python 主脚本修改配置。

统一配置：

```text
config.toml
```

包括：

- API_ID / API_HASH
- CHAT_ID / FORUM_TOPIC_ID
- 视频目录
- 图片目录
- ExifTool 路径
- 视频扩展名
- 图片扩展名
- 日期兜底策略
- Album 大小
- 图片排序方式
- 断点重置开关
- 完整文件列表开关
- TDLib 参数
- 新进度条显示参数

## 启动

```powershell
cd E:\tdlib-media-uploader
.\run.ps1
```

菜单：

```text
1. 上传视频
2. 上传图片
3. 打开配置文件 config.toml
4. 退出
```

所以以后想改目录、群 ID、Topic ID，直接选择：

```text
3
```

即可打开 config.toml。

## 新进度条

使用 Rich 动态面板，例如：

```text
╭─ VIDEO · TDLib 上传 ─────────────────────────────────────────╮
│ ━━━━━━━━━━━━━━━━━━━━━━━─────────   71.42%                    │
│ 速度  11.92 MiB/s  ·  100.0 Mbps    ETA  08:24             │
│ 2021-05 · Album 8/17 · 文件 73/142 · 已传 8.21 / 11.49 GiB │
╰──────────────────────────────────────────────────────────────╯
```

图片同样显示漂亮的动态面板。

默认还会同时显示：

```text
MiB/s
Mbps
```

因此可以直接和 Windows 任务管理器、宽带 Mbps 对照。

## UI 可配置

在 config.toml：

```toml
[ui]
rich_progress = true
refresh_hz = 8
bar_width = 32
show_mbps = true
transient_progress = true
```

如果某个旧终端不适合 Rich：

```toml
rich_progress = false
```

即可回退为简单进度显示。

## 视频和图片仍然不能同时运行

继续通过：

```text
run_video.ps1
run_image.ps1
```

共享 Windows Named Mutex。

所以即使误操作，也不会同时打开两个共享同一 tdlib_data / tdlib_files 的上传任务。
