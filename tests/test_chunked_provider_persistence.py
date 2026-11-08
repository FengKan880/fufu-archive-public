import hashlib
from fufu_archive.db import KnowledgeBase, initialize_database
from fufu_archive.deterministic_refinement import persist_chunked_provider_candidate

def record():
 return {"profile":"winefox","session_id":"s1","content_sha256":hashlib.sha256(b"source").hexdigest(),"coverage_manifest":{"status":"complete"},"messages":[{"source_message_id":1,"role":"user","content":"request"},{"source_message_id":2,"role":"assistant","content":"answer"}]}
class Good:
 def extract(self,request): return {"items":[{"title":"local","content":"fact","evidence":list(request.source_message_ids)}]}
 def synthesize(self,request): return {"items":[{"title":"final","content":"verified"}]}
class Bad(Good):
 def extract(self,request): raise TimeoutError("offline")
def test_chunked_candidate_is_pending_not_searchable(tmp_path):
 db=tmp_path/"archive.db";initialize_database(db);kb=KnowledgeBase(db)
 candidate=persist_chunked_provider_candidate(kb,record(),Good())
 assert candidate is not None and candidate["status"]=="pending"
 assert kb.list_candidates(status="pending")[0]["candidate_id"]==candidate["candidate_id"]
 assert kb.search(query="verified", collection=None, domain=None, limit=10, mode="fts5")==[]
 stored=kb.get_candidate(candidate["candidate_id"])["metadata"]["refinement"]
 assert stored["synthesis"]=="not-needed" and stored["items"][0]["evidence_scope"]=="input_range"
def test_blocked_chunk_run_writes_nothing(tmp_path):
 db=tmp_path/"archive.db";initialize_database(db);kb=KnowledgeBase(db)
 assert persist_chunked_provider_candidate(kb,record(),Bad()) is None
 assert kb.list_candidates(status="pending")==[]
