"""비밀 검사 후 제한된 SSE 응답을 공통 이벤트로 변환한다."""
from copy import deepcopy
import json

from .normalized import GenerationError, StreamEvent


def decode_stream(data, protocol):
    try:
        records = []
        for block in data.decode("utf-8", errors="strict").replace("\r\n", "\n").split("\n\n"):
            value = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            if value and value != "[DONE]": records.append(json.loads(value))
        if not records or len(records) > 20000: raise ValueError()
        events = [StreamEvent(event="response_start")]
        document, terminal = {}, False
        blocks, calls = {}, {}
        text = ""
        for item in records:
            kind = item.get("type", item.get("event_type"))
            if kind == "error" or item.get("error"): raise GenerationError("PROVIDER_UNAVAILABLE")
            delta = None
            if protocol == "responses":
                if kind in {"response.completed", "response.incomplete", "response.failed"}:
                    document, terminal = item["response"], True
                if kind == "response.output_text.delta": delta = item["delta"]
                if kind == "response.output_item.added" and item.get("item", {}).get("type") == "function_call":
                    call = item["item"]
                    events.append(StreamEvent(event="tool_call_start", data={"call_id":call["call_id"], "tool_name":call["name"]}))
                if kind == "response.function_call_arguments.delta": events.append(StreamEvent(event="tool_call_delta", data={"call_id":item.get("item_id"), "arguments_delta":item["delta"]}))
                if kind == "response.function_call_arguments.done": events.append(StreamEvent(event="tool_call_end", data={"call_id":item.get("item_id")}))
            elif protocol == "messages":
                if kind == "message_start": document = deepcopy(item["message"])
                if kind == "content_block_start":
                    block = deepcopy(item["content_block"])
                    blocks[item["index"]] = block
                    if block["type"] == "tool_use":
                        block["_arguments"] = ""
                        events.append(StreamEvent(event="tool_call_start", data={"call_id":block["id"], "tool_name":block["name"]}))
                if kind == "content_block_delta":
                    block, value = blocks[item["index"]], item["delta"]
                    if value["type"] == "text_delta":
                        delta = value["text"]
                        block["text"] = block.get("text", "") + delta
                    elif value["type"] == "input_json_delta":
                        block["_arguments"] += value["partial_json"]
                        events.append(StreamEvent(event="tool_call_delta", data={"call_id":block["id"], "arguments_delta":value["partial_json"]}))
                    elif value["type"] == "thinking_delta": block["thinking"] = block.get("thinking", "") + value["thinking"]
                    elif value["type"] == "signature_delta": block["signature"] = block.get("signature", "") + value["signature"]
                if kind == "content_block_stop":
                    block = blocks[item["index"]]
                    if "_arguments" in block:
                        raw = block.pop("_arguments")
                        if raw: block["input"] = json.loads(raw)
                        events.append(StreamEvent(event="tool_call_end", data={"call_id":block["id"]}))
                if kind == "message_delta":
                    document.update(item.get("delta", {}))
                    document.setdefault("usage", {}).update(item.get("usage", {}))
                if kind == "message_stop": terminal = True
                document["content"] = [blocks[k] for k in sorted(blocks)]
            elif protocol == "generate_content":
                document.update({k:v for k,v in item.items() if k != "candidates"})
                for candidate in item.get("candidates", []):
                    if candidate.get("index", 0) != 0: continue
                    for part in candidate.get("content", {}).get("parts", []):
                        blocks[len(blocks)] = deepcopy(part)
                        if "text" in part and not part.get("thought"): delta = part["text"]
                    if candidate.get("finishReason"):
                        document["candidates"] = [{"finishReason":candidate["finishReason"],"content":{"parts":list(blocks.values())}}]
                        terminal = True
            elif protocol == "interactions":
                if kind == "interaction.created": document.update(item["interaction"])
                if kind == "step.start": blocks[item["index"]] = deepcopy(item["step"])
                if kind == "step.delta":
                    block, value = blocks[item["index"]], item["delta"]
                    if value.get("type") == "text":
                        delta = value["text"]
                        block.setdefault("content", []).append({"type":"text","text":delta})
                    elif value.get("type") == "function_call": block.update(value)
                    elif value.get("type") == "thought_signature": block["signature"] = value.get("signature")
                    if item.get("metadata", {}).get("total_usage"): document["usage"] = item["metadata"]["total_usage"]
                if kind in {"interaction.completed", "interaction.incomplete", "interaction.failed"}:
                    document.update(item["interaction"])
                    terminal = True
                document["steps"] = list(blocks.values()) or document.get("steps", [])
            elif protocol == "chat":
                for key in ("id", "model", "system_fingerprint", "usage"):
                    if key in item and item[key] is not None: document[key] = item[key]
                for choice in item.get("choices", []):
                    if choice.get("index", 0) != 0: continue
                    value = choice.get("delta", {})
                    if value.get("content"):
                        delta = value["content"]
                        text += delta
                    if value.get("reasoning_content"): document["_reasoning"] = document.get("_reasoning", "") + value["reasoning_content"]
                    for part in value.get("tool_calls", []):
                        index = part["index"]
                        if index not in calls:
                            calls[index] = {"id":part["id"],"type":"function","function":{"name":part["function"]["name"],"arguments":""}}
                            events.append(StreamEvent(event="tool_call_start", data={"call_id":part["id"],"tool_name":part["function"]["name"]}))
                        fragment = part.get("function", {}).get("arguments", "")
                        calls[index]["function"]["arguments"] += fragment
                        if fragment: events.append(StreamEvent(event="tool_call_delta", data={"call_id":calls[index]["id"],"arguments_delta":fragment}))
                    if choice.get("finish_reason"):
                        document["_finish"], terminal = choice["finish_reason"], True
            else: raise GenerationError("UNSUPPORTED_CAPABILITY")
            if delta is not None: events.append(StreamEvent(event="text_delta", data={"text":delta}))
        if not terminal: raise GenerationError("INCOMPLETE_RESPONSE")
        if protocol == "chat":
            message = {"content":text,"tool_calls":list(calls.values())}
            if "_reasoning" in document: message["reasoning_content"] = document.pop("_reasoning")
            document["choices"] = [{"message":message,"finish_reason":document.pop("_finish")}]
            events.extend(StreamEvent(event="tool_call_end", data={"call_id":c["id"]}) for c in calls.values())
        return document, events
    except GenerationError: raise
    except (ValueError, UnicodeError, KeyError, TypeError, IndexError): raise GenerationError("MALFORMED_RESPONSE") from None
