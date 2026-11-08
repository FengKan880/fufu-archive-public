---
id: vrc-ytdlp-video
title: VRChat 地图内视频播放原理
collection: world_knowledge
domain: vrc
---
VRChat 世界内的视频播放器（Video Player）依赖 yt-dlp 这类下载工具去拉取视频流。世界作者通常在 SyncVideoPlayer 等组件里配置视频 URL，客户端通过 yt-dlp 请求解析视频地址再播放。

游戏加速器（UU、雷神这类）通常只代理游戏流量，不管 yt-dlp 发出的 HTTP 请求，所以地图内视频经常加载失败。机场代理（Clash、sing-box 等）是全局代理，能覆盖这些请求，因此 VRChat 地图内看视频用机场代理更可靠。

YouTube、Twitch 等平台的视频在中国大陆无法直连，需要代理节点能访问这些站点才能正常播放。
