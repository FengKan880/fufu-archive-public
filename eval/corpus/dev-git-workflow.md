---
id: dev-git-workflow
title: Git 与 GitHub 工作流
collection: world_knowledge
domain: dev
---
Git 是分布式版本控制系统，核心概念：仓库（Repository）、提交（Commit）、分支（Branch）、远程（Remote）。

日常流程：git add 暂存 → git commit 提交 → git push 推送到远程。分支用于隔离开发，功能完成后合并（Merge）或变基（Rebase）回主分支。

GitHub 是代码托管平台，支持私有仓库、Issue、PR（Pull Request）。Windows 上推荐使用 GCM（Git Credential Manager）实现免 token 登录，push 时会自动弹出浏览器授权。克隆仓库用 git clone，SSH 和 HTTPS 两种方式都可以。
