# 测试清理补充审计（2026-10-22）

基线：`3df4919`。以下证据先于测试合并登记。只合并重复输入/断言，不用删除测试处理产品失败。

## T1：合并同一视频大小函数的重复边界测试

拟移除的方法：
`test_runtime_hardening.V190HardeningTest.test_telegram_video_boundaries_are_exact_and_shared`。

保留并扩充的方法：
`test_v2_telegram.TelegramLimitsTest.test_video_boundaries_are_exact`。

两文件的 `app_config` / `config` 都直接导入
`tdlib_media_uploader.config.loader`；调用的是同一 `video_size_status`，不是不同生产入口或兼容 wrapper。
它们的重复断言如下，合并后每项仍执行一次：

| 输入/常量 | 两者相同的期望 |
| --- | --- |
| `VIDEO_STANDARD_MAX_BYTES` | `4000 * 524_288` |
| `VIDEO_PREMIUM_MAX_BYTES` | `8000 * 524_288` |
| `video_size_status(standard)` | `allowed` |
| `video_size_status(standard + 1)` | `requires_premium` |
| `video_size_status(standard + 1, is_premium=True)` | `allowed` |
| `video_size_status(premium, is_premium=True)` | `allowed` |

双方的独有断言全部迁入保留方法：

| 原方法 | 独有输入/常量 | 期望 |
| --- | --- | --- |
| runtime_hardening | `TELEGRAM_UPLOAD_PART_SIZE_MAX` | `524_288` |
| runtime_hardening | `video_size_status(standard - 1)` | `allowed` |
| runtime_hardening | `video_size_status(premium - 1, is_premium=True)` | `allowed` |
| runtime_hardening | `video_size_status(premium + 1, is_premium=True)` | `oversize` |
| v2_telegram | `IMAGE_MAX_BYTES` | `10 * 1024**2` |
| v2_telegram | `video_size_status(premium)` | `requires_premium` |
| v2_telegram | `video_size_status(premium + 1)` | `oversize` |

动态补证：基线全量测试的 `test_function` 分支覆盖上下文中，两方法各执行相同的
6 个生产源码行、9 条跳转，无任一方法独有的源码行或跳转。数据为本机
`/tmp/tdlib-cleanup-audit.AsJPlq/baseline.coverage`，以完整 test ID 筛选，只统计 `src/tdlib_media_uploader/`。
覆盖相同只作补证；删除依据是上表逐项核对同一函数、输入和断言，并保留七项独有断言。

三种媒体扫描器及发送前大小检查的集成测试均保留：函数边界测试不能替代不同生产入口。

## 保留结论

- 基线 575 个测试方法去除方法名和 docstring 后，没有完全相同的 AST 正文。
- 不按 phase/V2/legacy 文件名删除测试。未发现已删除设置弹窗的残留测试入口。
- `benchmarks/bench_mixed_resolve.py` 是上轮合并后唯一的混合条目性能脚本，支持指定样本量，
  并断言所有结果及顺序；保留。它与发送/扫描的行为测试目的不同。
- 源码结构/工作流字符串断言虽部分较脆弱，但单凭这种风格不能证明它们已失效；本项未删除。

## 合并后验证

`QT_QPA_PLATFORM=offscreen PYTHONPATH=src:tests .venv/bin/python -m unittest
test_v2_telegram test_runtime_hardening -v`：22 项全部通过。
本次合并使方法总数减少 1；19 条原断言合为 13 条不同输入/常量断言，未移除独有断言。

## T2：删除拆分测试后遗留的闲置导入（删除前登记）

以下绑定均无 AST `Load`。逐文件核查所有字符串补丁目标：补丁使用生产模块的完整路径，
不读取这些测试模块的导入别名；8 个文件均无 `globals/locals/eval/exec/getattr/setattr`
读取测试全局的路径，也没有其他文件导入这些测试绑定。标准库和 Qt 类型导入不承担注册，
媒体模块只定义类型/函数/常量；其仍在使用的类型/函数导入继续保留。
配置、日志和缓存模块的导入没有测试注册动作；7 个 regression 文件都保留 `main_window`
导入，它自身仍导入 config_service/tools/cache_service 等，原配置初始化时序没有移除。
remux 文件只移除未使用的值/函数导入，不依赖 cache_service 的全局初始化结果。

| 文件（tests/） | 拟移除绑定 |
| --- | --- |
| test_regression_filesystem_safety_1.py | json, random, SimpleNamespace, app_logging, config_service, tools, cache_service, QGroupBox, QHBoxLayout, QScrollArea |
| test_regression_filesystem_safety_2.py | json, random, subprocess, threading, time, SimpleNamespace, config_service, tools, path_utils, QGroupBox, QHBoxLayout, QScrollArea |
| test_regression_gui_configuration.py | json, random, threading, time, SimpleNamespace, app_logging, cache_service |
| test_regression_media_metadata.py | random, metadata, app_logging, config_service, cache_service, QGroupBox, QHBoxLayout, QScrollArea |
| test_regression_media_planning.py | random, subprocess, threading, time, SimpleNamespace, app_logging, config_service, tools, cache_service, path_utils, QGroupBox, QHBoxLayout, QScrollArea |
| test_regression_media_sorting.py | json, subprocess, threading, time, SimpleNamespace, metadata, app_logging, config_service, tools, cache_service, QGroupBox, QHBoxLayout, QScrollArea |
| test_regression_telegram_delivery.py | json, random, subprocess, metadata, app_logging, config_service, tools, cache_service, path_utils, QGroupBox, QHBoxLayout, QScrollArea |
| test_video_remux_pipeline.py | THUMBNAIL_CACHE_DIR, VIDEO_PROCESSED_CACHE_DIR, UploadBatchResult, CACHE_TARGETS, clear_cache, PreparedVideo, cleanup_stale_processed_videos, get_video_process_cache_key, format_unsupported_reason |

例外保留：sorting 和 telegram_delivery 的 `gui` 虽无 AST Load，
但 `main_window` 顶层调用 `_prepare_qt_plugins()`，这些测试仍创建 QApplication。
本次不移除这种现存 Qt 环境初始化副作用。所有 QApplication 全局 fixture、测试方法及断言保留。
嵌入子进程脚本中的 `import time`、独立 `__import__('threading')` 等与删除的本模块绑定无关，亦保留。

最终删除 84 个闲置导入绑定。8 文件除顶层 import 以外的 AST 与 HEAD 完全一致，
保留全部 123 个测试方法。将这 8 个模块一起运行：123 项全部通过，日志
`/tmp/tdlib-cleanup-test-imports.log`；`git diff --check` 通过。
