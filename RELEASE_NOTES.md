### V1.9.3-beta5 更新

#### Settings 全内联化架构与配置体验革新

- **彻底内联化 6 大设置面板**：彻底移除“打开配置编辑器”、“打开扫描工具”等二级弹窗，左侧子导航优化为 6 项核心分类：`常规`、`Telegram`、`上传参数`、`高级选项`、`存储与缓存`、`环境与许可`；点击左侧直接在右侧内联展示表单与信息卡片。
- **就地修改与平滑持久化机制**：进入面板展示真实配置值，修改任意输入项自动触发 dirty 脏状态检测；底部常驻操作区 `[恢复]` 与 `[保存更改]`，未修改时禁用、修改后高亮激活；点击恢复重置未保存输入，点击保存就地校验并无损写回 `config.toml`，右下角提示“✓ 设置已保存”，无冗余弹窗阻断。
- **模块化 Settings 架构解耦**：创建专职 `gui/settings/` 子包（`base.py`, `general_panel.py`, `telegram_panel.py`, `upload_panel.py`, `advanced_panel.py`, `storage_panel.py`, `environment_license_panel.py`），消除单一大文件。
- **敏感凭据安全掩码与可视化切换**：Telegram API Hash、代理认证密码与 MTProto Secret 默认密码掩码隐藏；右侧内嵌专属眼睛按钮（`eye` / `eye-off` 矢量图标），一键切换明文/密文显示。
- **上传参数分段控制与可复用目标编辑器**：顶部提供 Segmented Switcher（`[ 视频 ]` `[ 图片 ]` `[ 混合 ]`）无缝切换视频、图片与混合上传分组参数；抽象提取通用组件 `TelegramTargetEditor`，供 `UploadPanel` 与 `TargetDialog` 共同复用，严格保持校验与频道/群组动态显隐逻辑一致。
- **高级选项卡片折叠与 NoButtons 无箭头微调框**：外部工具路径与 I/O 并发常驻展示；文件稳定性、外部进程超时、重试与批处理大小收纳为可折叠卡片，防界面过载；所有 SpinBox 统一采用现代无箭头设计。
- **环境诊断与开源许可融合**：合并原“环境诊断”与“关于与许可”为单一“环境与许可”面板；上半部网格化呈现 6 项核心依赖检测状态药丸，下半部集成软件信息、GPL-3.0-only 协议声明、可点击的 GitHub 项目主页链接与 `[在 GitHub 中打开]` 快捷按钮。

#### Upload 交互精简与标题编辑去重

- **操作按钮文案优化**：顶部操作栏按钮移除多余省略号，精简为 `选择目录` 与 `修改目标`。
- **轻量目标编辑弹窗（TargetDialog）**：Upload 页面的“修改目标”只编辑目标 Chat ID、频道与话题 ID，不再堆叠排序、相册大小与文件名格式等全局设置，职责纯粹。
- **Caption 编辑去重与体验提升**：移除预览工具栏冗余的“编辑媒体组标题…”按钮，添加“双击媒体组可编辑标题”引导提示；双击或右键媒体组根节点直接调起专业 `CaptionEditDialog`；彻底删除 `upload.py` 中重复手写的内联简陋对话框逻辑。
- **Sidebar 品牌区精简**：移除侧边栏顶部品牌区重复的 Beta 版本 badge，左上角回归纯粹的 App 标题与主题切换按钮；版本号收敛至侧边栏底部（`Version 1.9.3-beta5`）。
- **SVG 矢量图标库扩展**：新增 `github`, `sliders`, `eye`, `eye-off`, `external_link` 等纯代码矢量图标。
- **全量自动化测试扩充**：新增 `tests/test_beta5_settings_and_ux.py`，全套测试集扩展至 316 项并通过 100% 验证。

### V1.9.3-beta4 更新

#### GUI 第二轮深度重构与全面视觉现代化

- **扁平化 6 路由侧边栏与媒体上传枢纽（UploadHubPage）**：将视频、图片、混合上传整合至统一的“媒体上传”工作台，通过顶部现代分段控制器（Segmented Control）流畅切换；侧边栏收敛为“概览、媒体上传、任务中心、未确认上传、历史记录、设置与诊断”6 个清晰的核心路由。
- **原生 SVG 矢量图标系统与 High-DPI / Retina 多倍率渲染**：全面移除传统 emoji 图标，引入轻量纯代码 SVG 矢量图标渲染引擎；支持根据屏幕 `devicePixelRatio` 动态生成 1x/2x/3x 高清位图，选中状态与激活态自适应切换纯白（`#ffffff`）高亮。
- **二级设置导航与模块化分类**：设置中心全面重构为双级导航架构（常规、Telegram、上传参数、扫描与工具、存储与缓存、环境诊断、关于与许可），消除臃肿单一视窗；存储与缓存面板支持全量缓存与封面缓存细粒度维护。
- **全页面主题热刷新广播（refresh_theme）**：统一广播至主导航侧栏、首页看板、上传枢纽、视频/图片/混合上传页、任务中心、未确认上传对账中心、历史中心与设置页面，所有语义前景文本与自适应辅助按钮图标联动热重载。
- **浅色模式高对比度选区体验**：修复浅色模式下选中文本颜色反白导致内容难以阅读的问题；提供高对比度 `text_selection` 设计令牌（`#0369a1`），确保多行输入框、下拉菜单、表格行及预览树高亮选中状态清晰可辨。
- **对话框现代去工具化与 860x560 紧凑可用性**：重构 `TargetDialog` 消除双层嵌套 GroupBox，全量数值微调框统一采用现代无箭头设计（NoButtons）；UploadHubPage 引入自适应滚动容器（QScrollArea）并优化树形列表最小高度（150px），确保 860x560 最小窗口下扫描与上传按钮绝对可见且不被遮挡。
- **首页任务卡片生命周期无缝联动**：重构 HomePage 任务看板，完整实现“空闲 ➔ 运行中（展示待传文件与 Album 摘要）➔ 实时进度更新 ➔ 空闲”全生命周期状态流转，移除冗余死代码并消除粗糙“无”字展示。
- **全局硬编码样式清除**：全面消除所有组件及对话框中的硬编码行内 `setStyleSheet`，统一绑定至 `theme.py` 主题样式表与动态属性，保障主题实时热切换（☀️/🌙）顺滑无色块残留。
- **超大列表挂载性能优化**：在媒体组扫描结果树加载过程中引入批量渲染更新挂起机制（`setUpdatesEnabled`），轻松应对 1,000–5,000 条大规模媒体列表挂载，界面零卡顿。

#### 关键架构与稳定性全面加固

- **扫描线程退出安全（P0）**：在退出主界面时对运行中的多媒体扫描线程执行协作式优雅中断（`request_stop()`），引入带超时的非阻塞保护，避免退出时后台悬挂。
- **异步化设置中心（P1）**：缓存占用体积统计异步转移至 `CacheStatsWorker` 后台工作线程运算，进入设置页无任何界面卡顿。
- **动态配置读取保障（P1）**：引入统一配置访问代理 `get_config()` 与 `get_cfg()`，解耦模块导入时的静态绑定，动态适配热重载后的运行时配置。
- **Telegram 连接状态完全解耦（P1）**：将底层长连接就绪指示器从上传任务完成态解耦（引入 `_telegram_connected` 保持有效连接状态），任务结束不再错误将 TG 状态标记为未连接。
- **纯净单向架构（P2）**：彻底清除页面与对话框对主窗口的运行时反向探测依赖（`sys.modules.get("...main_window")`），增强代码可维护性与测试隔离度。
- **全量回归保障**：新增 `tests/test_beta4_stage2_redesign.py` 自动化测试集，总测试用例扩充至 302 项并通过 100% 验证。

### V1.9.3-beta3 更新

#### GUI 架构全面重构与现代化设计

- **深色与浅色双模式热切换**：新增月亮 🌙 与太阳 ☀️ 模式切换按钮，内嵌于导航侧栏顶部品牌栏；提供专业浅色调色盘 `LIGHT_PALETTE` 与动态代理，支持界面整树 QSS 实时平滑热切换。
- **专职服务层解耦与反向依赖根除**：将 `main_window.py` 业务逻辑拆解为 5 个独立专职服务模块（`tools`, `config_service`, `history_service`, `cache_service`, `scanner`），彻底消除页面与对话框对主窗口的反向循环导入，主窗口代码量由 2019 行精简至 826 行。
- **架构解耦与模块化**：建立清晰的组件化体系：`gui.theme`（设计令牌与主题引擎）、`gui.components`（状态徽章、指标卡片、操作卡片、侧边栏）、`gui.dialogs`（标题编辑、目标配置、基础配置、扫描工具）与 `gui.pages`（概览、上传、未确认、任务、历史、设置）。
- **现代视觉体验**：构建基于专业设计规范的深色/浅色专业界面，包含统一排版规范、层次分明的背景体系、语义化状态色彩与呼吸徽章、圆角卡片与阴影效果。
- **全新侧边栏体验**：引入带有品牌标识、版本标牌、状态常驻底栏与模块快捷导航的 `NavigationSidebar`，实时联动 Telegram 连接状态。
- **任务中心与实时监控**：双进度条架构清晰区分总体进度与 Album 批次进度，实时呈现平滑计算的上传速率与预估剩余时间（ETA），带彩色高亮终端日志输出。
- **代码整洁与生命周期安全**：彻底删除已废弃的 `_LegacyUploadPage` 与重复内嵌对话框实现；并在主窗口关闭事件中安全等待并取消活跃扫描工作线程，消除崩溃隐患。

#### 稳定性与兼容性保障

- 保持 100% 向后兼容性：用户已有 `config.toml`、历史记录 `history.json`、断点状态、`InflightJournal` 与各级缓存目录无损读写。
- PyInstaller 打包规格（`tdlib_media_uploader.spec`）同步更新所有隐式模块引入，保障跨平台分发构建可靠性。
- 完整回归验证：保留所有既有 254 项测试并通过，新增全面的 GUI 模块化架构测试与设计系统测试。

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
