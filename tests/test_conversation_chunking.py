import pytest
from fufu_archive.conversation_chunking import chunk_conversation, normalize_messages, request_snapshot_messages

def test_no_overlap_and_complete_turns():
 messages=[{"source_message_id":i,"role":r,"content":"x"*10} for i,r in [(1,"user"),(2,"user"),(3,"assistant"),(4,"assistant"),(5,"user"),(6,"assistant")]]
 chunks=chunk_conversation(messages,max_chars=100)
 assert [c["source_message_ids"] for c in chunks]==[[1,2,3,4],[5,6]]
 assert not set(chunks[0]["source_message_ids"]) & set(chunks[1]["source_message_ids"])
def test_list_content_and_invalid_source_identity():
 assert "甲\n乙" in chunk_conversation([{"source_message_id":1,"role":"user","content":[{"type":"input_text","text":"甲"},{"type":"output_text","text":"乙"}]}])[0]["transcript"]
 with pytest.raises(ValueError): normalize_messages([{"source_message_id":1,"role":"user","content":"a"},{"source_message_id":1,"role":"assistant","content":"b"}])
 with pytest.raises(ValueError): normalize_messages([{"role":"user","content":"a"}])
def test_empty_and_soft_limit_never_splits_turn():
 assert chunk_conversation([])==[]
 with pytest.raises(ValueError): chunk_conversation([],max_chars=0)
 chunk=chunk_conversation([{"source_message_id":1,"role":"user","content":"x"*100},{"source_message_id":2,"role":"assistant","content":"x"*100}],max_chars=1)
 assert chunk[0]["source_message_ids"]==[1,2]

def test_request_snapshot_adapter_keeps_position_not_fake_archive_ids():
 records=request_snapshot_messages({"input":[{"role":"system","content":"ignore"},{"role":"user","content":[{"type":"input_text","text":"u"}]},{"role":"assistant","content":"a"}]})
 assert records==[{"snapshot_position":1,"role":"user","content":"u"},{"snapshot_position":2,"role":"assistant","content":"a"}]
 with pytest.raises(ValueError): chunk_conversation(records)
