# 遗留代码与测试审计（2026-10-18）

基线：`Maxwell233/tdlib-media-uploader` 的 `main`，提交 `943c860f4e4bb98eb3bad0d248a11a1fa58c1799`。
工作分支：`audit/legacy-code-test-cleanup`。在独立副本进行，未修改同步的 `sources/` 或既有工作目录。

## 删除前证据登记

本节先于删除写入。引用行号对应上述基线，可以用 `git show 943c860:<path>` 复核。
静态分析只用于提出候选；以下结论同时核查生产入口、动态模块加载、构建脚本、测试与运行覆盖。

### E1：旧独立上传流程及其私有调用闭包

`README.md:219` 明确说明应用使用 GUI 打包入口，内部模块没有独立启动入口。
实际入口为 `app.py → gui/application.py → gui/main_window.py → gui/integration.py → UploadEngine`。
`gui/integration.py:47-49`、三种媒体策略的 `_load_legacy_module` 与 `tdlib_media_uploader.spec:150-152`
仍加载三个 `legacy_*` 模块，所以保留模块及策略实际依赖的扫描、规划、内容准备、断点与缓存功能。

| 文件（均在 `src/tdlib_media_uploader/media/`） | 拟删除符号（基线行号） | 失效证据 |
| --- | --- | --- |
| `legacy_image.py` | `_main_impl` 865–1060 | 全库仅定义，没有调用、模块 `__main__` 调用或字符串反射入口 |
| `legacy_image.py` | `ImageUploadProgress` 615–742；`show_file_list` 745–772；`show_upload_summary` 775–803；`validate_config` 805–812 | 只由上述孤儿流程引用 |
| `legacy_image.py` | `_compressed_path` 274–279 | 私有函数，全库仅定义，实际图片规范化走 `image_prepare.py` |
| `legacy_video.py` | `_main_impl` 2102–2363 | 全库仅定义，没有调用、模块 `__main__` 调用或字符串反射入口 |
| `legacy_video.py` | `VideoUploadProgress` 1849–1975；`print_plan` 2055–2089；`validate_config` 2092–2099 | 只由上述孤儿流程引用 |
| `legacy_video.py` | `_probe_video_duration` 1264–1307 | 私有函数，全库仅定义，实际探测走 `video_probe.py` |
| `legacy_mixed.py` | `_main_impl` 830–1007 | 全库仅定义，没有调用、模块 `__main__` 调用或字符串反射入口 |
| `legacy_mixed.py` | `MixedUploadProgress` 681–817；`_validate_config` 820–827 | 只由上述孤儿流程引用 |

复核方法：对表内符号逐个执行 `git grep -n -w <symbol> 943c860 -- .`，并检查命中的所属函数；
再查 `import_module`、`getattr`、`__main__`、spec 的 hidden imports，确认没有动态启动路径。
基线完整 481 项测试的分支覆盖记录显示：表内函数及 Progress 方法的正文执行行均为 0。
这只是对静态不可达结论的补充，不将“未覆盖”单独视为删除依据。

生产替代链的覆盖继续保留：`test_phase5_{image,video,mixed}_strategy.py`、`test_gui_integration.py`、
`test_stop_semantics.py`、`test_prefetch_pipeline.py`、`test_media_probe_and_prepare.py`、`test_video_remux_pipeline.py`。
`_exiftool_rows` 明确作为外部集成兼容 wrapper 保留。

### E2：被覆盖的 GUI 方法

`gui/pages/upload.py` 的 `UploadPage._scan_button_clicked` 在基线 408–413 和 781–786 两次定义，正文相同。
Python 类构造时后者替代前者，实例初始化时的 Qt `connect(self._scan_button_clicked)` 取到后者。
仅删除前一份定义；后者、扫描/取消信号与现有 GUI 行为测试继续保留。

### 测试清理准则

已扫描基线 481 个测试方法；去掉方法名、位置及 docstring 后，没有完全相同的 AST 正文。
保留名称包含 phase/V2/legacy 的测试，除非逐个证明断言与入口均被其他保留用例覆盖。
工具函数测试不能取代不同生产入口的集成测试，也不会删除任何失败测试来修复产品问题。

### E3：合并重复测试 fixture，保留所有输入、断言和入口

| 原测试 ID（基线） | 保留测试 ID | 删除前证据与合并方式 |
| --- | --- | --- |
| `test_inflight_gui.InflightGuiTest.test_gui_inflight_page_loads_unresolved_records`（138–148） | `test_inflight_gui.InflightGuiTest.test_gui_legacy_target_warning_is_preserved` | 两者均创建临时 `upload_inflight`，对同一 `image` 类型和 `[{"path": "photo.jpg"}]` 调用 `prepare`、`unknown(..., "timeout")`，patch 同两处 `APP_DATA_DIR` 后构造 `InflightPage`。只有内部 Album ID 的无行为意义标签不同。原测试唯一断言 `page.table.rowCount() == 1` 原样移入保留测试；后者的目标未知提示、人工确认警告断言全部保留。 |
| `test_regression_media_sorting.ImprovementsTest.test_relative_path_sort_compares_directories_before_filenames`（101–114） | `test_v2_filesystem_process.SortingExtractionTest.test_relative_path_sort_compares_directories_before_filenames` | 两者 `root = Path("root")`、五条路径及输入顺序完全相同：`B2/x.41.jpg`、`A/x.1.jpg`、`B2/x.410.jpg`、`A/x.100.jpg`、`B10/x.2.jpg`；期望序列亦相同：`A/x.1.jpg`、`A/x.100.jpg`、`B2/x.41.jpg`、`B2/x.410.jpg`、`B10/x.2.jpg`。区别仅为直接 `sorting` 入口与 `filesystem_legacy` 兼容入口；保留方法通过 `subTest` 分别运行这两个入口，使用各自 `media_path_sort` 和 `relative_name`，原输入与断言不变。 |

合并前两个 inflight 用例均通过；临时逐行/跳转跟踪显示，弱用例执行的 329 个生产代码行和 322 个行间跳转，
均为保留警告用例的 365 个生产代码行和 361 个行间跳转的子集，无独有执行行或跳转。
这仅补充相同 fixture 和断言迁移的证据，不以覆盖率相似替代行为比对。
其余近似测试涉及不同媒体策略、错误分支或输入类型，继续保留；`phase2_layout` 的打包/资源检查
与 `architecture_contract` 的导入依赖边界检查无重复，`FileSnapshot` 的 tuple 测试覆盖自定义协议，亦保留。

## 基线验证

- Python 3.13.16；Pillow 12.3.0；PySide6 6.11.2；imageio-ffmpeg 0.6.0。
- `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m unittest discover -s tests -v`：481 项，成功，跳过 2 项。
- 同一套测试另运行分支覆盖基线；工具安装在本地 `audit/tooling/`，没有增加项目依赖。

### E4：历史手工诊断与重复基准脚本（删除前登记）

- `bench.py` 和根目录 `test_perf.py` 都是 receiver 异常循环的手工计时，无 TestCase、无 assert，
  CI 的 `unittest discover -s tests` 不会收集。注释讨论的 `time.sleep(1)` 已由生产代码
  `tdlib_common.py:786` 的 `stop_event.wait(1.0)` 替代；两个脚本都直接创建 native client、
  人工设置内部 stop event、无有界 join，没有覆盖应用实际 close/cancel 流程。
  全库 README、docs、workflow、spec 和 tests 没有脚本入口引用；删除不减少任何自动化用例。
  实际延迟关闭、关闭超时与 receiver 等待 closed update 的覆盖继续留在
  `test_review_regressions.DeliveryTests`，取消路径留在 `test_stop_semantics.py`。
- `benchmarks/bench_mixed_resolve.py` 与 `bench_mixed_resolve2.py` 的逐行 diff 只有
  `num_items = 5000` / `10000` 一处不同，当前均可运行，不能说它们已坏。
  保留一个可传样本量的脚本，仍运行两组规模；删除第二份复制文件。
  同时用真实 AlbumPlan、验证所有 outcome、移除 `except Exception: pass`，使错误能够失败退出，
  避免把失败提前退出误报为性能提升。不删除这项仍有用的性能诊断能力。

### B2：重复消息 ID 导致错误写入上传完成断点

基线 `_normalize_send_result({'status': 'CONFIRMED', 'succeeded': [10, 10]}, 2)`
和 `map_send_result([{'id': 10}, {'id': 10}], expected_count=2)` 都返回 CONFIRMED。
它们只检查列表长度，无法证明两个 Album 项各自收到不同的 Telegram 消息确认。
新回归 `UploadConsistencyTest.test_duplicate_confirmation_ids_keep_guard_and_do_not_checkpoint`
通过真实 GUI 上传适配器和临时图片/状态文件复现：旧代码错误返回 COMPLETED。
纯映射回归还复现立即重复 ID 和异步更新后 ID 冲突两种情况。修复前失败日志保存在本地
`audit/delivery-red.log`，未删测试。
修复在共享 TDLib 映射器和通用发送器归一化边界检查成功 ID 唯一性，降为 UNKNOWN，
保留已观察到的 ID 和日志，避免错误写断点或自动重试。

### E5：已完成拆分后未再使用的导入（删除前登记）

以下 10 个绑定经逐个全库 `rg -n`、AST Load、`__all__`、动态属性/Qt 信号和构建配置核验：
在源码、测试、README、开发文档、workflow、spec 和 benchmark 中均只有其导入处，没有使用点。
不使用自动 fix 批量移除主窗口导入，因为该模块仍暴露测试及旧适配层实际使用的兼容属性。

- `core/sorting.py`：`functools.cmp_to_key`；排序已使用 tuple key，保留两入口全部排序测试。
- `gui/main_window.py`：`QPalette`；原主题逻辑已迁移至 theme 模块。
- 同文件三个旧路径别名：`RUNTIME_HISTORY_PATH`、`RUNTIME_TDLIB_DATABASE_DIR`、`RUNTIME_TDLIB_FILES_DIR`。
  保留实际使用的 history/cache/config 服务和原定义模块。
- 同文件五个旧预览转换别名：`_v2_caption_payload`、`_v2_group_key`、`_v2_item_dict`、
  `_v2_item_identity`、`_v2_plan_dict`。实际 GUI 预览走 models/scanner，保留原函数及其测试。

## Bug B1：合法 TOML 表头导致保存失败或越界更新

修复前最小复现：`update_toml_value('[paths] # directories\nvideo_dir = "old"\n', 'paths', 'video_dir', 'new')`
追加第二个 `[paths]`，随后 `tomllib.loads` 和 GUI 保存均报 `Cannot declare ('paths',) twice`。
缩进表头 `  [paths]` 同样是合法 TOML，也会失败。原因是 `get_section_re` 只接受列 0 且无行尾注释的表头。

结束边界只识别列 0 的 `[`，还会把后续缩进表或数组表当作当前段正文：
对 `[scan]\n  [proxy]\nenabled = true\n` 新增 `scan.enabled=false` 时，会错误改掉 `proxy.enabled`。
先新增 `test_config_editing_boundaries.py`，在未修产品代码时运行并保存失败输出至 `audit/config-red.log`。
回归覆盖表头注释、缩进表头、后续同名键隔离、数组表边界，以及 CRLF 编辑/持久化不扩大改写。

修复将表头与键的空白匹配限定为横向空白，保留 CRLF，支持表头行尾注释，并在普通表/数组表前终止当前正文。
文件编辑与 reload 错误回滚都保留原始换行字节；空末段无结尾换行时也能正确插入新键。
`audit/config-red.log`：未修产品时的 6 项回归全部失败（包含子用例，计 6 failures、2 errors）。
修复后最终专用回归 8 项；连同配置、设置、页面、导航、工作线程套件共 59 项通过，见 `audit/config-related.log`。

## 删除证据补充 E2：重复主题导入

`gui/components/cards.py` 基线第 9、70 行两次从相同模块导入 `THEME`；中间没有更改该绑定。
保留顶部导入，删除后一次重复导入。`ActionCard` 的实际主题和 SVG 图标逻辑继续保留。


### E1 补充：旧流程的剩余私有闭包（删除前登记）

以下命中均针对基线 `943c860` 的完整 `src/`、`tests/`、构建脚本、文档和基准目录，
通过逐符号 `rg -n -w` 核查；同名的其他模块函数按定义模块区分。AST 核查进一步确认
每条内部调用所属的顶层函数；移除三项 `_main_impl` 后，下表没有剩余生产调用。

| 定义文件 | 符号与基线命中行 | 结论 |
| --- | --- | --- |
| `media/legacy_image.py` | `report_skipped_images`：定义 436，调用 919、935、1055 | 三次调用均位于孤儿 `_main_impl` |
| `media/legacy_image.py` | `report_scan_size_skips`：定义 460，调用 886 | 调用仅位于孤儿 `_main_impl` |
| `media/legacy_image.py` | `_process_timeout`：仅定义 114 | 旧 FFmpeg 图片处理助手；当前 `compress_image` 282–293 调用 `image_prepare.prepare_image_for_telegram`，不调用本助手 |
| `media/legacy_image.py` | `_hidden_subprocess_kwargs`：仅定义 244；`_find_ffmpeg`：仅定义 258 | 当前图片准备已使用 Pillow；视频模块自身的同名助手继续保留 |
| `media/legacy_image.py` | `PROJECT_DIR`：定义 59，仅旧 `_find_ffmpeg` 在 264–265 使用；`FFMPEG_COMPRESS_TIMEOUT_SECONDS`：仅定义 69 | 旧图片 FFmpeg 路径/超时常量随该闭包移除；实际压缩缓存目录及清理功能保留 |
| `media/legacy_video.py` | `report_scan_size_skips`：定义 1635，调用 2118；`report_skipped_videos`：定义 1661，调用 2191、2216、2358 | 全部调用均位于孤儿 `_main_impl` |
| `media/legacy_video.py` | `_find_ffprobe`：定义 157，仅 1267 调用；`_find_probe_binary`：导入 60，仅 158 使用 | 仅由本次移除的 `_probe_video_duration` 调用；实际 `video_probe._find_ffprobe` 及真实 FFmpeg 集成测试保留 |
| `media/legacy_mixed.py` | `flatten_items`：定义 347，调用 859 | 调用仅位于孤儿 `_main_impl` |
| `media/legacy_mixed.py` | `report_skipped_mixed`：定义 517，调用 888、1001；`report_scan_size_skips`：定义 540，调用 844 | 全部调用均位于孤儿 `_main_impl` |

同时检索全库 `getattr`、`import_module`、兼容属性、测试补丁目标，未发现表中符号的动态入口。
不能只按“AST 未使用”移除导入：三策略通过 `getattr` 调用 `with_filename_description` 和
`formatted_text`，所以在三个旧模块中保留这些属性。`UploadState`、`file_signature`、扫描、
规划、预检、输入内容构造、快照、临时文件清理仍通过 GUI 和策略被调用，全部保留。
仅移除因该闭包而变为未使用、且无全库兼容引用的标准库导入及旧 TDLib 客户端/版本探测导入。
未删除或改写任何媒体策略、真实封装、停止、预取、GUI 集成测试。

## 最终变更与验证

### 清理结果

- 删除旧媒体链路 22 个顶层函数、3 个旧进度类（30 个方法）、2 个无效常量和 21 个仅闭包使用的导入绑定；三文件净减 1,439 行。
- 这三文件保留的 93 个顶层函数/类与基线 AST 完全一致。另一位代理独立检查全库引用、getattr、字符串、模块导出和 spec，无误删现役入口的阻断发现。
- 删除 1 份被覆盖的 GUI 方法、1 个重复主题导入和 E5 的 10 个未使用导入绑定。
- 移除两个过时手工诊断脚本；合并一对仅样本量不同的基准，保留 5,000 / 10,000 项工作量，并验证实际结果、让异常失败退出。
- 合并 2 个测试方法，全部原有断言、输入与两个排序入口继续执行。新增 8 个配置回归和 2 个发送确认回归，最终 489 项（481 − 2 + 8 + 2）。
- 修复 B1 配置保存/段隔离 Bug 和 B2 重复确认 ID 错误写完成断点 Bug，均先保留失败复现，再修产品代码。

### 覆盖率及执行范围核对

分支覆盖的基线与最终运行使用相同 Python、依赖、offscreen Qt 环境，真实 FFmpeg 两项另行验证。

| 指标 | 基线 | 清理后 |
| --- | ---: | ---: |
| 源码语句覆盖率 | 72.16%（12,029 / 16,670） | 75.89%（11,971 / 15,774） |
| 源码分支覆盖率 | 52.89%（2,796 / 5,286） | 56.56%（2,802 / 4,954） |
| 未覆盖语句 | 4,641 | 3,803 |

覆盖率上升部分来自删除不可达代码，不能单凭百分比认定测试更强。
将基线与最终源码逐行映射后，**11,941 个保留且原先执行过的源码行全部继续执行，没有丢失**。
2,792 条可映射旧分支中，2,791 条原边直接保留；唯一一条差异是 `delivery.py` 新增重复 ID 检查
将原来的直达边拆为经过新检查的两段路径，该检查的通过和拒绝路径均已覆盖。
删除的两个测试没有独有业务断言或覆盖损失。保留现役生产源码的覆盖与百分比分别核查。

### 检查结果与复现方式

使用独立副本的 `src`，复用既有 Python 3.13 环境；审计工具仅装入忽略的 `audit/tooling/`，未改项目依赖。

- 最终完整离线测试：489 项，成功，跳过 2 项需要 FFmpeg/FFprobe 的测试；日志 `audit/final-tests.log`。
- 显式指定本地已构建的 FFmpeg/FFprobe 后，`test_video_remux_pipeline.RealFFmpegIntegrationTest` 两项全部通过，无跳过；日志 `audit/real-ffmpeg.log`。
- 运行核心离线健康检查（未启动 GUI 或登录 Telegram）：`SELF-TEST OK`，日志 `audit/self-test.log`。
- `compileall`（src/tests/benchmarks）、Ruff 未定义名称检查 `F821`、`pip check`、`git diff --check` 均通过。
- 合并基准：5,000 / 10,000 项均完成并核验全部 outcome；从其他工作目录运行及样本量 1 也通过。耗时只作手工诊断，未引入脆弱的 CI 时间阈值。
- B1 与基准另经独立代理复审；B2 与媒体删除另经独立代理复审，均无阻断发现。

主要检查命令（python 指安装了固定运行依赖的 Python 3.13）：

```sh
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen PYTHONPATH=src:tests IMAGEIO_FFMPEG_EXE=/path/to/ffmpeg TDLIB_FFPROBE_EXE=/path/to/ffprobe python -m unittest test_video_remux_pipeline.RealFFmpegIntegrationTest -v
python -m compileall -q src tests benchmarks
python -m pip check
python benchmarks/bench_mixed_resolve.py --items 5000 10000
git diff --check
```

覆盖原始数据与可复核摘要保留在本地 `audit/baseline-coverage.json`、`audit/final-coverage.json`、
`audit/coverage-comparison.json`；B1/B2 修复前失败输出为 `audit/config-red.log`、`audit/delivery-red.log`。
这些本地审计数据和工具没有加入项目依赖或发布包。

### 保留项与尚未验证的边界

- `legacy_*` 模块本身、公开配置兼容、明确的外部集成 wrapper、仍使用的主窗口兼容导出、phase/V2 测试均保留。静态工具的未使用告警不能直接作为删除依据。
- 此轮已确认的两个 Bug 均已修复；未留存确认却未修复的 Bug。此次审计不等于证明仓库不存在其他缺陷。
- 未用真实 Telegram 账号登录/上传，未在真实网络盘持续改写源文件的条件下运行；现有离线回归验证相应状态策略。
- 未在 Windows/Linux 构建发布包；本机运行了离线打包/架构契约与健康检查，跨平台产物仍需 CI 验证。
- 所有改动在独立本地分支，没有推送或合并远端；未更改同步的 `sources/` 或其他工作目录。
