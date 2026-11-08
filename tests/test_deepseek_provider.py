import json,pytest
from fufu_archive.deepseek_provider import DeepSeekProviderError,DeepSeekV4FlashProvider
from fufu_archive.semantic_provider import SemanticExtractionRequest
def req(text="hello"): return SemanticExtractionRequest("test","s","h",(1,),text)
def response(content,finish=None): return {"choices":[{"finish_reason":finish,"message":{"content":content}}]}
def test_payload_and_synthesis_are_distinct():
 seen=[]
 def tr(e,h,p,t): seen.append(p);return response('{"items":[]}')
 p=DeepSeekV4FlashProvider(api_key="test-key",transport=tr);p.extract(req());p.synthesize(req())
 assert all(x["thinking"]=={"type":"enabled"} and x["reasoning_effort"]=="low" for x in seen)
 assert "最终汇总器" in seen[1]["messages"][0]["content"]
def test_fenced_only_json_and_local_scope():
 p=DeepSeekV4FlashProvider(api_key="x",transport=lambda *a:response("```json\n{\"items\":[{\"title\":\"t\",\"summary\":\"s\"}]}\n```"))
 item=p.extract(req())["items"][0];assert item["evidence"]==[1] and item["evidence_scope"]=="input_range"
@pytest.mark.parametrize("content,code",[("prefix {\"items\":[]}","provider_invalid_response"),("{\"items\":[]}","provider_truncated")])
def test_rejects_unframed_json_and_truncation(content,code):
 finish="length" if code=="provider_truncated" else None
 with pytest.raises(DeepSeekProviderError) as e: DeepSeekV4FlashProvider(api_key="x",transport=lambda *a:response(content,finish)).extract(req())
 assert e.value.reason_code==code
def test_sensitive_input_or_output_and_item_limit_are_rejected():
 with pytest.raises(DeepSeekProviderError) as e: DeepSeekV4FlashProvider(api_key="x",transport=lambda *a:response('{"items":[]}')).extract(req("password=not-a-real-secret"))
 assert e.value.reason_code=="provider_sensitive_information"
 with pytest.raises(DeepSeekProviderError) as e: DeepSeekV4FlashProvider(api_key="x",transport=lambda *a:response('{"items":[{"title":"t","summary":"api_key=not-a-real-secret"}]}')).extract(req())
 assert e.value.reason_code=="provider_sensitive_information"
 many=json.dumps({"items":[{"title":"t","summary":"s"}]*21})
 with pytest.raises(DeepSeekProviderError) as e: DeepSeekV4FlashProvider(api_key="x",transport=lambda *a:response(many)).extract(req())
 assert e.value.reason_code=="batch_limit_exceeded"
def test_no_key_fails_closed():
 with pytest.raises(DeepSeekProviderError): DeepSeekV4FlashProvider(api_key="",transport=lambda *a:{}).extract(req())
