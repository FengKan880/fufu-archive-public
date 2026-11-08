"""Bounded DeepSeek V4 Flash provider for untrusted semantic summaries."""
from __future__ import annotations
import json, os, re, urllib.error, urllib.request
from collections.abc import Mapping
from typing import Any
from .semantic_provider import SemanticExtractionRequest

_SECRET=re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|bearer)\s*[=:]\s*[^\s,;]+")
_FENCE=re.compile(r"^```(?:json)?\s*(.*?)\s*```$",re.I|re.S)
class DeepSeekProviderError(RuntimeError):
 def __init__(self,code:str,detail:str="") -> None: self.reason_code=code; super().__init__(f"{code}: {detail[:240] or 'DeepSeek provider failed'}")
def _key(value:str|None)->str:
 key=(os.getenv("DEEPSEEK_API_KEY","") if value is None else value).strip()
 if not key: raise DeepSeekProviderError("credential_missing","DEEPSEEK_API_KEY is not set")
 return key
def _json(text:str)->Any:
 candidate=text.strip(); match=_FENCE.fullmatch(candidate)
 if match: candidate=match.group(1).strip()
 return json.loads(candidate)
class DeepSeekV4FlashProvider:
 def __init__(self,*,api_key:str|None=None,transport=None,endpoint="https://api.deepseek.com/chat/completions",model="deepseek-v4-flash",timeout_seconds:float=60.0,max_items:int=20,max_output_chars:int=20000):
  self.api_key=api_key;self.transport=transport or self._transport;self.endpoint=endpoint;self.model=model;self.timeout_seconds=timeout_seconds;self.max_items=max_items;self.max_output_chars=max_output_chars
 def _transport(self,endpoint,headers,payload,timeout):
  req=urllib.request.Request(endpoint,data=json.dumps(payload).encode(),headers={**headers,"Content-Type":"application/json"},method="POST")
  try:
   with urllib.request.urlopen(req,timeout=timeout) as response:return json.loads(response.read().decode())
  except urllib.error.HTTPError as e: raise DeepSeekProviderError("quota_exhausted" if e.code in (402,429) else "provider_http_error",f"upstream HTTP {e.code}") from e
  except (urllib.error.URLError,TimeoutError,OSError) as e: raise DeepSeekProviderError("provider_timeout" if isinstance(e,TimeoutError) else "provider_http_error",type(e).__name__) from e
 def _call(self,request,instructions):
  if _SECRET.search(request.transcript): raise DeepSeekProviderError("provider_sensitive_information","source contains sensitive values")
  payload={"model":self.model,"messages":[{"role":"system","content":instructions},{"role":"user","content":request.transcript}],"response_format":{"type":"json_object"},"thinking":{"type":"enabled"},"reasoning_effort":"low","stream":False}
  response=self.transport(self.endpoint,{"Authorization":f"Bearer {_key(self.api_key)}"},payload,self.timeout_seconds)
  try:
   if response.get("choices",[{}])[0].get("finish_reason")=="length": raise DeepSeekProviderError("provider_truncated","response hit length limit")
   text=response["choices"][0]["message"]["content"]; result=_json(text)
  except DeepSeekProviderError: raise
  except (KeyError,IndexError,TypeError,json.JSONDecodeError,UnicodeError) as e: raise DeepSeekProviderError("provider_invalid_response","content is not one JSON object") from e
  if not isinstance(result,Mapping) or not isinstance(result.get("items"),list): raise DeepSeekProviderError("provider_invalid_response","JSON output has no items array")
  if len(result["items"])>self.max_items: raise DeepSeekProviderError("batch_limit_exceeded","item count exceeds configured limit")
  items=[]; total=0
  for item in result["items"]:
   if not isinstance(item,Mapping) or not isinstance(item.get("title"),str) or not isinstance(item.get("summary"),str): raise DeepSeekProviderError("provider_invalid_response","each item needs title and summary")
   title,summary=item["title"].strip(),item["summary"].strip(); total+=len(title)+len(summary)
   if not title or not summary or _SECRET.search(title) or _SECRET.search(summary): raise DeepSeekProviderError("provider_sensitive_information","unsafe or empty summary")
   if total>self.max_output_chars: raise DeepSeekProviderError("batch_limit_exceeded","output character budget exceeded")
   clean={"title":title,"content":summary,"evidence":list(request.source_message_ids),"evidence_scope":"input_range"}
   for key in ("kind","confidence"):
    if isinstance(item.get(key),str) and item[key].strip(): clean[key]=item[key].strip()
   items.append(clean)
  return {"items":items}
 def extract(self,request):
  return self._call(request,"你是酒狐档案馆的局部会话缩写器。仅输出JSON对象 {\"items\":[{\"title\":\"...\",\"summary\":\"...\"}]}。只保留可核查事实、决定、流程、验证和待办；不要输出来源ID、状态、密钥或解释文字。")
 def synthesize(self,request):
  return self._call(request,"你是酒狐档案馆的最终汇总器。输入是按时间顺序的局部摘要。只输出JSON对象 {\"items\":[{\"title\":\"...\",\"summary\":\"...\"}]}。优先最后一次已验证结论，明确区分计划、尝试、失败与完成；合并重复、标记未解决冲突；不要输出来源ID、状态、密钥或解释文字。")
__all__=["DeepSeekV4FlashProvider","DeepSeekProviderError"]
