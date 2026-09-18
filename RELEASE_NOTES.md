### V1.9.3-beta2 更新

#### 上传安全与停止语义

- 未确认上传页面以文件名/Album 摘要为主显示，并提供完整源路径详情、状态翻译、Telegram 目标、文件数、更新时间和错误信息；legacy 与 corrupt journal 均有明确 fallback。
- `CONFIRMED` 仅允许修复本地 UploadState checkpoint，禁止走“Telegram 中不存在”路径；恢复失败保留 journal，避免重复上传。
- `CONFIRMED` checkpoint 恢复失败按本地断点待修复处理，GUI 明确提示 Telegram 已确认，不会引导普通重传。
- journal parse、截断和权限错误均 fail-closed；fallback lookup 使用 lazy index，正常 direct-path lookup 不扫描全目录。
- 新增安全停止和立即中断两种 GUI 操作。安全停止不取消当前 Album；立即中断可能产生 UNKNOWN，并保留人工核对保护。

#### MediaItem lookup 与回归验证

- Image/Video planner 使用 snapshot identity lookup，并显式拒绝未知项、重复 identity 和 snapshot mismatch。
- UploadState 在一个 Album 内复用统一 `sent_at`；清理无内部调用的 legacy image `format_duration`。
- 新增独立 journal、reconciliation、stop semantics、media lookup、inflight GUI 测试，保持 unittest/offline 运行方式。
- Windows 路径断言采用稳定路径语义，并迁移对应 ownership regression，保持 Windows/macOS 兼容。

### V1.9.3-beta1 更新

#### TDLib 与混合上传

- 回退到 `tdjson 1.8.64.post1`，与当前 V2 GUI 兼容层和既有上传实现保持一致。
- 混合上传的视频内容统一复用视频模式的 payload builder，避免 Photo + Video Album 生成缺少 `InputFile` 的内容。
- 发送前校验 Photo、Video、`inputThumbnail`、cover 及嵌套 `InputFile`；缺少 `photo`/`video` 或错误结构会在 journal 写入前明确失败。
- 上传失败日志增加每个媒体的内容类型、InputFile 类型、路径存在性、文件大小、thumbnail 和 cover 诊断。

#### 回归验证

- 已验证空 `InputFile` 不会调用 TDLib，也不会写入 `PREPARED` journal。
- 已验证有效混合 Photo + Video payload 可以进入 `sendMessageAlbum`。
- 完整离线回归与跨平台 PyInstaller 构建通过后发布 `v1.9.3-beta1`。

### V1.9.1 更新

本次重新发布集中修复视频日期扫描和关闭日期后的上传稳定性：

#### 视频扫描

- ExifTool 批处理会按已返回的 `SourceFile` 更新 GUI 进度，显示当前完成数；停止扫描会立即终止外部进程并明确标记为“扫描已取消”。
- ExifTool 输出带 warning 时仍接受已经返回的有效文件，只对真正缺失的文件重试或二分隔离，避免一个异常 MP4 让整批文件重复处理。
- ExifTool 路径保存前执行短时 `-ver` 校验，并提示不要使用会等待按键的 `exiftool(-k).exe`。

#### 关闭日期读取

- 修复 `read_dates = false` 扫描完成、登录成功后进入上传准备阶段的 `None.strftime()` 崩溃。
- 未读取日期的视频继续保持 `capture_time = None`、文件名自然排序和固定分组，不会把 mtime 冒充为视频日期。
- 日期读取开启时仍按 EXIF → 可选媒体创建日期 → mtime 的既有优先级处理；关闭媒体创建日期时不会启动 FFmpeg 日期探测。

#### 回归验证

- 新增 623 个视频的 ExifTool 进度、warning/缺失文件隔离、取消扫描、无日期上传分组和 FFmpeg 零调用回归测试。
- 通过 Windows x64 与 macOS arm64 的离线回归、打包自检、包验证和制品构建流程。
