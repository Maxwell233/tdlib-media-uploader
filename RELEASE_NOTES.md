# TDLib Media Uploader V1.9.3

## 版本概览

V1.9.3 是 1.9.3 beta1 至 beta5 的正式稳定版。这个版本完成了从底层 TDLib 上传安全、断点恢复和文件扫描，到 GUI 架构、设置中心和跨平台打包的连续重构，并在正式发布前完成了最后一轮全面审计。

本次正式发布的重点是：让大批量上传更可恢复，让异常结果更可核对，让扫描和预览更可信，让 Windows 与 macOS 用户使用同一套稳定的桌面体验。

## 上传可靠性与 TDLib 交互

- **混合媒体 Album 统一构建**：图片与视频使用一致的 Album 规划和 payload 构建路径，Photo、Video、缩略图、cover 以及嵌套 `InputFile` 会在发送前递归校验；无效或空的 `photo`/`video` 不会提交给 TDLib，也不会提前写入发送 journal。
- **稳定的 Album identity**：视频、图片和混合上传统一使用扫描快照、source-root scope 和媒体签名生成 Album identity。文件在扫描后发生变化时会被识别，而不是悄悄上传错误内容；caption、状态和 in-flight journal 可以稳定复用。
- **完整计划优先于预检结果**：Album 边界在上传前固定，文件预检或延迟不会重新填充后续 Album，避免断点恢复时分组漂移。
- **视频日期与分组**：支持 ExifTool、媒体创建日期和文件修改时间的有界回退；关闭日期读取时保持 `capture_time = None`，按文件名和固定数量分组，不会伪造日期。
- **文件限制与媒体处理**：视频使用 Telegram 标准账号/Premium 精确大小边界，图片支持超限压缩；FFmpeg、ExifTool 和 readiness 检查均有超时、取消和重试边界。
- **TDLib 版本兼容**：正式版固定使用 `tdjson 1.8.64.post1`，与当前上传核心和 GUI 兼容层保持一致。

## 断点恢复、停止语义与状态安全

- **上传状态可靠保存**：视频、图片和混合模式共享稳定的媒体 identity 与 UploadState；同一 Album 使用统一的 `sent_at`，已完成文件不会因路径显示变化而丢失状态。
- **未确认上传保护**：部分成功、失败、取消或超时的 Album 会进入 `UNKNOWN`，不会自动整组重发；`InflightJournal` 的解析、截断、权限错误和确认恢复均采用 fail-closed 处理。
- **两种停止方式**：安全停止会等待当前 Album 完成，立即中断会明确标记可能存在的未知发送结果，并保留人工核对入口。
- **文件与目录安全**：网络盘扫描使用有界 retry 和 readiness 快照；symlink、junction 和其他 reparse point 不会被跟随；受管 staging 清理不会误删用户文件；单实例锁防止多个进程同时修改 TDLib、state 或 journal。
- **跨平台路径兼容**：Windows 驱动器路径、UNC 路径、特殊字符、Unicode 文件名和超长路径使用稳定的非解析 identity 语义。

## GUI 架构与使用体验

- **模块化 GUI 服务层**：扫描、配置、缓存、历史和工具逻辑从主窗口拆分为独立服务，页面不再反向探测主窗口，降低循环依赖并改善测试隔离。
- **统一媒体上传工作台**：视频、图片和混合上传整合到同一工作台，通过分段控制器切换模式；预览树、任务中心、未确认上传和历史记录形成完整的任务闭环。
- **现代主题系统**：支持深色/浅色模式实时切换、High-DPI/Retina SVG 图标、统一语义颜色和高对比度文本选区；主题变化会同步刷新整个页面树。
- **大列表与线程生命周期**：扫描线程支持协作式取消和关闭等待，缓存统计移至后台线程，预览树使用批量渲染挂起，适合数千文件的目录。
- **自然排序性能**：文件名和路径采用逐组件自然排序；大规模排序改用原生 tuple key，减少 Python 比较器开销。

## 设置中心与上传操作

- **六类设置全部内联**：常规、Telegram、上传参数、高级选项、存储与缓存、环境与许可均可在设置页直接编辑，不再依赖层层弹窗。
- **可靠的保存体验**：表单显示真实运行配置，自动追踪 dirty 状态，支持恢复未保存修改、就地校验和无损写回 `config.toml`。
- **上传参数分组**：视频、图片和混合参数通过 Segmented Switcher 切换；统一的 `TelegramTargetEditor` 负责目标 Chat、频道和话题配置。
- **标题编辑简化**：移除重复的工具栏编辑入口，支持双击或右键媒体组编辑 caption，并保留文件名描述和 caption 长度校验。
- **凭据安全**：API Hash、代理密码和 MTProto Secret 默认掩码显示；空白或示例 API Hash 会在保存、加载和开始上传前被拒绝。
- **环境诊断与许可**：依赖检测、运行环境、GPL-3.0-only 许可、GitHub 项目入口和第三方依赖信息集中展示。

## 正式发布前最终审计

最终审计覆盖上传主流程、TDLib 调用、GUI 线程与取消、状态保存/恢复、文件扫描和元数据、路径编码、Windows 兼容性、配置迁移、日志隐私、性能、测试、CI、PyInstaller 和资源清理。

审计阶段额外修复了：

- legacy 扫描器 Album key 参数错误、缺少快照和旧格式分组导致的预览崩溃；
- fallback 预览与实际上传规划器之间的分组大小、source root、Album 编号和 caption identity 漂移；
- 上传验证开关、FFmpeg 压缩超时配置写入错误键导致的设置失效；
- 扫描后文件大小显示漂移、配置错误状态不刷新和历史记录直接覆盖造成的 JSON 损坏风险；
- 配置对话框和上传入口对空白/示例 API Hash 的放行问题。

## 验证与发布包

- 完整离线回归测试：**323 项全部通过**。
- Windows x64：PyInstaller 构建、打包自测、EXE 图标、FFmpeg、许可证文件和 ZIP 校验全部通过。
- macOS Apple Silicon arm64：PyInstaller 构建、打包自测、架构检查、FFmpeg、许可证文件和 DMG 校验全部通过。
- 发布包包含 `SHA256SUMS`；macOS 应用未配置 Apple Developer 签名和公证。

## 下载

- Windows：下载 `TDLib.Media.Uploader-v1.9.3-windows-x64.zip`，解压后运行 `TDLib Media Uploader.exe`。
- macOS：下载 `TDLib.Media.Uploader-v1.9.3-macos-arm64.dmg`，打开后将应用拖到 `Applications` 文件夹。
- 下载后使用 `SHA256SUMS` 校验 ZIP 和 DMG 的完整性。
