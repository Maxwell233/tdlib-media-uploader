### V1.9.0 更新

- 所有用户数据统一放入 `data/`，包含配置、Telegram 登录、断点、标题、缓存、历史和日志；复制整个目录即可迁移 V1.9 数据。
- Telegram/TDLib 登录数据库固定使用 `data/telegram/database` 和 `data/telegram/files`，程序目录与 `_internal` 保持只读。
- 暂存目录改为带 marker 的受管目录，只清理程序创建的副本，避免误删用户文件。
- 视频、图片和混合模式共用媒体身份与断点格式；Album 身份使用扫描快照，网络目录短暂断线不会改变分组。
- Album 部分成功、取消或确认超时会进入 `UNKNOWN` 并阻止自动重发；发送日志记录成功、失败和待确认消息。
- 普通账号的视频上限为 2 GiB，Premium 上限为 4 GiB；Caption 长度按 TDLib 当前配置检查。
- 增加单实例锁、应用日志轮转和 `--self-test` 离线健康检查。
