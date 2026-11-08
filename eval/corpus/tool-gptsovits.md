---
id: tool-gptsovits
title: GPT-SoVITS 语音合成
collection: world_knowledge
domain: tools
---
GPT-SoVITS 是开源语音克隆与文本转语音工具，可基于授权使用的参考音频生成语音。部署时按项目文档配置 Python 环境、WebUI 与 API 服务端口；不要将机器专属安装路径写入通用说明。

推理参数影响音质：top_k=5、top_p=0.7、temperature=0.6 是稳定参数；长文本、语速不等于 1.0、高 temperature 是爆音（破音）三大诱因。

推理参数会影响音质。top_k、top_p 与 temperature 可按模型和任务调节；文本长度、语速及较高随机性都可能影响稳定性，应以受控样例评估并针对目标语种和声音选择设置。使用参考音频前应确认权利与同意；按部署环境的隐私和数据处理要求运行。
