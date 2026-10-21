# 媒体深度审计（2026-10-22）

基线：`d0da14c`，已阅读 `LEGACY_CONSOLIDATION_2026-10-18.md`。范围为 `media/` 及媒体回归；源码快照、缓存验证、发送确认后的清理继续保留。下列证据在对应删除前记录。

## 删除及简化前证据

| 对象 | 引用及运行路径证据 | 处理及保留行为 |
| --- | --- | --- |
| `video_pipeline._NoMediaDate` | 全库精确检索仅有类声明和一个 `except`，没有实例化或 `raise`；实际 `_read_media_creation_metadata` 无日期时返回 `None`，`_media_creation_metadata` 已显式跳过该结果。 | 删除从未发生的异常及包装，保留正结果缓存、取消检查、上限和锁；`test_media_date_failure_is_not_negative_cached` 验证失败后重试。 |
| `video_pipeline._exiftool_rows` | 源码、测试、打包清单、README 和 benchmark 中仅声明；真实调用链为策略 `scan` → `read_exif_metadata` → `_exiftool_rows_detailed`。GUI 动态入口加载模块后调用公开扫描/构建/清理函数，不按名称访问此私有包装。 | 删除无人调用的两值兼容包装；保留详细批次返回及全部 EXIF 场景覆盖。 |
| ExifTool 的 `recursive` 参数及批次布尔值 | `_exiftool_command` 参数从未读取；批次以及缺项递归重试一直传 `False`，实际命令始终 `-@ -` 接收 Python 明确枚举的文件。`test_exiftool_date_query_keeps_full_time_batch` 断言不存在 `-r`。 | 删除没有行为的参数传递与 `(batch, False)` 数据壳；保留缺文件二分重试、取消、告警和进度。 |
| `common.format_size` 循环后的返回、`resolve_processed_filename` 的最后返回 | 前者在末单位 `TiB` 无条件返回；后者候选生成器无限递增且逐个检查有限磁盘/manifest 占位，只有找到空位才返回；循环自然结束不可能。 | 删除不可达 fallback，实际格式及冲突处理不变。 |
| mixed `_item_for_path` 的未知类型大小分支 | `_kind_for` 仅返回 `image`、`video` 或 `None`；函数开头对 `None` 返回，故下方 `media_kind != image and != video` 不可能成立。 | 删除不可达二次类型保护；未知文件仍在同一入口被忽略。 |
| 三 pipeline 的 `DEFERRED_STATUS`，mixed 的 `PROJECT_DIR` | 全库精确检索仅有声明（其他模块同名常量是独立符号）；媒体生产代码使用 readiness 状态或结构化结果。mixed 中根目录常量未被动态查找。 | 删除失效常量及其专用导入。 |
| 未用导入 | `audit/tooling/bin/ruff --select F401,F841` 初报 31 项；全库动态 getter 复核确认三份 `with_filename_description` 和 mixed 的 `formatted_text` 仍经策略使用，已保留并显式转出，增加真实 bundled 策略回归防止误删。其余 27 项无运行用途。`video_prepare.THUMBNAIL_CACHE_DIR` 只有一处无效测试 patch，实际缩略图写入由同一测试已 patch 的 `video_pipeline.THUMB_CACHE_DIR` 控制；顶层缩略图/格式化导入被函数内导入覆盖。 | 删除 27 项过时导入与无效 patch；保留动态接口、全部测试方法和实际缓存隔离、大小/几何/状态断言。 |
| 9 处 `video_prepare.build_thumbnail` patch | 局部回归揭示这 9 处仍访问顶层旧别名；逐个检查为 direct_native/output_name/deterministic_collision/case_collision/three_sources/manifest_disk/manifest_reuse/source_lifecycle/no_native_duplication，所有 prepare 调用都传 `generate_thumbnail=False`。即使开启，函数内从 `video_pipeline` 重导入也会遮蔽它们，无法拦截实际操作。 | 删除这些无作用的 patch；所有测试方法、原有源文件/缓存/碰撞/恢复断言完整保留，真正缩略图测试仍 patch 实际 `video_pipeline` 边界。 |
| `ConfigBehaviorNoAutoMigrationTest.test_existing_config_remains_unchanged` 旧正文 | 只创建本地 dict 后转 set，再断言相同常量，未调用 loader，任何产品代码变动均不可能使其失败；现有测试未实际验证旧视频扩展配置加载时不被注入新后缀。 | 用临时旧 TOML + 真实 `importlib.reload(loader)` 替换自证断言，验证实际 `VIDEO_EXTENSIONS`、mixed 继承和文件内容完全不改；保留测试意图。 |
| `image_pipeline.upload_path` 的路径复用层 | 全库仅声明和 `input_photo` 调用；map 只按源路径查旧副本，不含 size/mtime。真正规范化服务 `prepare_image_for_telegram` 已按源快照和变换参数建立并验证缓存。真实 Pillow 回归在同一路径先建红 BMP、再保存蓝 BMP 并刷新 mixed 扫描快照，改前两次 payload 指向同一旧红 JPEG（`audit/media-stale-image-red.log`）。mixed 预检不清除此 map，故真实混合上传也受影响。 | 删除该过期读取层；每次从原图或已验证 staging 开始，现有规范化缓存负责快照一致的复用。map 继续记录本轮产物以供清理，源快照保护保留。 |
| `is_cancelled` 的吞错及 `fallback_on_error` 参数 | 实际三种 bundled 策略的取消 checker 抛错后仍进入 scanner；新增边界回归三种策略均改前失败（`audit/media-dynamic-cancel-red.log`）。仅 mixed 的私有适配传入 fallback 参数，参数只决定继续执行时回退哪个 false 值，无现役业务需要隐藏此错误。 | 直接传播 checker 错误，在扫描前停止；仍保留没有 checker 时旧 token 的 `cancelled` 属性形状支持，真实取消包装/消息不变。 |

`imageio_ffmpeg` 可执行定位与 Windows 隐藏窗口适配有真实发布用途；策略注入参数、数据快照翻译和隔离锁也仍在测试/GUI/上传链路，不按“Legacy”字样删除。截图时间不参与缓存键由 README 明确约定，保持此已有行为。

## 发现的真实问题

探测函数明确忽略 `disposition.attached_pic == 1` 的封面流，转封装却用 `-map 0:v:0` 选择第一条任意视频流。封面位于真正视频前时，两者选择不一致，会转封装封面并在验证中拒绝原本有效的视频。已改为 `0:V:0` 选择非封面视频流；FFmpeg 7.1.1 官方源码随附 `doc/fftools-common-opts.texi` 明确大写 V 排除封面。修改前回归报 MP4 invalid（`audit/media-attached-cover-red.log`），修改后保留测试并通过。

混合模式的旧图副本复用问题通过删除失效缓存读取层修复：源图改变后即使新快照正确，原逻辑仍可能发送旧 JPEG；现在规范化始终以当前源图为依据。

取消查询发生真实异常时，媒体适配原先也按“未取消”继续运行；现改为向上层传播错误，避免策略继续读取和准备媒体。mixed 原来只在 scanner 返回后查询 token，现入口也检查；其 raiser 正常返回后仍需查询 checker，不把失效 checker 藏在 raiser 存在性之后。

真实 FFmpeg 测试还确认旧 `.m2ts` 造样默认触发 Blu-ray 模式，输入 AAC 已被写成 bin_data（`audit/ffmpeg-probe-m2ts.json`），测试却断言输出保留 AAC。fixture 创建现在显式关闭 M2TS 模式，以普通 TS 内容验证后缀识别及 AAC 无损流复制；不删除用例、不放宽 codec 断言。

## 验证

| 检查 | 结果 |
| --- | --- |
| 媒体探测、准备、三策略、EXIF、排序、分组、缓存及上一轮审计回归 | 197 项，195 成功，2 项真实 FFmpeg 默认环境跳过；日志 `audit/media-audit-tests.log` |
| 显式使用本轮编译 FFmpeg / FFprobe 7.1.1 的两项真实集成测试 | 主审计补测通过，无跳过；日志 `audit/personal-real-ffmpeg-final.log` |
| bundled 模块动态 getter 复核 | 逐 AST 提取三策略对实际 pipeline 的模块 getter，所有字段存在；有价值的动态转出完整保留 |
| 媒体 Ruff F401 / F821 / F822 / F823 / E9 | 通过；转出使用显式同名 alias，静态工具也能识别 |
| 媒体及已改测试 compileall、git diff --check | 通过 |
| 测试方法前后 AST 对照 | 三个已改文件原测试方法全部保留，新增 4 项；1 项无产品调用的自证测试换成真实 loader 行为 |

媒体生产源码净减少 52 行；删除 10 个无效 patch，新增缓存内容、正确流选择、真实动态 caption 和取消入口行为覆盖。未修改跨模块布局/架构测试，未执行真实 Telegram 上传。全量回归由主审计统一收束。
