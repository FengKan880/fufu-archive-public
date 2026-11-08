"""Fufu Archive retrieval evaluation harness.

Builds a scratch KB from eval/corpus, runs labeled queries through the
retrieval pipeline, and reports Recall@k / MRR / NDCG baselines.

Usage:
    python eval/run_eval.py                      # FTS5 baseline (default)
    python eval/run_eval.py --retriever fts5     # explicit
    python eval/run_eval.py --k 5 --k 10         # multiple cutoffs
    python eval/run_eval.py --verbose            # per-query detail

Future retrievers (dense / hybrid) plug in behind the same interface so the
baseline numbers stay comparable.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fufu_archive.db import KnowledgeBase, initialize_database  # noqa: E402
from fufu_archive.embedding import Embedder, OllamaEmbedder  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
CORPUS_DIR = EVAL_DIR / "corpus"
QUERIES_FILE = EVAL_DIR / "queries.json"

# ---------------------------------------------------------------------------
# Dense (Ollama qwen3-embedding + sqlite-vec)
# ---------------------------------------------------------------------------
OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embed"
EMBED_MODEL = "qwen3-embedding:0.6b"
EMBED_DIM = 1024
# Qwen3-Embedding 官方推荐查询侧加 instruct 前缀；文档侧不加。
QUERY_PREFIX_CN = "Instruct: 为这个句子生成表示以用于检索相关文章\nQuery: "
DEFAULT_CUTOFFS = (1, 5, 10)

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a corpus document into (frontmatter dict, body markdown)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("corpus document missing YAML frontmatter")
    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, match.group(2).strip()


def load_corpus() -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in sorted(CORPUS_DIR.glob("*.md")):
        meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        doc_id = meta.get("id")
        if not doc_id:
            raise ValueError(f"{path.name}: frontmatter requires 'id'")
        documents.append(
            {
                "id": doc_id,
                "collection": meta.get("collection", "default"),
                "domain": meta.get("domain"),
                "title": meta.get("title", path.stem),
                "content": body,
            }
        )
    return documents


def load_queries() -> list[dict[str, Any]]:
    data = json.loads(QUERIES_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("queries.json must be a non-empty list")
    for item in data:
        if "query" not in item or "expected" not in item:
            raise ValueError("each query needs 'query' and 'expected'")
    return data


# ---------------------------------------------------------------------------
# Retrievers (one per pipeline strategy; baseline = fts5)
# ---------------------------------------------------------------------------

def build_kb(
    db_path: Path,
    documents: list[dict[str, Any]],
    embedder: Embedder | None = None,
) -> KnowledgeBase:
    initialize_database(db_path, vector_dim=embedder.dim if embedder else EMBED_DIM)
    kb = KnowledgeBase(db_path, embedder=embedder)
    for doc in documents:
        kb.ingest(
            content=doc["content"],
            collection=doc["collection"],
            domain=doc["domain"],
            status="active",
            parent_id=None,
            supersedes=None,
            source=doc.get("title"),
            metadata={"eval_doc_id": doc["id"], "title": doc["title"]},
        )
    if embedder is not None:
        try:
            with kb._connection() as connection:
                chunk_count = int(
                    connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
                )
                vector_count = int(
                    connection.execute("SELECT count(*) FROM chunk_vec").fetchone()[0]
                )
        except sqlite3.Error as error:
            raise RuntimeError("dense index is unavailable") from error
        if vector_count != chunk_count:
            raise RuntimeError(
                f"dense index is incomplete: {vector_count}/{chunk_count} chunks indexed"
            )
    return kb


def retrieve_fts5(
    kb: KnowledgeBase, query: str, limit: int
) -> list[dict[str, Any]]:
    return kb.search(
        query=query, collection=None, domain=None, limit=limit, mode="fts5"
    )


# ---------------------------------------------------------------------------
# Dense/hybrid retrievers use the production KnowledgeBase implementation.
# ---------------------------------------------------------------------------

def retrieve_dense(
    kb: KnowledgeBase,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    # Evaluate the production dense implementation itself.  The public search
    # method intentionally falls back to FTS5, which would hide backend faults
    # and report keyword metrics as dense metrics.
    return kb._search_dense(query, collection=None, domain=None, limit=limit)


def retrieve_hybrid(
    kb: KnowledgeBase,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    return kb._search_hybrid(query, collection=None, domain=None, limit=limit)


RETRIEVERS: dict[str, Callable[[KnowledgeBase, str, int], list[dict[str, Any]]]] = {
    "fts5": retrieve_fts5,
    "dense": retrieve_dense,
    "dense-prefix": retrieve_dense,
    "hybrid": retrieve_hybrid,
}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    query: str
    expected: set[str]
    hits: list[str]  # retrieved doc ids in rank order
    domain: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)


def _resolve_doc_ids(
    kb: KnowledgeBase, results: list[dict[str, Any]]
) -> list[str]:
    """Map retrieved rows back to eval doc ids using the metadata table."""
    doc_ids: list[str] = []
    with kb._connection() as connection:
        for row in results:
            chunk_id = row["chunk_id"]
            found = connection.execute(
                """
                SELECT value_json FROM metadata
                WHERE document_id = (
                    SELECT document_id FROM chunks WHERE id = ?
                ) AND key = 'eval_doc_id'
                """,
                (chunk_id,),
            ).fetchone()
            if found:
                doc_ids.append(json.loads(found["value_json"]))
    return doc_ids


def ndcg_at(ranking: list[str], expected: set[str], k: int) -> float:
    """Binary-relevance NDCG@k: relevant doc = 1, others 0."""
    ideal = min(len(expected), k)
    if ideal == 0:
        return 0.0

    def dcg(items: list[str]) -> float:
        score = 0.0
        for rank, doc_id in enumerate(items[:k], start=1):
            if doc_id in expected:
                score += 1.0 / math.log2(rank + 1)
        return score

    ideal_items = list(expected)[:k]
    idcg = dcg(ideal_items)
    return dcg(ranking) / idcg if idcg > 0 else 0.0


def evaluate(
    kb: KnowledgeBase,
    queries: list[dict[str, Any]],
    retriever: Callable[[KnowledgeBase, str, int], list[dict[str, Any]]],
    cutoffs: list[int],
) -> list[QueryResult]:
    results: list[QueryResult] = []
    for item in queries:
        query = item["query"]
        expected = set(item["expected"])
        raw = retriever(kb, query, max(cutoffs))
        hits = _resolve_doc_ids(kb, raw)
        qr = QueryResult(
            query=query,
            expected=expected,
            hits=hits,
            domain=item.get("domain"),
        )
        for k in cutoffs:
            retrieved = hits[:k]
            relevant = [d for d in retrieved if d in expected]
            recall = len(relevant) / len(expected) if expected else 0.0
            mrr = 0.0
            for rank, doc_id in enumerate(retrieved, start=1):
                if doc_id in expected:
                    mrr = 1.0 / rank
                    break
            qr.metrics[f"recall@{k}"] = recall
            qr.metrics[f"mrr@{k}"] = mrr
            qr.metrics[f"ndcg@{k}"] = ndcg_at(retrieved, expected, k)
        results.append(qr)
    return results


def _fmt(value: float) -> str:
    return f"{value:.3f}"


def summarize(
    results: list[QueryResult], cutoffs: list[int]
) -> dict[str, float]:
    summary: dict[str, float] = {}
    for k in cutoffs:
        for metric in ("recall", "mrr", "ndcg"):
            key = f"{metric}@{k}"
            values = [qr.metrics[key] for qr in results]
            summary[key] = sum(values) / len(values) if values else 0.0
    return summary


def print_report(
    results: list[QueryResult],
    summary: dict[str, float],
    cutoffs: list[int],
    verbose: bool,
) -> None:
    header = "metric".ljust(10) + "".join(str(k).rjust(10) for k in cutoffs)
    print(header)
    print("-" * len(header))
    for metric in ("recall", "mrr", "ndcg"):
        line = metric.ljust(10)
        for k in cutoffs:
            line += summary[f"{metric}@{k}"].__format__(".3f").rjust(10)
        print(line)

    if verbose:
        detail_k = max(cutoffs)
        print("\n--- per-query detail ---")
        for qr in results:
            flag = "OK " if qr.hits and qr.hits[0] in qr.expected else "MISS"
            print(
                f"{flag} [{qr.domain or '-'}] {qr.query!r} "
                f"expected={sorted(qr.expected)} "
                f"top={qr.hits[:5]} "
                f"recall@{detail_k}={qr.metrics[f'recall@{detail_k}']:.2f}"
            )


def _positive_cutoff(value: str) -> int:
    try:
        cutoff = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("cutoff must be an integer") from error
    if cutoff < 1:
        raise argparse.ArgumentTypeError("cutoff must be positive")
    return cutoff


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retriever",
        choices=sorted(RETRIEVERS),
        default="fts5",
        help="retrieval strategy to evaluate (baseline: fts5)",
    )
    parser.add_argument(
        "--k", action="append", type=_positive_cutoff,
        help="cutoffs (repeatable, default: 1 5 10)",
    )
    parser.add_argument("--verbose", action="store_true", help="per-query detail")
    args = parser.parse_args(argv)
    args.k = list(dict.fromkeys(args.k or DEFAULT_CUTOFFS))
    return args


def main() -> int:
    args = parse_args()

    documents = load_corpus()
    queries = load_queries()
    print(
        f"corpus: {len(documents)} documents, "
        f"queries: {len(queries)}, retriever: {args.retriever}"
    )

    with tempfile.TemporaryDirectory(prefix="fufu-eval-") as tmp:
        db_path = Path(tmp) / "eval.db"
        embedder: Embedder | None = None
        if args.retriever in ("dense", "dense-prefix", "hybrid"):
            query_prefix = None if args.retriever == "dense" else QUERY_PREFIX_CN
            embedder = OllamaEmbedder(
                base_url=OLLAMA_EMBED_URL.removesuffix("/api/embed"),
                model=EMBED_MODEL,
                query_prefix=query_prefix,
                dim=EMBED_DIM,
                timeout=180,
            )
            print(f"building dense index via {EMBED_MODEL} ...")
        kb = build_kb(db_path, documents, embedder=embedder)
        results = evaluate(kb, queries, RETRIEVERS[args.retriever], args.k)
        summary = summarize(results, args.k)
        print_report(results, summary, args.k, args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
