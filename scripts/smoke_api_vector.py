"""API smoke: create_app with a real Ollama embedder, ingest + dense search."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from fufu_archive.config import Settings
from fufu_archive.main import create_app

TOKEN = "smoke-token-abc"


def main() -> None:
    settings = Settings(
        db_path=Path("data") / "smoke_api.db",
        host="127.0.0.1",
        port=8000,
        api_token=TOKEN,
        embed_url="http://127.0.0.1:11434",
        embed_model="qwen3-embedding:0.6b",
    )
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        ingest = client.post(
            "/kb/ingest",
            headers=headers,
            json={
                "content": "酒狐档案馆是一个面向 AI Agent 的外置长期记忆与知识库系统，"
                           "使用 SQLite 存储、向量混合检索中文知识。",
                "collection": "notes",
                "domain": "ops",
            },
        )
        print("ingest:", ingest.status_code, ingest.json())
        for mode in ("dense", "hybrid", "fts5"):
            resp = client.post(
                "/kb/search",
                headers=headers,
                json={"query": "AI Agent 的长期记忆系统", "collection": "notes", "mode": mode},
            )
            body = resp.json()
            top = body["results"][0]["content"][:30] if body["results"] else "(空)"
            print(f"search[{mode}]:", resp.status_code, "->", top)
    print("API-SMOKE-PASS")


if __name__ == "__main__":
    main()
