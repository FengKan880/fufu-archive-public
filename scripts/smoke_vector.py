"""End-to-end smoke: production code + real Ollama qwen3-embedding.

Ingests two docs, searches with dense and hybrid, prints results.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fufu_archive.db import KnowledgeBase, initialize_database
from fufu_archive.embedding import OllamaEmbedder


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fufu-smoke-") as tmp:
        db_path = Path(tmp) / "smoke.db"
        initialize_database(db_path)
        embedder = OllamaEmbedder(
            base_url="http://127.0.0.1:11434",
            model="qwen3-embedding:0.6b",
        )
        kb = KnowledgeBase(db_path, embedder=embedder)

        docs = [
            "厂里失去厂用电后，发电机组会触发紧急停机保护，需要立即启动备用电源，"
            "检查汽轮机振动与轴承温度，维持汽包水位稳定。",
            "VRChat 地图里的视频播放依赖 yt-dlp 请求，机场代理能覆盖这些流量，"
            "中途切节点会导致 Photon 连接断开，必须重启游戏。",
        ]
        for text in docs:
            kb.ingest(
                content=text,
                collection="world_knowledge",
                domain="vrc",
                status="active",
                parent_id=None,
                supersedes=None,
                source="smoke",
                metadata={},
            )

        queries = [
            ("厂里突然断电了对机组有什么影响", "plant"),
            ("VRChat 视频加载不出来怎么解决", "vrc"),
        ]
        for query, tag in queries:
            print(f"\n=== [{tag}] 查询: {query} ===")
            for mode in ("fts5", "dense", "hybrid"):
                results = kb.search(
                    query=query,
                    collection=None,
                    domain=None,
                    limit=3,
                    mode=mode,
                )
                top = results[0]["content"][:40] if results else "(无结果)"
                print(f"  {mode:8s} -> {top}")
    print("\nSMOKE-PASS")


if __name__ == "__main__":
    main()
