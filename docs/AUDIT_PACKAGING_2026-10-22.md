# 打包与跨模块测试审计（2026-10-22）

## 删除前证据 P1：重复模块清单与静默失败

基线 `d0da14c` 的 spec 同时手写 78 个 `PACKAGE_HIDDENIMPORTS`，又调用
`collect_submodules(PACKAGE_NAME)`，最后用 set 去重。使用项目固定版本
PyInstaller 6.22.2 和当前 src 实际执行：自动扫描得到 96 个模块，包含手写清单的
全部 78 项，差集为空（`audit/packaging-baseline.log`）。两条路径不承担不同职责。
`rg` 确认该常量只在 spec 本身和 `tests/test_phase2_layout.py` 的写法检查中使用。

删除手写清单，保留单一自动扫描及 tdjson/PIL/imageio_ffmpeg 外部模块入口。
自动扫描失败时的 except Exception 只会吞掉错误，没有恢复或诊断作用，还可能
生成缺少动态模块的产物，移除该静默分支。依赖 collect_all 的同类包装也没有必要：
当前固定版本实际扫描 tdjson/imageio_ffmpeg 均成功，分别收集 3/0 个二进制和 3/6
个模块；原先“单文件扩展不支持”的前提不符合当前依赖（tdjson 是 package）。
现有项目明确要求这些依赖，失败应在构建时报告。

## 测试取舍

原 spec 测试重复维护 59 项旧模块子集，不能证明 Analysis 真正接收了隐藏模块，
也不能验证依赖资源的合并及内嵌 FFmpeg 排除。修改现有测试为执行完整 spec，
在构建器边界捕获实际参数，继续验证入口、搜索路径、自动发现模块、外部依赖
及资源目的地，并加入构建发现失败不会被吞掉的断言。运行时资源解析、发布校验和、
冻结路径与架构边界测试继续保留。

没有找到 AST 完全相同的测试方法。不能因为历史 Phase/V2 名称就判定测试冗余。
FFmpeg 许可过滤、资源打包和发布校验和有具体的发布消费者，继续保留。

## 真实 Bug P2：普通依赖数据被当作可执行文件排除

新增的 spec 执行测试先失败（`audit/packaging-tests.log` 初次输出）：
`/imageio_ffmpeg/metadata.json → imageio_ffmpeg` 被排除。
原过滤器同时检查源与目的目录；目的目录本身含 ffmpeg，导致普通文件也被过滤。
用真实 collect_all 核对：当前依赖的源码数据和 dist-info 也落入旧过滤器。
改为只按源路径中 imageio_ffmpeg 包的可执行文件名判断，保留普通数据/元数据，
继续排除 wheel 自带 FFmpeg。测试同时验证原生 TDLib 库保留。

## 验证

最终结果由主审计报告补入；临时构建工具只安装在忽略的 audit/build-tooling，
不修改项目运行依赖。

最终：spec/发布/架构 11 项通过；随后全量 509 项成功（2 项真实 FFmpeg另行补测通过）。
使用真实 PyInstaller 6.22.2 hooks 执行最终 spec，91 个现存模块全部进入 Analysis，
共 101 项 hiddenimports、25 项 data、3 项 binary，无缺失模块。
真实构建器未运行；没有把 hook/参数检查称为完整平台构建。
