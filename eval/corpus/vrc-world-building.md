---
id: vrc-world-building
title: VRChat 世界制作
collection: world_knowledge
domain: vrc
---
VRChat 世界（World）使用 Unity 制作，通过 VRChat SDK 上传。世界制作流程：搭建场景（地形、物体、光照）→ 添加交互组件（传送门、视频播放器、音乐播放器）→ 测试 → 上传。

视频播放器使用 Video Player 组件配合 URL，音频播放器（AudioReactive）可以让物体随音乐节奏运动。世界性能同样重要：光照贴图、LOD、材质合批都能优化加载速度。

世界与角色模型共用 SDK，但需要单独构建上传。世界内可以放 Udon 脚本实现自定义逻辑，Udon 是 VRChat 的图灵完备脚本系统。
