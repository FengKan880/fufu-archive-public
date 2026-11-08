"""One-shot chunked semantic refinement into pending candidates only."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
from .config import Settings
from .db import KnowledgeBase
from .provider_factory import build_semantic_provider
from .deterministic_refinement import refine_source_archive_with_chunked_provider

def main(argv: list[str] | None=None)->int:
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument("--source-archive",required=True,type=Path);p.add_argument("--db",required=True,type=Path)
 p.add_argument("--profile");p.add_argument("--limit",type=int);p.add_argument("--max-chars",type=int,default=30_000)
 args=p.parse_args(argv)
 if not args.source_archive.is_file(): p.error("source archive does not exist")
 if not args.db.is_file(): p.error("archive database does not exist")
 provider=build_semantic_provider(Settings.from_env())
 if provider is None: p.error("semantic provider is disabled; set FUFU_ARCHIVE_SEMANTIC_PROVIDER=deepseek explicitly")
 report=refine_source_archive_with_chunked_provider(args.source_archive,KnowledgeBase(args.db),provider,profile=args.profile,limit=args.limit,max_chars=args.max_chars)
 json.dump(report,sys.stdout,ensure_ascii=False);sys.stdout.write("\n");return 0
if __name__=="__main__": raise SystemExit(main())
