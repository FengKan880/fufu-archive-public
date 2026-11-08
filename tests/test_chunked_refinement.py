from fufu_archive.chunked_refinement import summarize_session_in_chunks
class Provider:
 def __init__(self,fail=False): self.calls=[];self.fail=fail
 def extract(self,request):
  self.calls.append(("extract",request))
  if self.fail and len(self.calls)==1: raise TimeoutError("synthetic")
  return {"items":[{"title":"local","content":"fact","evidence":list(request.source_message_ids)}]}
 def synthesize(self,request): self.calls.append(("synthesize",request));return {"items":[{"title":"final","content":"resolved"}]}
def messages(): return [{"source_message_id":i,"role":r,"content":"x"*20} for i,r in [(1,"user"),(2,"assistant"),(3,"user"),(4,"assistant")]]
def test_persistent_chunk_failure_is_retried_then_blocked_and_not_exposed():
 class PersistentFailure(Provider):
  def extract(self,request):
   self.calls.append(("extract",request))
   if 1 in request.source_message_ids: raise TimeoutError("persistent")
   return {"items":[]}
 p=PersistentFailure();out=summarize_session_in_chunks(messages(),p,max_chars=70)
 assert out["status"]=="blocked" and out["items"]==[] and out["synthesis"]=="blocked"
 assert out["chunks"][0]["attempts"]==2
def test_transient_chunk_failure_recovers_once():
 class Transient(Provider):
  def extract(self,request):
   self.calls.append(("extract",request))
   if len(self.calls)==1: raise TimeoutError("first")
   return {"items":[]}
 out=summarize_session_in_chunks(messages(),Transient(),max_chars=70)
 assert out["status"]=="pending"
def test_synthesis_uses_dedicated_method_and_preserves_scope():
 p=Provider();out=summarize_session_in_chunks(messages(),p,max_chars=70)
 assert out["status"]=="pending" and out["synthesis"]=="completed"
 assert p.calls[-1][0]=="synthesize" and out["items"][0]["evidence"]==[1,2,3,4]
 assert out["items"][0]["evidence_scope"]=="session_input_range"
def test_empty_and_call_budget():
 assert summarize_session_in_chunks([],Provider())["items"]==[]
 assert summarize_session_in_chunks(messages(),Provider(),max_chars=1,max_calls=1)["status"]=="blocked"
