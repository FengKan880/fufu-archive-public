---
id: vrc-node-switch
title: VRChat 代理节点切换
collection: world_knowledge
domain: vrc
---
VRChat 的网络分为两层：Photon 负责房间内的实时同步（玩家位置、动画、交互），EOS（Epic Online Services）负责好友、邀请、头像下载等账号侧服务。两者都是长连接。

切换代理节点会改变出口 IP，导致 Photon 和 EOS 连接全部断开，因此 VRChat 不能中途切换节点——一切换就必须重启游戏，重连后才能正常游玩。使用机场订阅时，建议固定一个低延迟节点玩 VRChat，避免自动 fallback 造成连接抖动。

Hysteria2 协议基于 UDP，比 VLESS 更适合 VRChat 这类长连接场景，因为它的 QUIC 传输在丢包环境下表现更稳定。带宽优先、延迟其次的配置思路适合视频播放和世界加载。
