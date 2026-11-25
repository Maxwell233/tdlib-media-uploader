# TDLib Media Uploader

把本地或 SMB/NAS 目录中的视频和图片批量上传到 Telegram 超级群话题或频道。支持预览、逐组编辑标题、后台扫描和断点恢复。上传由内置 TDLib 完成，无需打开 Telegram Desktop。

**[下载最新版](https://github.com/Maxwell233/tdlib-media-uploader/releases/latest)** · [更新记录](CHANGELOG.md)

## 安装

| 平台 | 安装方式 |
| --- | --- |
| Windows x64 | 下载 ZIP，完整解压到可写目录，运行 `TDLib Media Uploader.exe`。 |
| macOS 14 及以上，Apple Silicon | 下载 arm64 DMG，把应用拖入“应用程序”。 |

两个版本均内置 FFmpeg、FFprobe 和运行环境。下载后可用 Release 中的 `SHA256SUMS` 校验文件。macOS 包未配置 Apple Developer 签名和公证，首次启动可能出现 Gatekeeper 提示。

## 第一次上传

1. 从 [Telegram 开发者页面](https://my.telegram.org/) 获取 API ID 和 API Hash，在“设置与诊断 → Telegram”中保存。这里也可以设置 SOCKS5、HTTP 或 MTProto 代理。
2. 打开“媒体上传”，选择视频、图片或混合模式，指定来源目录和 Telegram 目标。群组话题需要群组 ID 和话题 ID，频道只需要频道 ID。确认使用的账号有发送权限。
3. 点击“扫描目录”。展开媒体组检查文件，双击组标题可编辑 Caption。“跳过该组”会将它排除出本次入队。搜索和“只看待上传”只改变预览显示。
4. 点击“加入上传队列”。首次连接时按提示登录。可在“任务中心”查看进度，也可继续扫描其他目录并加入新任务。

## 媒体与分组

| 模式 | 默认分组 |
| --- | --- |
| 视频 | 按月份分组，也可按扫描顺序固定分组。 |
| 图片 | 按扫描顺序分组。 |
| 混合 | 每个一级子文件夹独立分组，递归收集其中的图片和视频。混合根目录直接放置的文件会被忽略。 |

每个 Album 可设为 1–10 个文件，标题附在组内第一条消息。兼容视频可直传，其他支持的容器可尝试无损封装为 MP4。静态图片按 Telegram Photo 规则处理，生成的副本放入缓存，源文件保持原样。视频单文件上限约为普通账号 2 GB、Premium 4 GB。

家庭或办公网络盘可在“设置与诊断 → 常规”启用本地暂存。分组、标题、格式和封面设置在“上传参数”调整。可选项与默认值见[配置模板](resources/default_config.toml)。

## 队列、暂停与恢复

- 入队会保存来源、目标、文件快照、组序和标题。文件后来变化或缺失时，按提示重新扫描。
- **暂停队列**暂停新任务调度。**安全停止**在当前发送的 Album 得到结果后停止。**立即中断**会尽快取消，已经提交但结果未知的记录会留待核对。
- 队列在重启后默认暂停。可在任务中心“重新验证并恢复”，程序会跳过已确认文件。严格顺序、发送间隔和 FloodWait 处理在“高级选项”配置。
- 遇到“待核对”时，请先在 Telegram 检查对应的 Album，再在“未确认上传”标记已发送或允许重传。部分成功时，已确认的文件保留断点。

## 数据与升级

| 平台 | 数据目录 |
| --- | --- |
| Windows | `TDLib Media Uploader.exe` 同目录下的 `data/` |
| macOS | `~/Library/Application Support/TDLib Media Uploader/data/` |

升级前退出程序并备份整个 `data/`。保留这个目录可沿用配置、Telegram 登录、标题和任务。2.0 会把旧版 JSON 队列、断点与未确认记录自动导入 `uploader.db`，原文件可作备份。

`config.toml` 可能含 API Hash 和代理凭据，`telegram/` 保存登录会话。分享日志或问题截图前，请移除这些信息以及本地路径和目标 ID。

## 开发与构建

使用 Python 3.13，安装 `requirements-build-lock.txt` 和 `requirements-test.txt`，并将 FFmpeg 与 FFprobe 加到 `PATH`。在仓库根目录运行离线检查：

```bash
python -m pip install -r requirements-build-lock.txt -r requirements-test.txt
python -m compileall -q src tests
PYTHONPATH=src QT_QPA_PLATFORM=offscreen python -m pytest tests -q
```

[GitHub Actions](.github/workflows/build-platforms.yml) 在 Windows x64 与 macOS arm64 上测试、打包和运行应用自检。推送与 `VERSION` 一致的 `v` 标签会发布 ZIP、DMG 和校验清单。安装包支持用 `--self-test` 执行离线诊断。

## 许可

[GPL-3.0-only](LICENSE) · [项目署名](ATTRIBUTION) · [第三方许可](THIRD_PARTY_LICENSES.md)
