---
id: dev-unity
title: Unity 游戏开发基础
collection: world_knowledge
domain: dev
---
Unity 是主流的游戏引擎之一，场景由 GameObject 组成，通过挂载 Component（组件）赋予功能。脚本用 C# 编写，生命周期方法包括 Awake、Start、Update、FixedUpdate。

坐标系为左手系，Y 轴向上。物理系统基于刚体（Rigidbody）和碰撞体（Collider），触发检测用 OnTriggerEnter。Prefab（预制体）用于复用对象，UI 用 Canvas 系统。

性能优化要点：减少 DrawCall（合批）、对象池（Object Pool）避免频繁实例化、使用 Addressables 管理资源加载。Unity 也用于 VRChat 世界和角色制作（配合 VRChat SDK）。
