"""기존 HTTP·예산·외부 전송 경계의 단일 어댑터 등록소. 도구 실행과 정본 쓰기는 H-TRSA가 담당한다."""
from __future__ import annotations

from copy import deepcopy
import asyncio
from hashlib import sha256
import json
import time
from urllib.parse import quote

import httpx

from ..schemas import utc_now
from .normalized import (CapabilityEvidence, CapabilityStatus as CS, DiscoveredModel,
                         GenerationError, GenerationMessage, GenerationResult,
                         GenerationUsage, ReasoningPolicy as RP, StreamEvent)


DEFINITIONS = {
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "credential": "OPENAI_API_KEY", "protocols": ["responses"]},
    "anthropic": {"name": "Anthropic", "base_url": "https://api.anthropic.com", "credential": "ANTHROPIC_API_KEY", "protocols": ["messages"]},
    "google_gemini": {"name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com", "credential": "GEMINI_API_KEY", "protocols": ["interactions", "generate_content"]},
    "xai": {"name": "xAI", "base_url": "https://api.x.ai", "credential": "XAI_API_KEY", "protocols": ["responses", "chat"]},
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "credential": "DEEPSEEK_API_KEY", "protocols": ["chat"]},
    "mistral": {"name": "Mistral", "base_url": "https://api.mistral.ai", "credential": "MISTRAL_API_KEY", "protocols": ["chat"]},
    "openai_compatible": {"name": "OpenAI-compatible · 개별 서버 검증 필요", "base_url": "", "credential": None, "protocols": ["chat", "responses"]},
}
VERSION = "1.0.0"
ANTHROPIC_VERSION = "2023-06-01"


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_json(value):
    def invalid_constant(_): raise ValueError("유효하지 않은 JSON 수치")
    return json.loads(value, parse_constant=invalid_constant)


def integer(value):
    return value if type(value) is int and value >= 0 else None


def arguments(value):
    if isinstance(value, str):
        value = parse_json(value)
    if not isinstance(value, dict):
        raise GenerationError("MALFORMED_RESPONSE")
    return value


def capability(profile, name):
    item = profile.capabilities.get(name)
    return item.status if item else CS.UNKNOWN


def require(profile, name, request):
    status = capability(profile, name)
    if status != CS.SUPPORTED and not (status == CS.UNKNOWN and request.probe_capability == name):
        raise GenerationError("UNSUPPORTED_CAPABILITY")


def normalized_error(status, body):
    # 알려진 오류 코드만 읽고 제공사 메시지는 노출하지 않는다.
    error = body.get("error", {}) if isinstance(body, dict) else {}
    code = str(error.get("code", error.get("type", ""))) if isinstance(error, dict) else ""
    message = str(error.get('message', '')) if isinstance(error, dict) else str(error)
    if status >= 400 and (code.lower() in {'oom', 'out_of_memory', 'resource_exhausted', 'cuda_out_of_memory'}
                         or any(term in message.lower() for term in ('out of memory', 'insufficient memory', 'cuda oom'))):
        return 'RESOURCE_EXHAUSTED'
    if any(word in code.lower() for word in ("quota", "spend", "billing", "credit")):
        return "SPEND_LIMIT"
    if "context" in code.lower(): return "CONTEXT_LIMIT"
    if "model_not_accessible" in code.lower(): return "MODEL_NOT_ACCESSIBLE"
    return {400: "INVALID_PARAMETER", 401: "AUTHENTICATION_ERROR", 403: "AUTHORIZATION_ERROR", 404: "MODEL_NOT_FOUND", 408: "TIMEOUT", 413: "CONTEXT_LIMIT", 429: "RATE_LIMIT"}.get(status, "PROVIDER_UNAVAILABLE" if status >= 500 else "UNKNOWN_PROVIDER_ERROR")


def parse_usage(adapter, protocol, document):
    u = document.get("usageMetadata") if protocol == "generate_content" else document.get("usage")
    if not isinstance(u, dict): return GenerationUsage()
    if protocol == "generate_content":
        inp, visible, thoughts = integer(u.get("promptTokenCount")), integer(u.get("candidatesTokenCount")), integer(u.get("thoughtsTokenCount"))
        out = visible + thoughts if visible is not None and thoughts is not None else visible if thoughts is None and "thoughtsTokenCount" not in u else None
        cached, write, total = integer(u.get("cachedContentTokenCount")), None, integer(u.get("totalTokenCount"))
    elif protocol == "interactions":
        inp, visible, thoughts = integer(u.get("total_input_tokens")), integer(u.get("total_output_tokens")), integer(u.get("total_thought_tokens"))
        out = visible + thoughts if visible is not None and thoughts is not None else visible if "total_thought_tokens" not in u else None
        cached, write, total = integer(u.get("total_cached_tokens")), None, integer(u.get("total_tokens"))
    elif adapter == "anthropic":
        base, cached, write = integer(u.get("input_tokens")), integer(u.get("cache_read_input_tokens")), integer(u.get("cache_creation_input_tokens"))
        inp = base + (cached or 0) + (write or 0) if base is not None else None
        out, thoughts, total = integer(u.get("output_tokens")), integer((u.get("output_tokens_details") or {}).get("thinking_tokens")), None
    else:
        inp, out = integer(u.get("input_tokens", u.get("prompt_tokens"))), integer(u.get("output_tokens", u.get("completion_tokens")))
        cached = integer((u.get("input_tokens_details", u.get("prompt_tokens_details")) or {}).get("cached_tokens", u.get("prompt_cache_hit_tokens")))
        write = integer(u.get("cache_write_tokens"))
        thoughts = integer((u.get("output_tokens_details", u.get("completion_tokens_details")) or {}).get("reasoning_tokens"))
        total = integer(u.get("total_tokens"))
    if total is None and inp is not None and out is not None: total = inp + out
    return GenerationUsage(input_tokens=inp, output_tokens=out, cached_input_tokens=cached, cache_write_tokens=write,
        reasoning_tokens=thoughts, total_tokens=total, provider_usage_raw_ref="sha256:" + sha256(compact(u).encode("utf-8", errors="strict")).hexdigest())


class ProviderAdapter:
    version = VERSION
    def __init__(self, adapter_id):
        self.adapter_id = adapter_id

    def protocol(self, profile, connection=None):
        allowed = DEFINITIONS[self.adapter_id]["protocols"]
        requested = profile.protocol
        if requested == "auto": return allowed[0]
        if requested not in allowed: raise GenerationError("UNSUPPORTED_CAPABILITY")
        return requested

    def validate_profile(self, profile, connection):
        protocol = self.protocol(profile, connection)
        if not connection.enabled: raise GenerationError("AUTHORIZATION_ERROR")
        if capability(profile, "text") == CS.UNSUPPORTED: raise GenerationError("UNSUPPORTED_CAPABILITY")
        if profile.max_output_tokens is not None and profile.output_limit > profile.max_output_tokens: raise GenerationError("OUTPUT_LIMIT")
        if profile.max_retries != 0: raise GenerationError("UNSUPPORTED_CAPABILITY")
        return {"adapter": self.adapter_id, "adapter_version": self.version, "protocol": protocol, "retry_policy": "NONE_AFTER_DISPATCH"}

    def reasoning(self, request, profile, protocol):
        level = request.reasoning_policy
        meta = {"requested_reasoning": level.value, "effective_reasoning": None, "mapping_reason": "AUTO는 제공사 기본값을 사용하며 추정한 수준을 전송하지 않는다."}
        if level == RP.AUTO: return {}, meta
        require(profile, "reasoning", request)
        if level not in profile.reasoning_levels: raise GenerationError("UNSUPPORTED_CAPABILITY")
        native = {RP.DISABLED: "none", RP.LOW: "low", RP.MEDIUM: "medium", RP.HIGH: "high", RP.EXTRA_HIGH: "xhigh", RP.MAX: "max"}[level]
        if self.adapter_id == "anthropic":
            values = {"thinking": {"type": "disabled"}} if level == RP.DISABLED else {"output_config": {"effort": native}}
            if level != RP.DISABLED and profile.provider_settings.get("thinking_mode") == "adaptive": values["thinking"] = {"type": "adaptive"}
        elif self.adapter_id == "google_gemini":
            if level not in {RP.LOW, RP.MEDIUM, RP.HIGH}: raise GenerationError("UNSUPPORTED_CAPABILITY")
            values = {"thinking_level": native} if protocol == "interactions" else {"thinkingConfig": {"thinkingLevel": native.upper()}}
        elif self.adapter_id == "deepseek":
            if level not in {RP.DISABLED, RP.LOW, RP.HIGH, RP.MAX}: raise GenerationError("UNSUPPORTED_CAPABILITY")
            values = {"thinking": {"type": "disabled"}} if level == RP.DISABLED else {"thinking": {"type": "enabled"}, "reasoning_effort": native}
        elif self.adapter_id == "mistral":
            if level != RP.HIGH: raise GenerationError("UNSUPPORTED_CAPABILITY")
            values = {"prompt_mode": "reasoning"}
        else:
            if self.adapter_id == "xai" and level not in {RP.LOW, RP.MEDIUM, RP.HIGH, RP.EXTRA_HIGH}: raise GenerationError("UNSUPPORTED_CAPABILITY")
            values = {"reasoning": {"effort": native}} if protocol == "responses" else {"reasoning_effort": native}
        meta.update(effective_reasoning=deepcopy(values), mapping_reason="선택한 모델에서 확인한 설정. 제공사별 의미이며 제공사 간 동등성을 가정하지 않는다.")
        return values, meta

    def headers(self, connection, key):
        headers = {"Content-Type": "application/json"}
        if self.adapter_id == "anthropic":
            headers["anthropic-version"] = ANTHROPIC_VERSION
            if key: headers["x-api-key"] = key
        elif self.adapter_id == "google_gemini":
            if key: headers["x-goog-api-key"] = key
        elif self.adapter_id == "openai_compatible":
            strategy = connection.auth_strategy
            if strategy == "auto": strategy = "none" if connection.endpoint_class == "loopback" else "bearer"
            if key and strategy == "bearer": headers["Authorization"] = "Bearer " + key
            if key and strategy == "api_key": headers[connection.credential_header] = key
        elif key: headers["Authorization"] = "Bearer " + key
        return headers

    def _messages(self, request, protocol):
        messages = request.messages or [GenerationMessage(role="user", text=request.input_text)]
        values = []
        for m in messages:
            if m._provider_content is not None and m._adapter_id != self.adapter_id: raise GenerationError("INVALID_PARAMETER")
            if protocol == "responses":
                if m.role == "tool":
                    values.extend({"type": "function_call_output", "call_id": t.call_id, "output": compact(t.result)} for t in m.tool_results)
                elif m._provider_content is not None: values.extend(deepcopy(m._provider_content))
                else:
                    if m.text: values.append({"role": m.role, "content": m.text})
                    values.extend({"type": "function_call", "call_id": t.call_id, "name": t.tool_name, "arguments": compact(t.arguments)} for t in m.tool_calls)
            elif protocol == "messages":
                if m.role == "tool": content = [{"type": "tool_result", "tool_use_id": t.call_id, "content": compact(t.result), "is_error": t.is_error} for t in m.tool_results]
                else: content = deepcopy(m._provider_content) if m._provider_content is not None else ([{"type": "text", "text": m.text}] if m.text else []) + [{"type": "tool_use", "id": t.call_id, "name": t.tool_name, "input": t.arguments} for t in m.tool_calls]
                values.append({"role": "user" if m.role == "tool" else m.role, "content": content})
            elif protocol == "generate_content":
                if m.role == "tool": content = [{"functionResponse": {"id": t.call_id, "name": t.tool_name, "response": t.result if isinstance(t.result, dict) else {"result": t.result}}} for t in m.tool_results]
                else: content = deepcopy(m._provider_content) if m._provider_content is not None else ([{"text": m.text}] if m.text else []) + [{"functionCall": {"id": t.call_id, "name": t.tool_name, "args": t.arguments}} for t in m.tool_calls]
                values.append({"role": "model" if m.role == "assistant" else "user", "parts": content})
            elif protocol == "interactions":
                if m.role == "tool": values.extend({"type": "function_result", "call_id": t.call_id, "name": t.tool_name, "result": t.result, "is_error": t.is_error} for t in m.tool_results)
                elif m._provider_content is not None: values.extend(deepcopy(m._provider_content))
                else:
                    if m.text: values.append({"type": "user_input" if m.role == "user" else "model_output", "content": [{"type": "text", "text": m.text}]})
                    values.extend({"type": "function_call", "id": t.call_id, "name": t.tool_name, "arguments": t.arguments} for t in m.tool_calls)
            else:
                if m.role == "tool": values.extend({"role": "tool", "tool_call_id": t.call_id, "content": compact(t.result)} for t in m.tool_results)
                elif m._provider_content is not None: values.extend(deepcopy(m._provider_content))
                else:
                    value = {"role": m.role, "content": m.text or None}
                    if m.tool_calls: value["tool_calls"] = [{"id": t.call_id, "type": "function", "function": {"name": t.tool_name, "arguments": compact(t.arguments)}} for t in m.tool_calls]
                    values.append(value)
        return values

    def serialize(self, request, profile, connection):
        p = self.validate_profile(profile, connection)["protocol"]
        reasoning, reasoning_meta = self.reasoning(request, profile, p)
        for name in ("temperature", "top_p", "stop", "seed"):
            if getattr(request, name) is not None: require(profile, name, request)
        if request.tools: require(profile, "tool_calling", request)
        if request.parallel_tool_calls: require(profile, "parallel_tools", request)
        if request.stream: require(profile, "streaming", request)
        if request.store_preference:
            require(profile, "storage", request)
            if p not in {"responses", "interactions"}: raise GenerationError("UNSUPPORTED_CAPABILITY")
        if request.reasoning_policy != RP.AUTO and (request.temperature is not None or request.top_p is not None) and profile.provider_settings.get("sampling_with_reasoning") is not True:
            raise GenerationError("UNSUPPORTED_CAPABILITY")
        if self.adapter_id == "deepseek" and request.reasoning_policy not in {RP.AUTO, RP.DISABLED} and request.tool_choice not in (None, "auto", "none"): raise GenerationError("INVALID_PARAMETER")
        messages, instructions = self._messages(request, p), "\n\n".join(x for x in (request.system_instructions, request.developer_instructions) if x)
        schema = request.structured_output_schema
        strict = capability(profile, "structured_output") == CS.SUPPORTED or request.probe_capability == "structured_output"
        json_mode = capability(profile, "json_mode") == CS.SUPPORTED
        mode = "NATIVE_JSON_SCHEMA" if schema and strict else "NATIVE_JSON_MODE" if schema and json_mode else "APPLICATION_JSON_VALIDATION" if schema else "TEXT"
        if schema and not strict: instructions += "\n다음 스키마에 맞는 JSON만 반환하라. 앱에서 검증하며 제공사의 스키마 강제 기능을 가정하지 않는다.\n" + compact(schema)
        tools = [{"type": "function", "name": t.tool_name, "description": t.description, "parameters": t.parameters} for t in request.tools]
        if p == "responses":
            payload = {"model": profile.model_id, "instructions": instructions, "input": messages, "max_output_tokens": request.max_output_tokens, "store": request.store_preference, "stream": request.stream, **reasoning}
            if request.tools: payload["tools"] = tools
            if schema and strict: payload["text"] = {"format": {"type": "json_schema", "name": "htrsa_output", "schema": schema, "strict": request.native_schema_strict}}
            elif schema and json_mode: payload["text"] = {"format": {"type": "json_object"}}
            suffix = "/responses" if self.adapter_id in {"openai", "openai_compatible"} else "/v1/responses"
        elif p == "messages":
            payload = {"model": profile.model_id, "system": instructions, "messages": messages, "max_tokens": request.max_output_tokens, "stream": request.stream, **reasoning}
            if request.tools: payload["tools"] = [{"name": t.tool_name, "description": t.description, "input_schema": t.parameters} for t in request.tools]
            if schema and strict: payload.setdefault("output_config", {})["format"] = {"type": "json_schema", "schema": schema}
            suffix = "/v1/messages"
        elif p in {"generate_content", "interactions"}:
            if p == "interactions":
                payload = {"model": profile.model_id.removeprefix("models/"), "system_instruction": instructions, "input": messages, "generation_config": {"max_output_tokens": request.max_output_tokens, **reasoning}, "store": request.store_preference, "stream": request.stream}
                if request.tools: payload["tools"] = tools
                if schema and strict: payload["response_format"] = {"type": "text", "mime_type": "application/json", "schema": schema}
                elif schema and json_mode: payload["response_format"] = {"type": "text", "mime_type": "application/json"}
                suffix = "/v1/interactions"
            else:
                config = {"maxOutputTokens": request.max_output_tokens, **reasoning}
                payload = {"systemInstruction": {"parts": [{"text": instructions}]}, "contents": messages, "generationConfig": config}
                if request.tools: payload["tools"] = [{"functionDeclarations": [{"name": t.tool_name, "description": t.description, "parametersJsonSchema": t.parameters} for t in request.tools]}]
                if schema and (strict or json_mode): config["responseMimeType"] = "application/json"
                if schema and strict: config["responseJsonSchema"] = schema
                suffix = "/v1beta/models/" + quote(profile.model_id.removeprefix("models/"), safe="") + (":streamGenerateContent?alt=sse" if request.stream else ":generateContent")
        else:
            payload = {"model": profile.model_id, "messages": ([{"role": "system", "content": instructions}] if instructions else []) + messages, "max_tokens": request.max_output_tokens, "stream": request.stream, **reasoning}
            if request.tools: payload["tools"] = [{"type": "function", "function": {"name": t.tool_name, "description": t.description, "parameters": t.parameters}} for t in request.tools]
            if schema and strict:
                if self.adapter_id == "deepseek": raise GenerationError("UNSUPPORTED_CAPABILITY")
                payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "htrsa_output", "schema": schema, "strict": request.native_schema_strict}}
            elif schema and json_mode: payload["response_format"] = {"type": "json_object"}
            if request.stream and profile.provider_settings.get("stream_usage_parameter") is True: payload["stream_options"] = {"include_usage": True}
            suffix = "/v1/chat/completions" if self.adapter_id in {"mistral", "xai"} else "/chat/completions"
        for name in ("temperature", "top_p", "stop", "seed"):
            value = getattr(request, name)
            if value is None: continue
            if p == "messages" and name == "seed": raise GenerationError("UNSUPPORTED_CAPABILITY")
            target = payload["generationConfig"] if p == "generate_content" else payload["generation_config"] if p == "interactions" else payload
            mapped = {"top_p": "topP", "stop": "stopSequences"}.get(name, name) if p == "generate_content" else "stop_sequences" if name == "stop" and p in {"messages", "interactions"} else "random_seed" if name == "seed" and self.adapter_id == "mistral" else name
            target[mapped] = value
        if request.tool_choice is not None:
            if not request.tools: raise GenerationError("INVALID_PARAMETER")
            choice = request.tool_choice
            if isinstance(choice, dict):
                name = choice.get("name")
                if name not in {t.tool_name for t in request.tools}: raise GenerationError("INVALID_PARAMETER")
                choice = {"type": "tool", "name": name} if p == "messages" else {"type": "function", "name": name} if p in {"responses", "interactions"} else {"type": "function", "function": {"name": name}}
            elif choice not in {"auto", "none", "required"}: raise GenerationError("INVALID_PARAMETER")
            if p == "messages": payload["tool_choice"] = choice if isinstance(choice, dict) else {"type": "any" if choice == "required" else choice}
            elif p == "generate_content":
                cfg = {"mode": {"auto": "AUTO", "none": "NONE", "required": "ANY"}.get(choice, "ANY") if isinstance(choice, str) else "ANY"}
                if isinstance(choice, dict): cfg["allowedFunctionNames"] = [request.tool_choice["name"]]
                payload["toolConfig"] = {"functionCallingConfig": cfg}
            elif p == "interactions": payload["generation_config"]["tool_choice"] = "any" if choice == "required" else choice
            else: payload["tool_choice"] = choice
        if request.parallel_tool_calls:
            if p in {"chat", "responses"}: payload["parallel_tool_calls"] = True
            elif p == "messages": payload.setdefault("tool_choice", {"type": "auto"})["disable_parallel_tool_use"] = False
            else: raise GenerationError("UNSUPPORTED_CAPABILITY")
        if request.explicit_parallel_tool_control:
            if p not in {"responses", "chat"}: raise GenerationError("UNSUPPORTED_CAPABILITY")
            payload["parallel_tool_calls"] = request.parallel_tool_calls
        meta = {"adapter": self.adapter_id, "adapter_version": self.version, "protocol": p, "provider_mode_used": mode,
                "schema_strictness": ("STRICT_NATIVE" if request.native_schema_strict else "NON_STRICT_NATIVE") if schema and strict else "JSON_ONLY" if schema and json_mode else "APPLICATION_ONLY" if schema else "NONE",
                "requested_schema_sha256": sha256(compact(schema).encode()).hexdigest() if schema else None,
                "validation_result": "NOT_VALIDATED", "requested_reasoning": reasoning_meta["requested_reasoning"], "reasoning": reasoning_meta,
                "effective_sampling": {n: getattr(request, n) for n in ("temperature", "top_p", "stop", "seed") if getattr(request, n) is not None},
                "max_output_tokens": request.max_output_tokens, "store_requested": request.store_preference,
                "storage_control": "EXPLICIT" if p in {"responses", "interactions"} else "PROVIDER_POLICY_UNVERIFIED"}
        return connection.base_url.rstrip("/") + suffix, payload, meta

    def normalize(self, document, profile, protocol):
        from .normalized import NormalizedToolCall
        if not isinstance(document, dict): raise GenerationError("MALFORMED_RESPONSE")
        calls, content, text, finish, refusal = [], None, "", None, None
        try:
            if protocol == "responses":
                content = document["output"]
                if not isinstance(content, list): raise ValueError()
                for item in content:
                    if item.get("type") == "function_call": calls.append(NormalizedToolCall(call_id=item["call_id"], tool_name=item["name"], arguments=arguments(item["arguments"]), provider_call_type="function_call"))
                    for part in item.get("content", []):
                        if part.get("type") == "output_text": text += part["text"]
                        if part.get("type") == "refusal": refusal = "PROVIDER_REFUSAL"
                finish = document.get("status")
            elif protocol == "messages":
                content = document["content"]
                for item in content:
                    if item["type"] == "text": text += item["text"]
                    if item["type"] == "tool_use": calls.append(NormalizedToolCall(call_id=item["id"], tool_name=item["name"], arguments=arguments(item["input"]), provider_call_type="tool_use"))
                finish = document.get("stop_reason")
                if finish == "refusal": refusal = "PROVIDER_REFUSAL"
            elif protocol == "generate_content":
                if document.get("promptFeedback", {}).get("blockReason"): refusal = "PROVIDER_REFUSAL"
                candidates = document.get("candidates", [])
                if not candidates and not refusal: raise ValueError()
                candidate = candidates[0] if candidates else {}
                content = candidate.get("content", {}).get("parts", [])
                for index, item in enumerate(content):
                    if "text" in item and not item.get("thought"): text += item["text"]
                    if "functionCall" in item:
                        call = item["functionCall"]
                        calls.append(NormalizedToolCall(call_id=call.get("id") or "gemini-call-" + str(index), tool_name=call["name"], arguments=arguments(call["args"]), provider_call_type="functionCall"))
                finish = candidate.get("finishReason")
                if finish in {"SAFETY", "RECITATION", "PROHIBITED_CONTENT"}: refusal = "PROVIDER_REFUSAL"
            elif protocol == "interactions":
                content = document["steps"]
                for item in content:
                    if item.get("type") == "function_call": calls.append(NormalizedToolCall(call_id=item["id"], tool_name=item["name"], arguments=arguments(item["arguments"]), provider_call_type="function_call"))
                    if item.get("type") == "model_output": text += "".join(p["text"] for p in item.get("content", []) if p.get("type") == "text")
                finish = document.get("status")
            else:
                choice = document["choices"][0]
                message = choice["message"]
                content = [deepcopy(dict(message, role="assistant"))]
                text = message.get("content") or ""
                if not isinstance(text, str): raise ValueError()
                if message.get("refusal"): refusal = "PROVIDER_REFUSAL"
                for call in message.get("tool_calls", []): calls.append(NormalizedToolCall(call_id=call["id"], tool_name=call["function"]["name"], arguments=arguments(call["function"]["arguments"]), provider_call_type="function"))
                finish = choice.get("finish_reason")
            status = "REFUSED" if refusal else "INCOMPLETE" if finish in {"length", "max_tokens", "MAX_TOKENS", "incomplete", "failed", "aborted", "cancelled", "queued", "in_progress", "insufficient_system_resource"} else "REQUIRES_TOOL" if calls else "COMPLETED"
            if not text and not calls and status == "COMPLETED": raise ValueError()
            output = None
            if text:
                try: output = parse_json(text)
                except ValueError: pass
            value = GenerationResult(response_id=document.get("id", document.get("responseId")), model_id=document.get("model", profile.model_id), model_revision=document.get("modelVersion", document.get("system_fingerprint")),
                status=status, output_text=text, structured_output=output, tool_calls=calls, finish_reason=finish, refusal=refusal, usage=parse_usage(self.adapter_id, protocol, document))
            value._provider_content, value._adapter_id = deepcopy(content), self.adapter_id
            return value
        except GenerationError: raise
        except (KeyError, IndexError, TypeError, ValueError): raise GenerationError("MALFORMED_RESPONSE") from None

    async def _http(self, method, url, connection, key, *, client_factory=None, profile=None, payload=None, secrets=(), reservation_id=None):
        if not reservation_id: raise GenerationError("BUDGET_RESERVATION_REQUIRED")
        if (httpx.URL(url).scheme, httpx.URL(url).host, httpx.URL(url).port) != (httpx.URL(connection.base_url).scheme, httpx.URL(connection.base_url).host, httpx.URL(connection.base_url).port): raise GenerationError("AUTHORIZATION_ERROR")
        if client_factory: client = client_factory(connection, profile)
        else:
            from ..control_plane import PinnedTransport
            client = httpx.AsyncClient(transport=PinnedTransport(connection), timeout=httpx.Timeout(min(connection.read_timeout, profile.timeout_sec if profile else 10), connect=connection.connect_timeout), trust_env=False, follow_redirects=False)
        protected = [s.encode("utf-8", errors="strict") for s in secrets if s]
        if key: protected.append(key.encode("utf-8", errors="strict"))
        try:
            async with asyncio.timeout(min(connection.read_timeout, profile.timeout_sec if profile else 10)), client:
                async with client.stream(method, url, headers=self.headers(connection, key), json=payload, follow_redirects=False) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 2000000: raise GenerationError("MALFORMED_RESPONSE")
                        if any(s in data for s in protected): raise GenerationError("SECRET_IN_PROVIDER_RESPONSE")
                    if 300 <= response.status_code < 400: raise GenerationError("AUTHORIZATION_ERROR")
                    if response.status_code >= 400:
                        try: body = json.loads(data)
                        except ValueError: body = {}
                        error = GenerationError(normalized_error(response.status_code, body))
                        error.http_status = response.status_code
                        identity = response.headers.get("x-request-id", response.headers.get("request-id"))
                        if identity and not any(s.decode("utf-8") in identity for s in protected): error.provider_request_id = identity
                        raw_code = body.get("error", {}).get("code") if isinstance(body, dict) and isinstance(body.get("error"), dict) else None
                        if raw_code in {"invalid_api_key", "model_not_found", "insufficient_quota", "rate_limit_exceeded", "context_length_exceeded", "invalid_parameter"}: error.provider_error_code = raw_code
                        raise error
                    request_id = response.headers.get("x-request-id", response.headers.get("request-id"))
                    if request_id and any(s.decode("utf-8") in request_id for s in protected): raise GenerationError("SECRET_IN_PROVIDER_RESPONSE")
                    try:
                        decoded = compact(json.loads(data))
                        if any(s.decode("utf-8") in decoded for s in protected): raise GenerationError("SECRET_IN_PROVIDER_RESPONSE")
                    except (ValueError, UnicodeError): pass
                    return bytes(data), request_id
        except GenerationError: raise
        except (httpx.TimeoutException, TimeoutError): raise GenerationError("TIMEOUT") from None
        except httpx.ConnectError as exc:
            raise GenerationError("TLS_ERROR" if "ssl" in str(type(exc.__cause__)).lower() else "NETWORK_ERROR") from None
        except httpx.HTTPError: raise GenerationError("NETWORK_ERROR") from None

    async def create_response(self, request, profile, connection, key, **kwargs):
        started = time.perf_counter()
        url, payload, meta = self.serialize(request, profile, connection)
        if request.data_egress_policy == "none": raise GenerationError("AUTHORIZATION_ERROR")
        data, request_id = await self._http("POST", url, connection, key, profile=profile, payload=payload, reservation_id=request.budget_reservation_id, **kwargs)
        try:
            if request.stream:
                from .streams import decode_stream
                document, events = decode_stream(data, meta["protocol"])
                meta["stream_events"] = [e.model_dump(mode="json") for e in events]
                meta["stream_delivery"] = "BOUNDED_BUFFER_AFTER_SECRET_VALIDATION"
            else: document = parse_json(data)
            result = self.normalize(document, profile, meta["protocol"])
        except (ValueError, UnicodeError): raise GenerationError("MALFORMED_RESPONSE") from None
        result.provider_metadata = meta
        raw_usage = document.get("usageMetadata", document.get("usage")) or {}
        result.provider_metadata["usage_sources"] = {name: "PROVIDER_REPORTED" if value is not None else "UNKNOWN" for name, value in result.usage.model_dump().items() if name.endswith("tokens")}
        if "total_tokens" not in raw_usage and "totalTokenCount" not in raw_usage:
            result.provider_metadata["usage_sources"]["total_tokens"] = "ESTIMATED" if result.usage.total_tokens is not None else "UNKNOWN"
        result.provider_metadata["provider_reported_model"] = document.get("model", "UNKNOWN")
        result.provider_metadata["provider_identity_source"] = "PROVIDER_REPORTED" if "model" in document else "UNKNOWN"
        result.reasoning_metadata = meta["reasoning"]
        result.provider_request_id = request_id
        result.raw_response_ref = "sha256:" + sha256(data).hexdigest()
        result.latency_ms = (time.perf_counter() - started) * 1000
        if any(secret and secret in result.model_dump_json() for secret in kwargs.get("secrets", ())): raise GenerationError("SECRET_IN_PROVIDER_RESPONSE")
        return result

    async def stream_response(self, request, profile, connection, key, **kwargs):
        if not request.stream: raise GenerationError("INVALID_PARAMETER")
        result = await self.create_response(request, profile, connection, key, **kwargs)
        for event in result.provider_metadata.get("stream_events", []): yield StreamEvent.model_validate(event)
        yield StreamEvent(event="usage", data=result.usage.model_dump(mode="json"))
        yield StreamEvent(event="response_end", data={"status": result.status})

    async def count_tokens(self, request, profile, connection):
        _, payload, _ = self.serialize(request, profile, connection)
        return {"status": "ESTIMATED", "upper_bound": len(compact(payload).encode("utf-8", errors="strict")) + 4096, "source": "UTF8_BYTES_PLUS_PROTOCOL_ALLOWANCE", "exact_tokenizer": False}

    def model(self, item):
        identity = item.get("id", item.get("name"))
        if not isinstance(identity, str) or not identity or len(identity) > 160: raise GenerationError("MALFORMED_RESPONSE")
        caps, settings, levels = {}, {}, []
        checked = utc_now().isoformat()
        raw = item.get("capabilities") or {}
        aliases = {"completion_chat": "text", "text": "text", "tool_calling": "tool_calling", "function_calling": "tool_calling", "structured_outputs": "structured_output", "structured_output": "structured_output", "json_mode": "json_mode", "streaming": "streaming", "reasoning": "reasoning", "usage_reporting": "usage_reporting"}
        if isinstance(raw, dict):
            for key, name in aliases.items():
                v = raw.get(key)
                v = v.get("supported") if isinstance(v, dict) else v
                if isinstance(v, bool): caps[name] = CapabilityEvidence(status=CS.SUPPORTED if v else CS.UNSUPPORTED, source="PROVIDER_METADATA", checked_at=checked, details="Explicit model endpoint field: " + key)
            effort = raw.get("effort", {})
            for native, level in {"low": RP.LOW, "medium": RP.MEDIUM, "high": RP.HIGH, "xhigh": RP.EXTRA_HIGH, "max": RP.MAX}.items():
                if isinstance(effort, dict) and isinstance(effort.get(native), dict) and effort[native].get("supported") is True: levels.append(level)
            if levels: caps["reasoning"] = CapabilityEvidence(status=CS.SUPPORTED, source="PROVIDER_METADATA", checked_at=checked, details="Model effort metadata")
            thinking = raw.get("thinking", {})
            if isinstance(thinking, dict) and thinking.get("types", {}).get("adaptive", {}).get("supported") is True: settings["thinking_mode"] = "adaptive"
        actions = item.get("supportedGenerationMethods")
        if isinstance(actions, list): caps["text"] = CapabilityEvidence(status=CS.SUPPORTED if "generateContent" in actions else CS.UNSUPPORTED, source="PROVIDER_METADATA", checked_at=checked, details="supportedGenerationMethods")
        return DiscoveredModel(model_id=identity, display_name=item.get("display_name", item.get("displayName", identity)), provider_model_revision=item.get("version", item.get("revision")),
            context_window=integer(item.get("context_length", item.get("max_context_length", item.get("context_window")))) or None,
            max_input_tokens=integer(item.get("max_input_tokens", item.get("inputTokenLimit"))) or None,
            max_output_tokens=integer(item.get("max_tokens", item.get("outputTokenLimit", item.get("max_output_tokens")))) or None,
            capabilities=caps, reasoning_levels=levels, provider_settings=settings,
            candidate_pricing={"value": item["pricing"], "source": "PROVIDER_METADATA", "checked_at": checked, "owner_verified": False} if isinstance(item.get("pricing"), dict) else None)

    async def list_models(self, connection, key, *, reservation_id, **kwargs):
        if self.adapter_id == "openai_compatible" and not connection.models_endpoint_enabled: raise GenerationError("UNSUPPORTED_CAPABILITY")
        suffix = "/v1beta/models" if self.adapter_id == "google_gemini" else "/models" if self.adapter_id in {"openai", "openai_compatible", "deepseek"} else "/v1/models"
        models, seen, query = [], set(), ""
        for _ in range(8):
            data, _ = await self._http("GET", connection.base_url.rstrip("/") + suffix + query, connection, key, reservation_id=reservation_id, **kwargs)
            try:
                page = json.loads(data)
                items = page.get("models") if self.adapter_id == "google_gemini" else page.get("data")
                if not isinstance(items, list): raise ValueError()
                for item in items:
                    if not isinstance(item, dict): raise ValueError()
                    model = self.model(item)
                    if model.capabilities.get("text", CapabilityEvidence()).status != CS.UNSUPPORTED: models.append(model)
                if len(models) > 1000: raise ValueError()
                token = page.get("nextPageToken") if self.adapter_id == "google_gemini" else page.get("last_id") if page.get("has_more") is True else None
                if not token:
                    if page.get("has_more") is True: raise ValueError()
                    return models
                if not isinstance(token, str) or token in seen: raise ValueError()
                seen.add(token)
                query = "?" + ("pageToken=" if self.adapter_id == "google_gemini" else "after_id=" if self.adapter_id == "anthropic" else "after=") + quote(token, safe="")
            except (ValueError, TypeError, KeyError): raise GenerationError("MALFORMED_RESPONSE") from None
        raise GenerationError("INCOMPLETE_RESPONSE")

    async def get_model(self, model_id, connection, key, *, reservation_id, **kwargs):
        if self.adapter_id in {"deepseek", "xai", "openai_compatible"}: raise GenerationError("UNSUPPORTED_CAPABILITY")
        suffix = "/v1beta/models/" + quote(model_id.removeprefix("models/"), safe="") if self.adapter_id == "google_gemini" else ("/models/" if self.adapter_id == "openai" else "/v1/models/") + quote(model_id, safe="")
        data, _ = await self._http("GET", connection.base_url.rstrip("/") + suffix, connection, key, reservation_id=reservation_id, **kwargs)
        try: return self.model(json.loads(data))
        except (ValueError, TypeError, KeyError): raise GenerationError("MALFORMED_RESPONSE") from None


class OpenAIAdapter(ProviderAdapter):
    def __init__(self): super().__init__("openai")

class AnthropicAdapter(ProviderAdapter):
    def __init__(self): super().__init__("anthropic")

class GeminiAdapter(ProviderAdapter):
    def __init__(self): super().__init__("google_gemini")

class XAIAdapter(ProviderAdapter):
    def __init__(self): super().__init__("xai")

class DeepSeekAdapter(ProviderAdapter):
    def __init__(self): super().__init__("deepseek")

class MistralAdapter(ProviderAdapter):
    def __init__(self): super().__init__("mistral")

class CompatibleAdapter(ProviderAdapter):
    def __init__(self): super().__init__("openai_compatible")

class ProviderRegistry:
    def __init__(self):
        self.adapters = {a.adapter_id: a for a in (OpenAIAdapter(), AnthropicAdapter(), GeminiAdapter(), XAIAdapter(), DeepSeekAdapter(), MistralAdapter(), CompatibleAdapter())}

    def get(self, adapter_id):
        try: return self.adapters[adapter_id]
        except KeyError: raise GenerationError("UNSUPPORTED_CAPABILITY") from None


REGISTRY = ProviderRegistry()
