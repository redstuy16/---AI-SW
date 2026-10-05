"""압축 전후 한도를 적용하고 해제 출력을 작은 청크로 제한한다."""
import zlib

CHUNK = 64 * 1024


class ResponseLimitError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


async def bounded_bytes(response, limit):
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise ResponseLimitError("UNSUPPORTED_CONTENT_ENCODING")
    length = response.headers.get("Content-Length", "0")
    if not length.isdecimal() or int(length) > limit:
        raise ResponseLimitError("RESPONSE_TOO_LARGE")
    # 이미 읽은 응답은 모의 전송 등에서만 전달된다. 실제 네트워크는 원시 스트림을 사용한다.
    buffered = response.is_stream_consumed
    stream = response.aiter_bytes(chunk_size=CHUNK) if buffered else response.aiter_raw(chunk_size=CHUNK)
    decoder = None if buffered or encoding == "identity" else zlib.decompressobj(31 if encoding == "gzip" else 15)
    wire, body = 0, bytearray()
    def append(part):
        if len(part) > CHUNK or len(body) + len(part) > limit:
            raise ResponseLimitError("RESPONSE_TOO_LARGE")
        body.extend(part)
    try:
        async for raw in stream:
            wire += len(raw)
            if wire > limit:
                raise ResponseLimitError("RESPONSE_TOO_LARGE")
            if decoder is None:
                append(raw)
                continue
            pending = raw
            while pending:
                append(decoder.decompress(pending, min(CHUNK, limit - len(body) + 1)))
                pending = decoder.unconsumed_tail
                if decoder.unused_data:
                    raise ResponseLimitError("INVALID_CONTENT_ENCODING")
        if decoder:
            while True:
                part = decoder.decompress(b"", min(CHUNK, limit - len(body) + 1))
                if not part:
                    break
                append(part)
            if not decoder.eof:
                raise ResponseLimitError("INVALID_CONTENT_ENCODING")
        return bytes(body)
    except zlib.error:
        raise ResponseLimitError("INVALID_CONTENT_ENCODING") from None
