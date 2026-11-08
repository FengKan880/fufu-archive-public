---
id: vrc-network-tuning
title: VRChat 网络调优
collection: world_knowledge
domain: vrc
---
VRChat 对网络质量敏感，尤其是多人房间内。网络调优可以从几个方面入手：

1. 选择低延迟节点：延迟比带宽更影响实时同步体验，但世界加载和视频播放又需要足够带宽，所以推荐带宽优先、延迟其次的策略。
2. 使用 Hysteria2 协议：基于 QUIC/UDP，抗丢包能力强，适合 VRChat 长连接。
3. 关闭自动 fallback：自动切换节点会造成连接抖动，固定节点更稳定。
4. 清理 DNS 缓存：VRChat 依赖域名解析，DNS 污染会导致连接失败。

如果出现频繁断线，先检查代理节点的稳定性，再检查本地网络（Wi-Fi 信号、路由器）。
