# 第三方许可清单

审计基线：`requirements.txt`、`requirements-build.txt`、PyInstaller one-folder
Windows x64 构建，以及源码中实际导入的模块。审计日期：2026-09-04。

本文件是随源码和便携 ZIP 分发的许可索引，不替代各上游项目的完整许可文本。
第三方组件不受根目录 `LICENSE` 的 CC BY-NC 4.0 条件覆盖；下表中的组件仍按
自己的许可证使用。发布新版本或升级依赖时，应重新核对对应版本的上游文本。

## 结论

- 没有发现项目源码主动使用 GPL 或 AGPL 代码，也没有发现会把本项目原创代码
  强制改成 GPL/AGPL 的“强传染性”依赖。
- `PySide6/Qt` 和 FFmpeg 属于需要特别处理的弱传染性运行库：它们不会自动要求
  本项目源码改用 GPL，但便携包必须保留许可说明，并且不能阻止用户替换 LGPL
  运行库。
- `imageio-ffmpeg` 的 Windows wheel 会附带一个 FFmpeg 可执行文件；该 wheel 的
  构建选项并不适合作为本项目的固定发布来源。因此安装脚本使用 `--no-binary`
  只安装 Python wrapper，Windows 便携构建则固定下载 BtbN 的 LGPL 版本并检查
  SHA-256、`-version` 构建标志和随包的 `LICENSE.txt`。
- FFmpeg 的具体许可取决于构建选项。当前固定构建没有 `--enable-gpl` 或
  `--enable-nonfree`，并随包提供 LGPLv3 文本；若以后更换构建，必须重新审计。
- PyInstaller 的 bootloader 带有 GPL Bootloader Exception，可将 bootloader
  嵌入组合程序；这不会把本项目源码变成 GPL。PyInstaller 仅是构建依赖，但其
  例外说明仍列入本清单以便追溯。
- CC BY-NC 4.0 的非商业条件只针对本项目原创内容。它不是 OSI 定义的软件
  开源许可证，因此不能替代或覆盖 LGPL、Boost、MIT、BSD、PSF 等第三方条款。

## 组件矩阵

| 组件 | 版本/来源 | 上游许可证 | 是否强传染 | 对本项目的影响与处理 |
| --- | --- | --- | --- | --- |
| `tdjson` | `1.8.64.post1`，`requirements.txt` | MIT（Python 绑定；wheel 内含预编译 TDLib） | 否 | 保留 MIT 条款；同时遵守 wheel 内 TDLib 的独立许可证。 |
| TDLib | `tdjson` wheel 内的原生库 | Boost Software License 1.0 | 否 | 允许组合和再分发；保留上游许可链接。 |
| Pillow | `>=11.0` | MIT-CMU | 否 | 许可宽松，但分发时保留上游许可与署名说明。 |
| `imageio-ffmpeg` | `>=0.6.0`；安装脚本使用 `--no-binary imageio-ffmpeg` | BSD 2-Clause | 否 | 仅使用 Python wrapper；不把其 wheel 内置的 FFmpeg 二进制放进发布包。 |
| FFmpeg | BtbN `autobuild-2026-09-03-13-17`，`ffmpeg-N-126390-g9fc8c785e2-win64-lgpl.zip` | LGPLv3（随包 `tools\ffmpeg\LICENSE.txt`） | 否，LGPL 为弱传染 | 构建 SHA-256：`ba8bf7dec00022c2dbf2cbeb9a601d7e0d131990e276b8c5f88954775735ec8a`；构建标志不含 `--enable-gpl` / `--enable-nonfree`。本项目通过子进程调用可执行文件，不与 Python 源码静态合并；发布包保留许可证、固定资产来源和可替换/源码获取信息。 |
| PySide6 / Qt for Python | `>=6.8.0` | Community Edition：LGPLv3/GPLv3（另有商业版） | LGPL 为弱传染；GPL 模块为强传染 | 当前代码只导入 `QtCore`、`QtGui`、`QtWidgets`；不要新增 GPL-only Qt 模块。便携包不能阻止替换 Qt DLL，并应保留许可入口。 |
| PyInstaller | `6.22.2`，仅构建依赖 | GPL-2-or-later + Bootloader Exception；运行时 hook 为 Apache-2.0 | 例外覆盖嵌入 bootloader | one-folder EXE 可继续使用；不把 PyInstaller 的普通 GPL 源码当作项目代码分发。 |
| Python | 3.13 运行时/构建环境 | PSF License 2.0 | 否 | PyInstaller 可能带入 Python 运行时；保留 Python 许可来源。 |
| ExifTool | 可选外部工具，不随仓库提交 | 由 ExifTool 上游另行规定 | 不纳入本包审计 | 用户自行放入 `tools\exiftool.exe` 时，应同时遵守 ExifTool 的分发条款。 |

Qt、TDLib 和 FFmpeg 的发行包还可能包含由其他作者提供的 ICU、OpenSSL、zlib
或编解码器等材料；这些材料不一定与主项目使用同一许可证。它们的具体范围
取决于实际 wheel、Qt DLL 和 FFmpeg 构建，本清单通过上游来源链接指向对应的
完整清单，不把这些二进制误归入 CC BY-NC 4.0。

## 分发检查

每个源码包和便携 ZIP 都应包含：

1. `LICENSE`（本项目原创内容的 CC BY-NC 4.0 条件）；
2. `ATTRIBUTION`（Maximum 署名）；
3. `THIRD_PARTY_LICENSES.md`（本清单）；
4. 随 Qt、TDLib、Pillow、`imageio-ffmpeg` 和 FFmpeg 二进制一起提供的上游
   许可/来源信息，或指向对应版本完整文本和源码的可访问链接；便携 ZIP 还应
   包含 BtbN FFmpeg 的 `LICENSE.txt`。

`build_exe.ps1` 会在打包前下载并校验固定的 LGPL FFmpeg，然后执行 `ffmpeg -version`
检查；如果输出包含 `--enable-gpl` 或 `--enable-nonfree`，构建会失败，不生成可
发布 ZIP。这样可把 FFmpeg 的条件性强传染风险挡在发布流程之外。

发布者不要把 `config.toml`、Telegram 登录数据、上传断点或本地媒体文件放入
发行包。若升级未锁定的依赖，需重新执行本清单审计，尤其是 PySide6 的模块
许可和 FFmpeg wheel 的构建选项。

## 上游来源

- [`tdjson` 1.8.64.post1（PyPI）](https://pypi.org/project/tdjson/1.8.64.post1/)
- [TDLib 许可证](https://github.com/tdlib/td/blob/master/LICENSE_1_0.txt)
- [Pillow 许可证](https://github.com/python-pillow/Pillow/blob/main/LICENSE)
- [`imageio-ffmpeg` 许可证](https://github.com/imageio/imageio-ffmpeg/blob/main/LICENSE)
- [FFmpeg 许可与合规说明](https://ffmpeg.org/legal.html)
- [BtbN Windows LGPL 构建发布页](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-09-03-13-17)
- [BtbN/FFmpeg-Builds 构建说明与源码入口](https://github.com/BtbN/FFmpeg-Builds)
- [Qt for Python 许可说明](https://doc.qt.io/qtforpython-6/)
- [Qt 组件许可说明](https://doc.qt.io/qt-6/licensing.html)
- [PyInstaller COPYING 与 Bootloader Exception](https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt)
- [Python 许可说明](https://docs.python.org/3/license.html)
