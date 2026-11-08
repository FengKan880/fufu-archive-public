---
id: tool-ai-image
title: AI 生图工具
collection: world_knowledge
domain: tools
---
AI 生图工具分云端和本地：云端如 OpenAI 的 gpt-image 系列、Midjourney，本地如 ComfyUI（基于 Stable Diffusion 工作流）。

ComfyUI 是节点式工作流工具，把采样、提示词、模型、后期处理串成流程图，灵活但上手门槛高。文生图（Text-to-Image）从提示词生成图，图生图（Image-to-Image）基于已有图片编辑变换。

图生图路由会走编辑端点，支持参考图风格迁移。价格按分辨率分档（low/med/high），最高可到 4K。提示词工程：主体描述 + 风格 + 构图 + 光线 + 负面词（排除不要的元素）。
