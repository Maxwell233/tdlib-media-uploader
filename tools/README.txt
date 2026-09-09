ExifTool（可选）
================

V1.8.9 默认 missing_date_policy = "mtime"，因此没有 ExifTool 也能上传视频。

如果希望脚本优先读取视频内部的 EXIF，以及可选的媒体/QuickTime 创建时间，可安装 ExifTool：

官方主页 / 下载：
https://exiftool.org/

Windows 安装方式：
1. 下载 Windows Executable。
2. 将 exiftool(-k).exe 重命名为 exiftool.exe。
3. 放到本项目：
   tools/exiftool.exe
4. 如果下载包附带 exiftool_files 文件夹，也一并放入 tools/。

macOS 安装方式：
1. 从官网获取 macOS 版本的可执行文件。
2. 放到本项目：
   tools/exiftool
3. 运行：
   chmod +x tools/exiftool

Windows 最终示例：
tools/
  exiftool.exe
  exiftool_files/

macOS 最终示例：
tools/
  exiftool

配置文件默认值按平台填写：
Windows: exiftool_path = 'tools/exiftool.exe'
macOS:  exiftool_path = 'tools/exiftool'

日期规则：
- 优先读取 EXIF；启用 read_media_creation_date 后，再读取媒体/QuickTime 创建日期；最后按配置使用 mtime 兜底。
- ExifTool 不存在：在 missing_date_policy="mtime" 时使用文件修改时间。

FFmpeg（源码运行视频功能）
============================

发布的 Windows 和 macOS ZIP 已内置经过许可检查的 LGPL FFmpeg。若直接运行源码，
请使用 LGPL 构建并任选一种方式提供：

1. 放置在：
   Windows: tools/ffmpeg/ffmpeg.exe
   macOS:  tools/ffmpeg/ffmpeg
2. 或将 ffmpeg 可执行文件所在目录加入 PATH。

Windows 构建脚本会从 BtbN/FFmpeg-Builds 固定版本下载 Windows x64 LGPL 构建，校验
SHA-256；macOS 构建脚本从 FFmpeg 7.1.1 官方源码编译 arm64 LGPL 构建。两者都会把
LICENSE.txt 一起放进应用，并检查 `-version` 输出。不要把启用 GPL/nonfree 编码器的
FFmpeg 二进制替换进发布包；详情见根目录 THIRD_PARTY_LICENSES.md。
