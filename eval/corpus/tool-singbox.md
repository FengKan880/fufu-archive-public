---
id: tool-singbox
title: sing-box 代理原理
collection: world_knowledge
domain: tools
---
sing-box 是新一代代理内核，支持多种协议（VLESS、Hysteria2、Trojan、Shadowsocks）和多种流量接管方式：系统代理、TUN 模式、透明代理。

TUN 模式在系统层面创建虚拟网卡，接管全部流量（包括不走系统代理的程序），适合游戏等应用的全局代理。系统代理只影响遵守系统代理设置的程序，很多游戏和命令行工具不认系统代理。

GUI.for.SingBox 是 sing-box 的图形化管理工具，支持节点选择、规则配置、缓存持久化（Store Cache 可以记住上次选的节点）。规则（Rules）决定哪些流量走代理、哪些直连，可按域名、目标地址或应用设置策略。改代理配置应使用部署环境提供的管理入口，核对实际生效规则；图形界面和配置文件的管理方式由具体工具决定。
