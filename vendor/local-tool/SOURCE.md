# 本机参考实现

来源：同一作者的「抖音数据看板」`local-tool/research_bili_search.py`、`research_bilibili.py`、`research_youtube.py`，MIT 协议。核对日期：2026-11-06。原项目仅只读参考，未修改。

本项目 `bili_worker.py` 改编了已安装 B站 SDK 的搜索、时间范围和公开元数据调用；`platforms.py` 改编了子进程隔离、yt-dlp 元数据读取、未知计数保留为空、限流即停的方式。新增 UP 主粉丝读取、官方 YouTube API、趋势解析和平台调度由本项目实现。完整 MIT 许可证保存在本目录。
