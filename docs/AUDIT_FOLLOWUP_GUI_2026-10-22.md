# GUI、配置与通用助手复审（2026-10-22）

基线：`3df4919`。本节在删除之前登记。只审查本仓库跟踪的源码、测试、文档、
构建 spec、workflow、tools 和 benchmarks；历史审计文档不作为运行入口。
逐符号检索同时核对 import/getattr/属性和字符串补丁、Qt connect/emit、模块导出及
spec 自动模块发现。未覆盖或静态工具告警不单独构成删除依据。

## 删除前证据

| 项目（基线位置） | 实际消费者与失效依据 | 现役路径与测试处理 |
| --- | --- | --- |
| `core/readiness.py:272–288` 的 `is_file_stable` | 全库只有定义、同文件 `__all__` 和 `LEGACY_CONSOLIDATION` 历史记录；没有调用、字符串获取或补丁入口。其正文只是 `check_file_readiness(..., probe=False).ready` | 媒体实际使用 `wait_for_file_ready` / `check_file_readiness`，保留完整实现及 `test_v2_filesystem_process.py`、文件系统与媒体预检回归；没有测试需要删除 |
| `core/concurrency.py:183–192` 的 `map_bounded` | 全库只有定义与同文件 `__all__`；没有 import/getattr/字符串入口，也没有测试。仅把 `ordered_bounded_map` 转成 list | 实际媒体并发调用 `ordered_bounded_map`，保留取消、缓冲上限、顺序及工作线程检查 |
| `gui/main_window.py:98–102` 的 `_ensure_config_file` 及唯一服务导入 | 私有包装只有定义和 `test_runtime_hardening:162` 调用；正式启动 `main:844` 已调用 `config_service.initialize_config`，后者调用服务 `ensure_config_file`；Qt 信号无该函数连接 | 保留创建/权限/错误提示逻辑，将“self-test 不创建配置”断言移到启动使用的 `config_service.ensure_config_file`，并保留 `test_gui_application` 对真实 `gui.application.main → run_self_test` 分派的断言及 loader 对缺失/损坏用户配置的隔离断言 |
| `config/loader.py:40` 的 `IMAGE_COMPRESSION_TARGET_BYTES` | 全库只有定义，没有动态获取或导出使用 | 图片实际目标来自 `media.image_probe.TELEGRAM_PHOTO_TARGET_BYTES` 并传给 Pillow 规范化，保留图片准备/大小断言 |
| `config/loader.py:513–519,616–622` 的两项 `*_SHOW_FILE_LIST` 与模板两项 `show_file_list` | 全库常量仅定义；模板键无生产读取者，旧控制台 `show_file_list` 已在前次审计删除；GUI 文件列表不读取这些键 | 保留 GUI 预览/任务文件列表及其现有测试；不改旧用户配置文件，残留未知键自然不再读取 |
| `config/loader.py:708–715` 的 `FFMPEG_COMPRESSION_TIMEOUT_SECONDS`、模板键、`AdvancedPanel` 的“FFmpeg 压缩超时”控件 | 常量唯一消费者是 AdvancedPanel 的 load，键唯一写入者是其 collect_values；图片实际经 Pillow `image_prepare`，不运行 FFmpeg 压缩，视频仅封装/探测/封面，均不读取该值；旧拼写也无消费者 | 删除无作用的控件、信号、load/collect 与校验；`test_final_audit_regressions` 将原空键存在断言迁为当前 ExifTool/FFmpeg 日期/封面超时的保存值断言，同时确认两种失效键不再输出；保留其他所有断言 |
| `gui/main_window.py:526–529` 的 `_stop_upload` | 只有定义，无调用/getattr/patch 或 Qt 连接。正文仅转发 `_safe_stop_upload` | 正式 TaskPage 的 safe/immediate 信号在 main_window:209–210 分别连接实际处理函数；保留停止语义与页面按钮测试 |
| `gui/pages/task.py:28,151–153` 的旧 `stop_requested` 信号和双发射包装 | 此信号在 TaskPage 中仅定义与 emit，零 connect/getattr/测试订阅；GuiConsoleUI 同名属性是另一种对象的现役取消状态，不删除 | 安全停止按钮直接连接 `safe_stop_requested`，立即中断连接保持；`test_gui_pages.test_task_page_keeps_progress_and_stop_contracts` 继续检查两种真实信号和按钮 |
| `gui/components/cards.py:55–64` 的 `set_subtitle` / `set_description` | 前者唯一调用者是后者，后者只有定义；无 Qt 信号/动态属性或补丁入口。StatCard 实际只有构造 subtitle 和 `set_value` 消费者 | 保留构造字幕、主页及历史页面的指标更新；GUI 页面和导航测试不删除 |
| `MainWindow` 七处 `_cache_clear_worker` 的 `getattr(..., None)` 与 `SettingsPage.save_changes` 的 `_conflicts` 默认值 | 两个属性都在各自构造函数中无条件初始化，前者在 `_build_ui` / `_refresh_pages` 前初始化，后者在首个 refresh 和用户保存前初始化；全库未找到删除属性的代码。两个绕过构造的 SimpleNamespace fixture 需要补上相同初始值 | 直接读取实际属性，保留全部 `None`、运行中、取消/等待及冲突判断；仅给 fixture 补 `_cache_clear_worker=None` / `_conflicts={}`，不改其任何行为断言 |

spec 通过 `collect_submodules` 发现模块，未注册上述函数/方法/常量；删除孤儿成员不删除其现役模块。
不移除实际 I/O 边界、取消、持久断点、UNKNOWN 发送保护、登录或现存用户配置兼容读取。

## 验证

- 最终局部回归：109 项全部通过，日志 `/tmp/tdlib-followup-gui-tests.log`。
  范围为 runtime_hardening、final_audit_regressions、gui_pages、gui_application、
  settings_data_safety、v2_filesystem_process、regression_filesystem_safety_1、
  regression_gui_configuration、one_time_reset 与 BackgroundMaintenanceTests。
- GUI 全部源码与本次修改的配置/并发/就绪助手直接编译检查通过；`git diff --check` 通过。
- 两个本轮 GUI 测试文件的 AST 方法集合与基线一致：final_audit_regressions 7 项、
  settings_data_safety 9 项。启动隔离测试原方法保留，仅改为实际服务入口。
- 没有删除测试方法或弱化停止、持久化与冲突断言；fixture 补上正式构造器已有属性。
- 本代理未运行全量套件、不新增门禁；全量/覆盖与其他模块修改由主代理统一验证。
