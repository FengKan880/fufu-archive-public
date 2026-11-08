---
id: vrc-avatar-creation
title: VRChat 角色模型制作
collection: world_knowledge
domain: vrc
---
VRChat 角色模型（Avatar）的制作流程：建模软件（Blender）中完成模型和贴图 → 绑定骨骼（Humanoid）→ 导入 Unity 工程 → 使用 VRChat SDK 配置参数和菜单 → 上传到 VRChat。

模型面数建议控制在 VRChat 的配额内（PC 端一般 7 万面以下，Quest 端更严格），贴图大小 2K 以内。动态骨骼（Dynamic Bone / PhysBone）用于头发、裙摆等物理效果，过多会拖慢性能。

上传前必须用 SDK 的 Validation 检查通过，否则无法上传。角色支持 VRCFaceTracking 表情追踪，需要模型有对应 BlendShape。
