ExifTool（可选）
================

V1.7.0 默认 missing_date_policy = "mtime"，因此没有 ExifTool 也能上传视频。

如果希望脚本优先读取视频内部的 EXIF / QuickTime 创建时间，可安装 ExifTool：

官方主页 / 下载：
https://exiftool.org/

Windows 常见安装方式：
1. 下载 Windows Executable。
2. 将 exiftool(-k).exe 重命名为 exiftool.exe。
3. 放到本项目：
   tools\exiftool.exe
4. 如果下载包附带 exiftool_files 文件夹，也一并放入 tools\。

最终示例：
tools\
  exiftool.exe
  exiftool_files\

配置文件默认：
exiftool_path = 'tools\exiftool.exe'

日期规则：
- ExifTool 存在：优先读取 EXIF/QuickTime；缺失时 mtime 兜底。
- ExifTool 不存在：在 missing_date_policy="mtime" 时全部使用 Windows 修改时间。
