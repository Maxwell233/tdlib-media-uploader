# Legacy 功能合并（2026-10-18）

本轮基于上一轮清理提交 `1983659`。目标是将所有仍有用的 Legacy 实现纳入职责明确的正式模块，消除重复逻辑与旧文件入口；配置和上传断点格式保持兼容。

## 迁移前核查

共有四个 Legacy 源文件：`core/filesystem_legacy.py`、`media/legacy_image.py`、`media/legacy_mixed.py`、`media/legacy_video.py`。
它们仍在生产调用链上，不能按文件名直接删除。文件系统重复实现已有 `filesystem/readiness/models/concurrency/processes.runner` 对应职责；媒体中仍有用的实现迁到对应 pipeline，跨媒体及策略层重复辅助逻辑分别合并。

原始引用、构建动态入口、共享代码语义差异、测试和最终核查见下列各节。任何删除以调用迁移完成和测试验证为前提，保留已明确的兼容参数及数据读取能力。

## F1：文件系统职责合并（删除前证据）

基线 `1983659` 的 `core/filesystem_legacy.py` 有 1302 行，生产调用仍有效；本轮删除的是重复实现及旧入口，不删除对应功能。
实际迁移路由如下，调用与测试先切换，再移除旧模块：

| 旧职责/符号 | 正式归属 | 语义与保留证据 |
| --- | --- | --- |
| `FileSnapshot` | `core.models.FileSnapshot` | 字段、不可变性、`as_tuple()`、解包相同；统一扫描、媒体和上传使用的实际类型 |
| `FileReadiness`/错误/状态、probe/check/wait/is_file_stable | `core.readiness` | 相同 stat/read/stat、大小/mtime 校验、有限等待；正式实现额外支持 token 和预取消 |
| 路径、UNC、网络识别、目录扫描、snapshot、重试 | `core.filesystem` | 相同 lstat/reparse 拒绝策略、一文件一次 stat、目录迭代断流重试和取消；低层结果名用 `DirectoryScanResult`，保留两值解包、索引、warnings、snapshots |
| 自然排序、相对路径排序 | `core.sorting`，filesystem 保留现有转出 | 原有排序输入及断言继续执行；统一数值段及确定性比较，重复入口收敛为正式转出，避免保留第二个比较器 |
| sleep、bounded map | `core.concurrency` | legacy bounded map 原本只是包装正式调度器；正式 sleep 修正非正 quantum 并支持 token |
| 外部进程 | `processes.runner` | 保留无取消时 subprocess.run 路由、UTF-8、双管道排空、进程组取消/超时/reap；正式实现还支持 bounded output 和参数冲突校验 |

逐函数比对发现两个必须保留的差异：旧 `iter_files(root, None)` 表示不过滤后缀，已有 `test_directory_entry_filter.py` 验证嵌套未知后缀和快照，正式实现需补入该分支；macOS mount 诊断在旧实现通过统一进程执行器运行，正式实现需改用同一 `run_cancellable_process` 边界。
原 `revalidate_file` 的 tuple 返回未有生产/测试调用；正式模块已有结构化 FileSnapshot 返回及解包，本轮统一该实现，并让 readiness 转出同一函数，消除另一份重复校验正文。

测试的 patch 归属也必须迁移：readiness 的 sleep 在 `core.readiness` patch，mac mount 的执行器在 `core.filesystem` patch，不能继续 patch 已移除的外观模块导致伪覆盖。
保留全部有价值的断言：UNC/Windows 路径、symlink/reparse 拒绝、扫描重试/中途恢复、每文件 stat 次数、stability、取消、双管道/输入、输出限额和子进程回收；不同层入口测试继续保留。


## 媒体管线合并：删除重复实现前证据（media agent）

核对基线 `1983659`：三个 `media/legacy_*.py` 仍执行扫描、分组、准备与内容构建，不能删除其功能。
本次合并下列重复实现后迁入 `image_pipeline.py`、`mixed_pipeline.py`、`video_pipeline.py`，不留下旧文件。

| 重复功能 | 基线定义位置 | 可合并证据与保留差异 |
| --- | --- | --- |
| readiness 配置 | image:66；mixed:67；video:416 | 三份配置字段、network/local默认值及旧字段回退算法相同。来源根目录不同；image/video模块默认attempts取初始化配置，mixed默认3，保留为显式参数。共同算法接收config与network标志，不读取或回写模块cfg。 |
| readiness 错误记录 | image:89；mixed:89；video:440 | 除docstring外三份AST相同：只处理FileReadinessError并保留status/code/attempts/category。合并到common后继续以原私有名称导入。 |
| 大小显示 | image:103；mixed:114；video:166 | 单位与进位格式相同。mixed原先对空值执行value or 0；保留mixed薄适配，其余调用同一个format_size。 |
| 旧暂存过期清理 | image:116；mixed:127；video:217 | 三份startup开关、目录/base目录和max_age算法相同。合并到显式config的common函数，三pipeline保留GUI调用入口。仍在禁用新暂存时清理旧缓存。 |
| 上传确认后的暂存清理 | image:130；mixed:141；video:232 | image/video循环相同；mixed允许dict或Path且清除两类map。common统一配置开关及pop/remove循环，mixed保留输入适配并显式提供两个map；不清未确认源与其他map键。 |

检索上述符号的全库调用与测试patch目标，现有测试未patch各媒体模块的cleanup_staging或remove_staged_file；
合并后的底层暂存服务可在media.common对应名称处patch，pipeline公共cleanup入口仍保留。
`cfg`、扫描诊断和快照map、`UI`、UploadState、预检、缩略图、真实封装、相册边界及恢复行为继续属于各管线。
`file_signature`的扫描快照来源和根目录绑定不同，保留专用入口，避免抹平来源身份语义。
现有策略、GUI集成、停止/预取、源快照与真实FFmpeg测试全部保留；新增独立common测试验证共享算法及配置隔离。


## S1：策略共用辅助逻辑合并前证据

基线 `1983659` 的三策略仍各自实现相同边界协议，拟将下列相同实现合并到
`media/strategy_support.py`，不删除任何测试或媒体业务分支。以下行号均为本轮修改前。

| 重复实现 | 原位置 | 等价依据及迁移后保留的差异 |
| --- | --- | --- |
| 取消判断、可取消异常包装、Event 形状 | `image.py:42–83`、`video.py:45–64,117–141`；`mixed.py:42–71` | 图片/视频 checker 失败均返回 false，raiser 抛带 cancelled 的异常均包装为 cancelled=true 的 RuntimeError；消息文字作为参数保留。混合策略 checker 失败后仍读取 cancelled 属性，raiser 异常原样传播；此区别保留，仅复用带显式 fallback 参数的判断函数。视频 token=None 时仍向底层传 None，图片/混合仍传 Event bridge。 |
| 日志 sink 转发 | `image.py:86–109`、`mixed.py:74–97`；`video.py:74–82,280–286` | 图片/混合 warning/info/log 正文相同，仅 LogEvent.source 不同，移为带 source 的共享小型 UI；消息转换及 sink 失败仍不能改变媒体决策。视频独有 progress/finish 与消息构造时机保留，只复用安全事件转发函数。 |
| 完整模型 identity | `image.py:112–120`、`mixed.py:100–108` | 两者均为 path、source_root、media_kind、snapshot.path、snapshot.size、snapshot.mtime_ns 六元组；字段顺序及 Path/string 转换完全相同，不改 path-only 匹配规则。 |
| 原始快照翻译 | `image.py:123–143`、`mixed.py:111–131` | 同样依次接受 FileSnapshot、属性对象、二元快照、scan_size/size 与 scan_mtime_ns/mtime_ns；唯一差异是缺快照错误前缀，作为参数保留。视频使用 _as_int 宽松转换及不同缓存 fallback，不强行合并。 |
| 状态完成查询 | `image.py:335–357`、`mixed.py:349–362`、`video.py:423–435` | 同样先检查 state/is_completed，再依次尝试 MediaItem 与各策略原有字典；四种兼容异常吞掉后继续，其他异常传播。共享探测循环，字典工厂仍由策略提供，视频/混合附加字段不丢失。 |
| 目标字典翻译 | `image.py:360–367`、`video.py:222–229` | Mapping 复制、as_dict() 调用、非 Mapping 回退空字典的行为完全相同，只是局部变量 converter/as_dict 名不同；保留原静态方法入口委托共享函数。 |
| 按快照构建路径索引 | `image.py:318–333`、`video.py:476–494` | 均按规范路径、size、mtime_ns、media_kind 建唯一索引并维护 path/kind 多值表，重复时 ValueError；仅媒体名错误前缀不同，作为参数保留。真实匹配函数仍各自保留，图片严格 int 与视频宽松 _as_int 以及缺 path 默认规则不合并。 |

三个 StateBridge 的 resolver 参数与 lookup 形状不同，根目录/快照全局的加锁与恢复范围亦不同，
继续留在各策略，避免通用基类掩盖行为差异。构造参数 legacy/legacy_module 保持兼容，
模块加载路径由主代理统一迁移。现有策略、查找、预检、取消、内容准备和上传一致性用例继续保留。

F1 局部验证：既存 None-filter 回归先切正式入口，产生 `TypeError: 'NoneType' object is not iterable`（`audit/filesystem-consolidation-red.log`）；补齐正式实现后，目录过滤和 filesystem/readiness/concurrency/process 全部 24 项通过（`audit/filesystem-consolidation-green.log`）。
补充用例验证正式路径下的 Windows UNC 不解析网络、映射盘并发分类、两种正式重验入口的快照/mtime/大小变化与删除拒绝；无新增第二份实现。原排序两入口循环在统一后收敛为一次 filesystem 正式转出入口断言，保留全部原输入和期望，sorting 的独立自然排序用例继续保留。


## 全库迁移及真实 Bug 修复

- 所有源码导入按职责显式拆分；GUI 动态模块表、扫描 fallback、恢复流程、健康检查、PyInstaller hidden imports 和测试 patch 目标均迁入正式模块。
- `core/filesystem_legacy.py` 的生产和测试引用清零后才删除；三个媒体旧路径已全部切换为 pipeline，不保留重复实现或临时 facade。当前源码、测试和 spec 不再引用四个旧模块。
- `legacy` / `legacy_module` 策略注入参数及已有字典、配置、JSON 断点兼容继续保留。它们是仍使用的边界接口，不构成第二份实现。
- 避免只做文件改名：文件系统和 readiness 共用一种快照/重验实现；媒体 common 合并五类策略；strategy_support 合并取消、日志、身份、快照、状态与路径索引辅助逻辑。
- 发现并修复基础混合预览的实际 Bug：`_process_mixed_group` 用 `key_func=` 调用只接受 `path_key=` 的排序函数，有文件的预览会抛 TypeError。新增真实临时目录、两张非空图片路径的回归，修复前失败见 `audit/mixed-preview-red.log`，修复后自然顺序及无错误结果断言通过；没有删除或放宽失败测试。

## 最终验证

| 检查 | 结果 |
| --- | --- |
| 全套离线回归、GUI/架构/打包契约 | 500 项成功，跳过 2 项需真实 FFmpeg 的测试 |
| 显式指定本地 FFmpeg/FFprobe 的两项集成测试 | 2 项成功，无跳过 |
| 核心离线健康检查 | SELF-TEST OK，无登录/网络发送 |
| compileall（src/tests/benchmarks） | 成功 |
| Ruff F821/F822/F823/E9 | 成功，无未定义名称、导出及语法错误 |
| pip check / git diff --check | 成功 |
| 混合 outcome 基准 | 5,000 / 10,000 项均完成并校验结果，约 0.045 / 0.084 秒，仅供本机诊断 |
| 原测试方法核对 | 基线 489 个方法全部保留，新增 11 项；没有删除方法掩盖失败 |
| 运行入口检查 | 源码、测试、spec 对四个旧模块的引用为零，Legacy 源文件为零 |

主要测试输出：`audit/consolidation-final.log`、`audit/consolidation-ffmpeg.log`、`audit/consolidation-selftest.log`。
完整回归在最终文件删除、调用迁移、打包清单和文档更新后运行。

### 源码精简和覆盖核对

本轮 Python 源码由 **30,482 行降至 28,983 行，净减少 1,499 行**（不计测试/文档）。
三份 pipeline 保留的 78 个非合并业务函数/类在迁名前后 AST 不变；共享策略辅助逻辑还对照基线做了异常及边界矩阵核验。

| 覆盖指标 | 基线 1983659 | 合并后 |
| --- | ---: | ---: |
| 语句覆盖 | 75.89%（11,971 / 15,774） | 77.78%（11,680 / 15,017） |
| 分支覆盖 | 56.56%（2,802 / 4,954） | 58.90%（2,714 / 4,608） |

旧重复实现被移除会减少覆盖计数的分母和分子，不能单凭比例证明覆盖保留。
对剩余代码及三文件迁名做逐行映射，**11,276 个原先已执行且保持不变的源码行全部继续被执行，没有丢失**。
原有测试保留全部有价值断言，并改在实际归属模块 patch/运行，避免旧入口移除后出现伪覆盖。
原始覆盖与映射数据保存在 `audit/consolidation-baseline.json`、`audit/consolidation-final.json`、`audit/consolidation-coverage-comparison.json`。

## 交付边界

本轮已确认的基础预览 Bug 已修复，旧行为差异已迁移，没有确认却未修复的在途问题。
未进行真实 Telegram 账号上传或 Windows/Linux 发布包构建；这些仍需真实环境/平台 CI。
配置、目标标识、JSON 断点和上传日志格式没有迁移。所有工作位于独立本地分支，未推送或合并远端，未修改同步 sources/。
