### V1.9.1 更新

#### 界面

- 设置与诊断页面改为可滚动布局，用户数据目录和运行日志并排显示，避免窗口较小时内容重叠或被截断。
- 通用配置与扫描/外部工具设置分为两个入口；扫描稳定性、ExifTool、FFmpeg 超时和批次设置在独立页面中编辑。

#### 数据与状态可靠性

- 用户数据统一收敛到 `data/`；Telegram 登录数据库和文件统一使用 `data/telegram/database` 与 `data/telegram/files`。
- video、image、mixed 共用媒体 identity 和 UploadState；Album identity 使用扫描快照并包含 source-root scope。
- Telegram target identity 统一规范化；UNKNOWN journal 会阻止不确定发送结果被自动重复上传。

#### 网络盘和扫描

- SMB/NAS 扫描支持有界重试、目录枚举中断恢复和多次稳定快照检查。
- 扫描跳过 symlink/junction；staging 使用带 marker 的受管目录，清理时不会删除非程序文件。
- staging 对 symlink、junction 和其他 reparse point 失败关闭，降低误读或误删风险。

#### Telegram 上传

- Album 部分成功、失败、取消或超时会进入 UNKNOWN，不会自动整组重发。
- 普通账号视频精确限制为 `4000 × 524288 bytes`，Premium 精确限制为 `8000 × 524288 bytes`。
- Caption 长度按 TDLib 当前 `message_caption_length_max` 检查；单实例锁防止多个进程同时访问 TDLib、state 和 journal。

#### 排序与 Album

- video、image、mixed 共用自然数字排序；数字按整数升序，多级路径逐组件比较。
- mtime 模式按修改时间从旧到新，mtime 相同时使用同一自然路径排序作为稳定排序。
- 视频日期只用于月份分组，不覆盖组内排序；完整 Album plan 在预检前固定，deferred 文件不会导致后续文件补位或重新分组。

#### 构建与发布验证

- Windows x64 与 macOS arm64 均通过 offline regression、PyInstaller 构建、打包 `--self-test`、包验证和制品上传。
- macOS DMG 创建针对偶发的 `hdiutil Resource busy` 增加有界重试；其他错误仍会保留诊断并立即失败。
- `VERSION` 作为唯一版本来源，构建包和发布标题均从该文件读取。
