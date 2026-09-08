# TDLib Media Uploader

**V1.8.6 · Windows x64 + macOS arm64 桌面应用**

把本地或网络目录中的视频、图片批量上传到 Telegram 群组话题或频道。Windows x64 与 macOS Apple Silicon arm64 使用同一套 GUI、上传核心和功能配置；支持上传预览、标题编辑、断点恢复和独立代理，上传时无需打开 Telegram Desktop。

## 开始使用

已发布的便携包在 [Releases](https://github.com/Maxwell233/tdlib-media-uploader/releases)。Windows 下载 x64 ZIP，完整解压后运行 `TDLib Media Uploader.exe`；macOS 下载 arm64 ZIP，解压后打开 `TDLib Media Uploader.app`，两者都无需安装 Python。下载后可用 Release 中的 `SHA256SUMS` 校验 ZIP；源码中的修改可能尚未打包发布。

macOS 首次打开若出现“无法验证开发者”等提示，请先确认下载地址和 SHA-256，再在 Finder 中右键点按应用并选择“打开”。当前包没有 Apple Developer 签名/公证，见下方[信任与风险](#信任与风险)。

1. 打开“设置与诊断 → 编辑配置”，填写 API ID 和 API Hash。凭据可从 [Telegram 开发者页面](https://my.telegram.org/) 获取。
2. 进入“视频上传”或“图片上传”，选择目录，或粘贴目录后按 Enter 保存。
3. 点击“编辑视频目标与配置”或“编辑图片目标与配置”，填写群组及话题 ID，或频道 ID。两种媒体可以使用不同目标。
4. 扫描目录，核对待上传文件；选中媒体组点击“编辑标题”，或双击该组/文件进行编辑。
5. 点击“开始上传”并确认。在“任务中心”查看进度、速度、剩余时间和日志。

首次上传可能要求手机号、验证码、两步验证密码或其他已登录设备确认。Windows 和源码运行时，登录数据保存在项目目录；macOS 发布版保存在 `~/Library/Application Support/TDLib Media Uploader/`，下次可继续使用。

## 预览与编辑

- 图片直接显示“媒体组 → 文件”；视频显示“月份 → 媒体组 → 文件”。展开媒体组查看完整文件列表，鼠标停留在标题上可查看完整说明。
- 搜索支持文件名、路径和标题；“只看待上传”隐藏已完成文件。**筛选只改变显示，不改变上传范围。**
- 标题和追加文字在一个窗口内编辑，支持多行文字及实时预览。图片保留原有编号规则。
- 修改目录、目标或分组配置后，需要重新扫描。任务结束后也需重新扫描，以读取最新断点。
- 扫描或上传期间不能修改配置；同一时间只运行一个上传任务。

## 上传规则

“媒体组”即 Telegram Album；“标题”指媒体说明文字 Caption。

| 选项 | 视频 | 图片 |
| --- | --- | --- |
| 扫描范围 | 目录及所有子目录 | 目录及所有子目录 |
| 默认分组 | 按月份，每组最多 10 个 | 按排序，每组最多 10 张 |
| 每组数量 | 可设为 1–10 | 可设为 1–10 |
| 默认标题 | 月份，如 `21-5` | 编号，如 `1`、`2` |
| 标题编辑 | 基础标题及追加文字 | 编号后追加文字；可关闭编号 |
| 文件名清单 | 可开启，默认关闭 | 可开启，默认关闭 |
| 其他选项 | 日期策略、强制十个一组、视频封面 | 按修改时间或完整路径排序 |

每组仅第一条消息显示统一标题。最后不足一组也会发送；只有一个文件时发送单条消息。

视频开启“强制每 10 个视频组成一个 Album”后，忽略月份，按扫描顺序连续分组，普通组大小不参与分组。默认标题为 `Album 1`、`Album 2` 等，仍可逐组编辑。

可选文件名清单附加在统一标题后，例如：

```text
21-5 · 第一天
1. clip_a.mp4
2. clip_b.mp4
```

## 日期与视频工具

日期优先级为 **EXIF / QuickTime 信息 → 媒体创建日期 → 文件修改日期**。没有 ExifTool 或其日期不可用时，使用发布包自带的 FFmpeg 读取视频内部 `creation_time` 等媒体创建字段，Windows 和 macOS 使用相同逻辑。它对应“创建媒体日期”一类内容元数据，不是复制文件后可能变化的文件“创建时间”。

默认 `missing_date_policy = "mtime"` 仅在以上内部日期都不可用时使用文件修改时间；`error` 则停止并提示缺失日期。UTC 媒体创建日期按本机时区显示，也可用 `quicktime_utc_target_zone` 指定时区。预览文件行的日期提示会标明来源。已有 EXIF 日期的文件无需额外运行 FFmpeg；媒体日期结果按路径、大小和修改时间缓存。

从 [ExifTool 官网](https://exiftool.org/) 下载对应平台版本。Windows 将程序放到 `tools/exiftool.exe`；macOS 可将可执行文件放到 `tools/exiftool` 并执行 `chmod +x tools/exiftool`。若附带 `exiftool_files`，一起放入 `tools/`；也可在设置中指定工具路径。

视频封面默认开启，可以在视频配置中关闭。源码运行请提供不含 GPL/nonfree 组件的 LGPL FFmpeg：Windows 放入 `tools/ffmpeg/ffmpeg.exe`，macOS 放入 `tools/ffmpeg/ffmpeg`，或加入 PATH。便携包自带构建时校验的 FFmpeg；后台调用工具时会隐藏 Windows 控制台窗口。

## 断点、停止与缓存

“安全停止”会取消当前上传，并保留已写入的断点。只有整组消息确认发送成功后才记录完成状态。重新扫描时，已完成的组自动跳过；尚未完整完成的组会重新处理，其中已发送的部分可能重复。

文件路径、大小或修改时间变化后，会被视为新文件。视频与图片分别使用 `.video_state`、`.image_state`；旧版视频状态可能在 `.state`。

“设置与诊断”提供两类清理：

- **仅清理视频封面**：删除 `.thumb_cache` 的生成文件，之后需要时重新生成。
- **清理所有**：清空视频/图片断点、旧版断点、封面、媒体组标题和本地任务历史。保留缓存目录、`config.toml` 及 Telegram 登录数据。清理断点后重新上传可能产生重复消息。

也可在相应媒体配置中临时设置 `reset_state = true`；运行一次后务必改回 `false`。

历史记录保留最近 100 次任务。Telegram 登录数据位于 `tdlib_data` 和 `tdlib_files`；标题修改存放在 `.video_album_captions.json` / `.image_album_captions.json`，独立于上传断点。macOS 发布版会将这些可写数据放到上面的 Application Support 目录，避免写入只读的 `.app` 包。

## 配置与代理

配置保存在应用目录的 `config.toml`，首次运行从 `config.example.toml` 创建。日常通过界面编辑；完整键名、默认值和说明见 [配置模板](config.example.toml)。保存无效配置时恢复原文件。

- 通用 API、目录、ExifTool 和代理：在“设置与诊断”编辑。
- 视频/图片目标、分组、标题和处理选项：在相应上传页面编辑。
- 目标未单独配置时继承 `[telegram]`；单独目标位于 `[telegram.video]` 和 `[telegram.image]`。频道模式不使用 Topic。
- 代理支持 SOCKS5、HTTP、MTProto，默认关闭并使用直连。SOCKS5/HTTP 可填写用户名与密码；MTProto 需要 Secret。代理由 TDLib 配置，无需额外代理库。

支持本地目录及 `\\server\share\...` 网络目录。扫描时不可读取的项目会跳过并提示；网络恢复后可重新扫描。

## 从源码运行

Windows 源码运行（Windows 10/11 x64、Python 3.13 x64、PowerShell）：

```powershell
git clone https://github.com/Maxwell233/tdlib-media-uploader.git
cd tdlib-media-uploader
.\setup.cmd
.\run.cmd
```

安装脚本创建 `.venv`、安装依赖并创建本地配置。也可用 `setup.ps1` / `run.ps1`；若执行策略阻止脚本，可在当前 PowerShell 进程运行 `Set-ExecutionPolicy -Scope Process Bypass`。自动安装可用 `.\setup.ps1 -NoPause`。

依赖为 `tdjson==1.8.64.post1`、Pillow、imageio-ffmpeg、PySide6。固定 TDLib 版本是为了兼容现有上传实现，请勿随意升级。源码和便携包都使用图形界面。

macOS 源码运行（Apple Silicon、Python 3.13）：

```bash
git clone https://github.com/Maxwell233/tdlib-media-uploader.git
cd tdlib-media-uploader
./setup.sh
./run.sh
```

也可以双击 `setup.command` 和 `run.command`。源码运行的视频封面需要自行准备 LGPL FFmpeg；发布包已在构建阶段准备并检查。

离线回归测试（安装依赖后运行）：

```powershell
python -m unittest discover -s tests -v
```

如 `.venv` 损坏，可删除项目中的 `.venv` 后重新运行对应平台安装脚本。

## 构建 Windows 和 macOS 包

Windows：

```powershell
.\build_exe.ps1 -Clean
```

macOS Apple Silicon：

```bash
./build_macos.sh --clean
```

Windows 和 macOS 构建使用各自平台的原生 runner，并行执行离线回归测试。Windows 脚本下载固定的 BtbN LGPL FFmpeg；macOS 脚本从 FFmpeg 7.1.1 官方源码构建 arm64 FFmpeg，使用 `--disable-gpl --disable-nonfree`，并检查二进制架构、构建标志和许可文件。两条构建路径都会在 PyInstaller 前排除 `imageio-ffmpeg` wheel 自带的 FFmpeg 二进制。

输出为 Windows 的 `dist/TDLib Media Uploader/TDLib Media Uploader.exe` 及 Windows x64 ZIP，和 macOS 的 `dist/TDLib Media Uploader.app` 及带版本号的 macOS arm64 ZIP。构建包不包含个人配置、断点、封面缓存、媒体文件或登录数据；macOS 包另附 FFmpeg 构建信息。

核心文件：`gui_app.py`（界面）、`app_config.py`（配置）、`tdlib_video_app.py`（视频流程）、两个 `*_album_uploader.py`（媒体上传）、`tdlib_common.py`（TDLib）、`album_metadata.py`（标题）、`path_utils.py`（路径和扫描）。

版本使用“主版本.功能版本.修订版本”：日常优化增加最后一位，较大功能更新增加中间一位。更新时同步 `VERSION`、`app_config.py` 和界面版本信息。此次修改见 [CHANGELOG.md](CHANGELOG.md)。

## 信任与风险

建议只从本仓库的 [GitHub Releases](https://github.com/Maxwell233/tdlib-media-uploader/releases) 下载，并在运行前核对 `SHA256SUMS`。构建脚本、平台构建 workflow、项目许可、作者署名和第三方依赖清单都公开在仓库中；发布 ZIP 也包含许可/署名文件，便于检查来源和再分发条件。SHA-256 只能证明文件与发布者提供的摘要一致，不能替代代码审查或操作系统安全认证。

macOS v1.8.6 包未配置 Apple Developer 签名和公证，所以 Gatekeeper 可能显示“无法验证开发者”。请不要绕过来源核验后直接运行未知文件；确认仓库地址、标签和 SHA-256 后再按系统提示打开。Windows 版也不应被视为经过独立安全机构认证的程序。

程序需要 Telegram API ID/API Hash，并会在本机保存 Telegram 登录数据库、代理设置、上传断点和用户输入的标题；这些数据不会随发布包提供。不要把 `config.toml`、API Hash、登录数据库、代理密码或缓存发给他人。上传目标、代理、媒体内容和 Telegram 账号权限均由使用者自行确认；请遵守 Telegram 使用条款、版权要求和目标群组/频道规则。若使用 ExifTool 或自行替换 FFmpeg，还需遵守对应上游许可证。

## 项目许可与署名

本项目的原创代码、文档和界面资源采用 [Creative Commons BY-NC 4.0 国际许可](https://creativecommons.org/licenses/by-nc/4.0/) 发布：允许分享、复制、修改和再创作，但必须保留作者署名 Maximum、提供许可链接、说明修改内容，并且禁止商业用途。CC BY-NC 4.0 不是 OSI 定义的软件开源许可证，本项目采用它是为了明确非商业使用条件。

完整许可文本见根目录的 `LICENSE`，作者署名见 `ATTRIBUTION`，第三方组件清单见 `THIRD_PARTY_LICENSES.md`。本项目为独立社区项目，与 Telegram 官方无隶属关系；TDLib、PySide6、Pillow、imageio-ffmpeg、FFmpeg、PyInstaller、Python 及其他第三方组件分别遵循各自许可证，根目录许可中的非商业条件不会限制这些上游许可证授予的权利。源码包和编译包均包含上述三个许可/署名文件，请勿将 `config.toml`、Telegram API 凭据、登录数据或本地断点状态随包分发。
