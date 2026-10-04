# 开发与验证

应用使用发布包启动；以下命令仅运行离线测试，不是源码启动入口。

```sh
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen PYTHONPATH=src:tests IMAGEIO_FFMPEG_EXE=tools/ffmpeg/ffmpeg TDLIB_FFPROBE_EXE=tools/ffmpeg/ffprobe python -m unittest test_video_remux_pipeline.RealFFmpegIntegrationTest -v
```

第二条为 macOS/Linux 示例，需先准备可运行的 FFmpeg/FFprobe。测试覆盖乱序消息确认、源文件变化、旧断点导入及写入失败回滚、延迟关闭、后台清理取消、标题读取次数和图片元数据复用。外部进程的 `max_output_bytes` 按每个输出流的字节数限制保留内容，超出部分持续排空丢弃；未指定时保持原有完整输出语义。

## 职责边界

- `upload/engine.py` 保留 Album 状态转换和取消策略；`upload/prepared_media.py` 将扫描源和本地载荷绑定为不可变快照。
- `core/source_snapshot.py` 供图片、视频准备及最终发送边界共用；处理文件只有在转换前后源快照一致时才能发布缓存记录。
- `upload/delivery.py` 统一发送器返回值；实际 TDLib 客户端与独立测试客户端使用同一个 `telegram/send_result.py` 消息映射器。
- `core/upload_state.py` 保留原有 JSON 格式，整批写入临时文件、刷盘并原子替换后才更新内存完成状态；保存或重置失败时保留原记录，无需格式迁移。
- `core/compat.py`、`core/sorting.py`、`core/concurrency.py` 分别集中旧接口调用适配、排序及有界调度。旧模块仍作为兼容入口，避免一次性重写全部媒体逻辑。
- GUI 上传与 TDLib 使用只读配置快照；上传根目录在任务开始时固定，兼容作用域不再改写相同根目录。旧模块引用仍由任务作用域接入并在结束时恢复。
- `gui/workers.py` 承担缓存删除，主线程处理进度和完成事件；直到线程结束才解除操作互斥。取消不会撤销已删除的文件。

## CI 分工

快速 CI 在 Linux 上检查离线行为与架构，通常不具备 FFmpeg 工具，因此真实媒体测试可能跳过。Windows/macOS 平台 CI 在构建工具准备后显式运行真实封装测试，再执行包自测和平台产物验证。

macOS 工具缓存以源归档 SHA-256、平台、架构及 workflow 内容摘要为键；后者包含完整构建标志和缓存验证逻辑。命中时仍检查版本、运行状态、许可和架构。无可用缓存时校验源码归档后构建；禁用外部库自动探测以避免宿主机可选依赖渗入产物。

## 人工验证边界

离线测试不会登录或向 Telegram 发送消息。合并前还可用专门的测试目录核对真实账号上传、网络盘持续写入、停止后退出，以及跨版本数据备份与恢复。直接路径发送的最终 stat 只能缩短竞态窗口；持续改写的源应启用经过快照校验的暂存副本。
