---
id: vrc-cache-cleanup
title: VRChat 缓存清理
collection: world_knowledge
domain: vrc
---
VRChat 会把世界、头像模型、贴图缓存在本地，长时间游玩后缓存会越来越大，可能导致启动变慢、世界加载卡顿甚至游戏异常。

清理缓存的方法是删除 Cache-WindowsPlayer 和 TextureDiskCache 这两个目录下的内容。Cache-WindowsPlayer 存放模型和世界缓存，TextureDiskCache 存放贴图纹理缓存。删除后 VRChat 会在下次进入世界时重新下载，属于安全操作。

缓存清理无法通过游戏内设置完成，需要退出游戏后在文件管理器中手动删除，或者使用第三方清理脚本。
