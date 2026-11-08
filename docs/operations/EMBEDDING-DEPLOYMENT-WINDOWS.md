> **文档性质：部署实录**
> 本文从技能参考资料迁入，记录 Windows/Ollama 的实测部署方法。

# Embedding 模型 Windows 部署调研（2026-08，P1 用）

背景：个人中文知识库 SQLite+sqlite-vec（Python 嵌入式），候选 bge-m3(568M)/Qwen3-Embedding-0.6B/bge-base-zh-v1.5，本地推理优先。结论基于官方文档/源码/HF 页面交叉验证。

## 决策速览

| 方案 | Windows | CPU/GPU | 安装复杂度 | 与 Python/sqlite-vec 集成 | 结论 |
|---|---|---|---|---|---|
| Ollama | ✅ exe 原生 | CPU / NVIDIA CUDA / AMD ROCm 自动 | 最低 | REST /api/embed，`ollama` pip 包 | **Top1** |
| TEI | ❌ 仅 Docker/WSL2 | GPU 主（有 cpu 镜像但非重点） | 高 | OpenAI 兼容 | 排除 |
| vLLM / SGLang | ❌ Linux 优先 | GPU only (CUDA) | 很高 | OpenAI 兼容 | 排除 |
| sentence-transformers + ONNX | ✅ pip 原生 | CPU 有 int8/OpenVINO 加速 | 中 | 进程内零服务零网络 | **Top2** |
| Infinity (michaelfeil) | ⚠️ pip 可装，官方主打 Linux/容器 | CPU/GPU | 中 | 常驻服务 + OpenAI 兼容 | 备选 |
| FastEmbed (Qdrant) | ✅ pip 原生 | CPU（ONNX，不装 torch） | 低 | 库级调用 | 备选（缺 bge-m3） |
| llama.cpp | ✅ 预编译二进制 / winget | CPU 主 | 中 | llama-server /v1/embeddings 或 llama-cpp-python | 备选 |

## 关键事实

- **Ollama 库现状**：`bge-m3` 官方有（1.2GB Q8_0，8K 上下文，5.6M 下载）；`qwen3-embedding` 有 0.6b(639MB/32K ctx)/4b(2.5GB)/8b(4.7GB/40K)；**`bge-base-zh-v1.5` 库内 404 不存在**（只能自转 GGUF 或换模型）。
- `/api/embed` 的 `input` 支持字符串**数组 = 原生批量**（索引管线直接 POST 一批 chunk）；Python 用 `ollama.embed(model=..., input=[...])`。
- **TEI**：README「Local Install」只支持 Apple Silicon；Windows 必须 Docker Desktop(WSL2)。附带价值：能顺带跑 reranker（bge-reranker / gte-multilingual-reranker-base）。
- **vLLM**：`vllm serve <model> --task embed`（旧名 `--embedding-mode`）；**SGLang**：`sglang serve --model-path ... --is-embedding`（encoder 架构自动识别，decoder 型才需 flag）；都出 OpenAI `/v1/embeddings`；都 Linux+CUDA only。
- **sentence-transformers 三后端**：PyTorch（默认）/ ONNX / OpenVINO；`SentenceTransformer("BAAI/bge-m3", backend="onnx")`；官方效率文档 CPU 基准**含 bge-m3**；现成 ONNX：onnx-community/bge-m3-ONNX；int8 动态量化（avx512_vnni）CPU 短文本 ~3.08x；官方流程图：Intel CPU + 短文本 + 可接受 ~0.4% 损失 → OpenVINO qint8，否则 onnx。
- **Infinity**：`pip install infinity-emb[all]`，引擎 PyTorch/optimum-ONNX/CTranslate2，加速器含 CPU，int8(cpu/cuda) 支持；bge-m3 官方 issue 确认兼容（GPU 2-3x）；README 用法以 docker `latest-cpu` 镜像为主，Windows 属社区自测状态。
- **FastEmbed**：⚠️ **官方模型清单无 bge-m3 也无 bge-base-zh**，中文只到 `BAAI/bge-small-zh-v1.5`（512 维）——达不到 bge-m3 级别；要 bge-m3 需 `add_custom_model` 自备 onnx 文件 + pooling/归一化配置（麻烦）。适合 Qdrant 生态轻量起步。
- **llama.cpp**：`llama-embedding` 工具 + `llama-server` 的 `/v1/embeddings`(OpenAI 兼容) 与 `/embedding`；参数 `--embedding --pooling {mean,cls,last,rank} --embd-normalize`；`--pooling rank` 还能跑 bge-reranker；bge-m3 GGUF 社区有 **CompendiumLabs/bge-m3-gguf**（HF 卡片直接给 llama.cpp 用法，`llama serve -hf CompendiumLabs/bge-m3-gguf:F16`）；Qwen3-Embedding 有**官方 GGUF**（Qwen/Qwen3-Embedding-0.6B-GGUF，apache-2.0，70k 下载）；只支持 dense（bge-m3 的 sparse/multi-vector 特性不可用，个人库只要 dense 无所谓）。
- ⚠️ **社区实测（r/LocalLLaMA 2025）**：qwen3-embedding 的 GGUF 版与原始 checkpoint 检索表现有出入（NDCG@10 异常），选型前用自己数据集验证。

## 量化对检索质量

- **模型权重**：Q8/int8 安全（与 FP32 余弦相似度 >0.999 量级）；**Q4 以下明显掉点**。Ollama bge-m3 默认即 Q8_0，属安全档。
- **输出向量（存库时压 embedding）**：HF 官方实验（MTEB 15 数据集）——int8 标量量化保留 **~95-97%** 检索性能（mxbai-large 97%、e5-base 94.7%），存储/检索快 4x；二进制 32x 压缩 + rescore ~96%。个人库起步 float32，文档量大再上 int8。

## 推荐落地（P1）

1. **Top1 Ollama**：`ollama pull bge-m3` → 索引脚本 `ollama.embed(model='bge-m3', input=chunks)` → numpy 写 sqlite-vec；在线查询同 API。想要更强中文可换 `qwen3-embedding:0.6b`（639MB/32K ctx）。
2. **Top2 ST+ONNX**：`pip install sentence-transformers[onnx]` → `SentenceTransformer("BAAI/bge-m3", backend="onnx")` → `encode(chunks, batch_size=16)` 进程内批量 → sqlite-vec；零常驻服务、零网络依赖，与 sqlite-vec 同进程集成最自然；日后 reranker 同进程加 bge-reranker 即可。
- 两者 bge-m3 dense 向量同源，**sqlite-vec 侧零改动可无缝切换**：先 Ollama 跑通 P1，性能不满意再切 ONNX。

## 通用部署与验证

此节仅描述项目接口要求，不记录具体电脑的安装路径、硬件或运维过程。

- 项目使用 Ollama 的 `/api/embed` 接口；本地默认地址可为 `http://127.0.0.1:11434`。
- 向量模型以项目配置为准；使用 `qwen3-embedding:0.6b` 时需保持维度与库中配置一致。
- 安装位置、模型缓存位置和启动方式由部署环境决定，不能假定固定盘符。
- 修改模型目录环境变量后，已有进程可能需要重启才能读取新值；不同版本的 GUI 与服务行为应独立验证。
- 验证时检查服务实际监听地址、模型可用性、返回向量维度以及数据库中的模型标识，不能只根据安装成功判断接入完成。
- 涉及模型迁移、进程停止或系统自启的操作由部署维护者单独决定，不属于数据库自动维护职责。
