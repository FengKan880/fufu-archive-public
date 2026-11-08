import hashlib
from fufu_archive.db import KnowledgeBase,initialize_database
from fufu_archive.deterministic_refinement import refine_source_archive_with_chunked_provider

def rec(session="safe", content="ordinary source content"):
 return {"record_type":"session","profile":"winefox","session_id":session,"content_sha256":hashlib.sha256(session.encode()).hexdigest(),"coverage_manifest":{"status":"complete"},"messages":[{"source_message_id":1,"role":"user","content":content},{"source_message_id":2,"role":"assistant","content":"ordinary answer"}]}
class Provider:
 def extract(self,request): return {"items":[{"title":"summary","content":"safe summary"}]}
 def synthesize(self,request): return {"items":[{"title":"synthesis","content":"safe final"}]}
def test_batch_creates_pending_once_and_reports_no_content(tmp_path,monkeypatch):
 archive=tmp_path/"source.json";archive.write_text("{}",encoding="utf-8")
 monkeypatch.setattr("fufu_archive.session_collector.load_source_archive",lambda _: {"records":[rec()]})
 db=tmp_path/"archive.db";initialize_database(db);kb=KnowledgeBase(db)
 first=refine_source_archive_with_chunked_provider(archive,kb,Provider())
 second=refine_source_archive_with_chunked_provider(archive,kb,Provider())
 assert first["records_considered"]==1 and first["candidates_created"]==1
 assert second["records_considered"]==1 and second["candidates_created"]==0
 assert len(kb.list_candidates(status="pending"))==1
 assert "ordinary source content" not in repr(first) and "safe summary" not in repr(first)
def test_batch_blocks_sensitive_source_without_provider_or_write(tmp_path,monkeypatch):
 archive=tmp_path/"source.json";archive.write_text("{}",encoding="utf-8")
 monkeypatch.setattr("fufu_archive.session_collector.load_source_archive",lambda _: {"records":[rec(content="password=not-a-real-secret")]})
 class Never(Provider):
  def extract(self,request): raise AssertionError("must not call provider")
 db=tmp_path/"archive.db";initialize_database(db);kb=KnowledgeBase(db)
 report=refine_source_archive_with_chunked_provider(archive,kb,Never())
 assert report["candidates_created"]==0 and report["blocked"][0]["reason"]=="provider_sensitive_information"
 assert kb.list_candidates(status="pending")==[] and "not-a-real-secret" not in repr(report)
