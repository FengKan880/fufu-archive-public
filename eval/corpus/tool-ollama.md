---
id: tool-ollama
title: Ollama 本地模型部署
collection: world_knowledge
domain: tools
---
Ollama 是本地运行大模型的工具，Windows 装 exe 即可，模型一条命令拉取。支持聊天模型（LLaMA、Qwen、DeepSeek）和嵌入模型（Embedding）。

嵌入模型用于把文本变成向量：ollama pull bge-m3 或 ollama pull qwen3-embedding，接口 POST /api/embed 支持一次传多个文本批量计算。Python 侧用 ollama 包几行代码接入。

Ollama 默认用 Q8 量化，质量和速度平衡。CPU 也可运行满足资源条件的中小模型；兼容的 GPU 后端可提供加速。服务常驻 localhost:11434，适合个人知识库的向量化部署。
