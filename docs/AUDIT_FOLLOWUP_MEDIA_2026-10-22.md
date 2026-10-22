# 媒体、文件系统和上传补充审计（2026-10-22）

基线：`3df4919`。已参考此前 `AUDIT_CLEANUP_2026-10-18.md`、
`AUDIT_MEDIA_2026-10-22.md`、`AUDIT_CORE_2026-10-22.md`，以下证据先于删除登记。
没有修改同步的 `sources/`，不进行真实 Telegram 登录或发送。

## 删除前证据

检索包含隐藏文件，排除 `.git`、本地虚拟环境和审计输出、用户 `data`；范围覆盖
`src`、`tests`、文档、README、resources、tools、benchmarks、workflows 和 PyInstaller spec。
同时检查包导出、动态 `import_module` / `getattr` 入口；不将测试未覆盖单独作为删除依据。

| 对象（基线位置） | 失效证据 | 保留行为和相关验证 |
| --- | --- | --- |
| `core/filesystem.py:310` 的 `retry_with_backoff` 和 `__all__` 转出 | 全库名称命中只有此定义和该文件第 642 行导出；没有调用或字符串反射入口。`_T` / `TypeVar` 也只为这个函数服务。 | 实际目录读取调用 `retry_fs_operation`，保留有限重试、取消、永久错误处理；相关覆盖在 `test_v2_filesystem_process`、`test_regression_filesystem_safety_1`。 |
| `upload/engine.py:487` 的 `stage_plan` 候补方法名 | 全库唯一命中就是这个 `getattr`；没有任何 `stage_plan` 实现、调用、测试 fake 或文档接口。`Stager` 协议及全部注入 stager 都提供 `stage`。 | 保留 `stage(plan, context=...)` 调用和暂存后 Album 边界校验；`test_prefetch_pipeline` 验证正常、预取与取消清理。 |
| `upload/preflight.py:55` 的 `PreflightResult.reason_for` | 全库唯一命中为方法定义；动态入口、导出及测试都没有按名称使用。 | `reasons` 数据完整保留，实际上传引擎第 741 行直接遍历它形成诊断；保留预检分区、取消和错误结果测试。 |
| `core/filesystem.py:496` 的 `FileScanResult` 别名 | 全库名称仅赋值和该文件 `__all__`；生产和测试均使用实际 `DirectoryScanResult`。 | 保留实际类及现有两值解包兼容，不扩大此次删除范围。 |
| `media/image_prepare.py:326` 的编码后超限检查 | `encode_jpeg_under_limit` 的三个返回分别受 `len(data) <= target_bytes` 条件保护，或返回只在该条件下赋值的 `best_data`；无法达标必抛异常。全库测试的编码 patch 都调用真实编码后触发取消/源变化，没有超限返回，也无动态编码替换入口。 | 删除永远不能触发的二次检查；真正编码限额、缩放和无法达标抛错仍由原帮助函数及相关媒体测试验证。 |
| `media/video_pipeline.py:24–28` 的 imageio namespace 猴补 | 固定依赖 `imageio-ffmpeg==0.6.0` 的实际 `__init__.py` 无条件从 `._utils` 导出 `get_ffmpeg_exe`；没有 namespace 注入、配置或测试 fake。现有测试只 patch 真实 API，发布 spec 收集固定包。 | 保留正常依赖 API、便携 FFmpeg 查找、环境变量和 Windows 隐藏窗口适配；去掉仅为不受支持的缺失 API 假设补入的 lambda。 |
| `media/image_prepare.py:161–176` 的 `extreme_aspect_policy` / `transparency_background` 参数别名 | 实际生产调用 `image_pipeline.compress_image` 只传 `aspect_policy` / `background`；两个别名参数仅 3 项媒体测试使用，无文档 Python API 或动态调用。default_config 的同名 TOML 键是现役配置，loader 和生产转换链继续使用它们。 | 删除双参数覆盖，3 项测试只改成 canonical 参数名；透明图合成、padding 几何和 skip 错误原断言全部保留，不移除测试。 |
| `media/image_probe.py:123–132` 的转码格式分类分支 | BMP/TIFF/WEBP 返回 true，PNG/JPEG 返回 false，其余返回 true；严格等价于 `fmt not in {"JPEG", "PNG"}`，不改变任何输入的分类。 | 保留原生 JPEG/PNG、WebP/BMP/TIFF 转码、alpha、动画、EXIF 和尺寸限制覆盖。 |

未删除任何测试方法。媒体源快照、确认前后断点、UNKNOWN 防重复、取消、缓存有效性、
暂存目录所有权及链接检查仍有实际使用，继续保留。策略注入参数仍为 GUI 和测试所用，
不因名称带有 legacy 而整体删除。

## 验证

- 文件系统、上传引擎及预取相关测试：63 项通过；日志 `/tmp/audit-followup-media-tests.log`。
- 图片/视频探测准备、日期 metadata、图片策略和媒体审计回归：74 项通过，无跳过；
  日志 `/tmp/audit-followup-media-image-tests.log`。
- 编码中源变化、快照和预检池边界：`test_review_regressions.PreparationBoundaryTests`
  5 项通过；日志 `/tmp/audit-followup-media-boundary-tests.log`。
- 7 个已改 Python 文件 AST 解析通过，`git diff --check` 通过；改前/改后测试方法清单
  完全相同，`test_media_probe_and_prepare` 的 38 项原测试方法全部保留。
- 源码、测试、资源、工具、benchmark、workflow 和 spec 再检索上述已删符号/参数用法，
  无遗留导入或调用；文档中的历史记录继续保留。

本子任务生产源码净减少 76 行，未删除任何测试方法或业务断言。没有发现并复现新的产品 Bug，
没有新增防御层或检查门禁。全量测试、打包与最终合并由主审计统一验证；此处不重复运行。
