---
id: ops-hermes
title: Hermes Agent 使用
collection: hermes_ops
domain: ops
---
Hermes 是 Nous Research 出品的 AI Agent 框架，运行在本地，通过会话（Session）与用户交互。支持多种平台接入：桌面应用、Telegram、QQ、Discord。

会话按来源分类管理，可以列出、删除、清理历史会话。Hermes 的配置在 config.yaml，包括模型、提供商、工具开关。技能（Skill）是 Hermes 的程序性记忆，存放在 skills/ 目录。

开新会话与启动子 agent 任务属于不同的会话管理操作，应按宿主文档区分。Hermes 支持 cron 定时任务，可以在指定时间自动执行脚本或提示词任务。
