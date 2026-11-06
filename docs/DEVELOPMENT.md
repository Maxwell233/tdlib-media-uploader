# 开发与验证

应用使用发布包启动。修改源码后可运行以下命令检查 Python 语法。

```sh
python -m compileall -q src benchmarks
```

外部进程的 `max_output_bytes` 按每个输出流的字节数限制保留内容。超出部分持续排空丢弃，未指定时保留完整输出。

## 职责边界

- `upload/engine.py` 保留 Album 状态转换和取消策略；`upload/prepared_media.py` 将扫描源和本地载荷绑定为不可变快照。
- `core/source_snapshot.py` 供图片、视频准备及最终发送边界共用；处理文件只有在转换前后源快照一致时才能发布缓存记录。
- `upload/delivery.py` 统一发送器返回值；实际 TDLib 客户端与独立测试客户端使用同一个 `telegram/send_result.py` 消息映射器。
- `core/upload_state.py` 保留原有 JSON 格式，整批写入临时文件、刷盘并原子替换后才更新内存完成状态；保存或重置失败时保留原记录，无需格式迁移。
- `core/compat.py`、`core/sorting.py`、`core/concurrency.py` 分别集中旧接口调用适配、排序及有界调度。文件系统、就绪检查和外部进程各只有一份正式实现；媒体流程的共用策略集中在 `media/common.py`，策略适配共用逻辑集中在 `media/strategy_support.py`。
- GUI 上传与 TDLib 使用只读配置快照；上传根目录在任务开始时固定，兼容作用域不再改写相同根目录。媒体流程配置引用由任务作用域接入并在结束时恢复。
- Telegram 登录、请求与发送统一使用 `telegram/tdlib_common.py`；测试直接验证这个实际入口，不维护平行的客户端或鉴权状态机。状态与日志直接注入上传引擎。
- 打包模块由 `collect_submodules` 自动发现，构建发现失败直接报告；依赖数据保留，仅排除 imageio-ffmpeg wheel 自带的可执行文件。
- `gui/workers.py` 承担缓存删除，主线程处理进度和完成事件；直到线程结束才解除操作互斥。取消不会撤销已删除的文件。

## 发布包验证

Windows/macOS 构建会执行包内的 `--self-test`，检查配置模板、可写数据目录、TDLib 和内置媒体工具。构建还会校验平台产物。

macOS 工具缓存以源归档 SHA-256、平台、架构及 workflow 内容摘要为键；后者包含完整构建标志和缓存验证逻辑。命中时仍检查版本、运行状态、许可和架构。无可用缓存时校验源码归档后构建；禁用外部库自动探测以避免宿主机可选依赖渗入产物。

## 人工验证边界

可使用独立目录核对真实账号上传、网络盘持续写入、停止后退出，以及跨版本数据备份与恢复。直接路径发送的最终 stat 只能缩短竞态窗口。持续改写的源可启用暂存副本。
