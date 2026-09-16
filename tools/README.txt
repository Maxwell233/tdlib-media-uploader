ExifTool（可选）
================

V1.9.1 默认 missing_date_policy = "mtime"，因此没有 ExifTool 也能上传视频。

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
- read_dates = false 时跳过所有日期读取，自动按文件名排序并按固定数量分组，只使用文件名和自定义标题。
- 优先读取 EXIF；启用 read_media_creation_date 后，再读取媒体/QuickTime 创建日期；最后按配置使用 mtime 兜底。
- ExifTool 一次批量读取日期；已有日期的文件不会启动 FFmpeg。
- 缺少 EXIF 的视频最多同时由 4 个 FFmpeg 任务读取媒体创建日期，每个文件只启动一次。
- ExifTool 不存在：启用 read_media_creation_date 时仍会尝试 FFmpeg；失败后按缺失日期策略处理。

视频分组和标题：
- group_mode = "date" 按日期分组；group_mode = "fixed" 按扫描顺序固定分组。
- album_size 可设为 1~10；sort_mode 可在修改时间和文件名之间二选一。
- caption_include_group_title、caption_include_filenames、caption_include_filename_numbers
  分别控制组标题、文件名和文件名序号。
- “带文件名”统一只显示 stem，不显示最后的扩展名；视频、图片和混合上传均适用。

混合上传：
- 在 [paths] 设置 mixed_dir；每个一级子文件夹作为一个独立组，递归收集其中的图片和视频。
- [mixed] 的 album_size 可设为 1~10，组超过该数量会拆成多个 Telegram Album。
- 可在 [telegram.mixed] 配置独立 Chat/Topic 或频道目标；旧版配置会继承 [telegram]。
- caption_include_group_title、caption_include_filenames、caption_include_filename_numbers
  控制文件夹标题、文件名和序号。

FFmpeg（发布包内置）
====================

发布的 Windows ZIP 和 macOS DMG 已内置经过许可检查的 LGPL FFmpeg。应用仅支持从发布包
运行，不提供源码启动方式，也不需要用户自行准备 FFmpeg。

Windows 构建脚本会从 BtbN/FFmpeg-Builds 固定版本下载 Windows x64 LGPL 构建，校验
SHA-256；macOS 构建脚本从 FFmpeg 7.1.1 官方源码编译 arm64 LGPL 构建。两者都会把
LICENSE.txt 一起放进应用，并检查 `-version` 输出。不要把启用 GPL/nonfree 编码器的
FFmpeg 二进制替换进发布包；详情见根目录 THIRD_PARTY_LICENSES.md。
