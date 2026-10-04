"""안내 원본·문서·공식 링크의 실제 조회 상태를 별도 결과에 기록한다."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from htrsa.tutorial_guide import tutorial_catalog, render_tutorial_guide


def probe(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return {"url": url, "status": "INVALID_URL"}
    try:
        request = Request(url, headers={"User-Agent": "H-TRSA-documentation-check/2"})
        with urlopen(request, timeout=12) as response:
            response.read(1)
            final = urlsplit(response.url)
            status = response.status
            return {"url": url, "http_status": status,
                    "status": "REACHABLE" if status < 400 else "NOT_VALIDATED",
                    "final_page": urlunsplit((final.scheme, final.netloc, final.path, "", ""))}
    except HTTPError as error:
        return {"url": url, "http_status": error.code,
                "status": "BROKEN_LINK" if error.code in {404, 410} else "AUTH_OR_ACCESS_REQUIRED" if error.code in {401, 403} else "NOT_VALIDATED"}
    except (URLError, TimeoutError, OSError, ValueError) as error:
        return {"url": url, "status": "NOT_VALIDATED", "reason": type(error).__name__}


def main():
    catalog = tutorial_catalog()
    urls = set()
    for provider in catalog["providers"]:
        urls.update(url for url in (provider["console"], provider["keys"]) if url)
        urls.update(url for _, url in provider["sources"])
    for tool in catalog["local_tools"]:
        urls.update((tool["download"], tool["source"]))
    urls.update(url for _, url in catalog["benchmarks"])
    with ThreadPoolExecutor(max_workers=6) as pool:
        links = list(pool.map(probe, sorted(urls)))
    synced = (ROOT / "docs/guides/BEGINNER_GUIDE.md").read_text(encoding="utf-8") == render_tutorial_guide()
    invalid = [link for link in links if link["status"] in {"INVALID_URL", "BROKEN_LINK"}]
    value = {"checked_at": datetime.now(timezone.utc).isoformat(), "guide_matches_source": synced,
             "providers": len(catalog["providers"]), "topics": len(catalog["topics"]), "links": links,
             "invalid_links": invalid, "paid_calls": 0,
             "limitation": "로그인이 필요한 발급 화면의 내부 조작·계정별 결제 조건은 자동 검증하지 않는다. 접근 차단·시간 초과를 링크 통과로 기록하지 않는다."}
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8", errors="strict")
    path = ROOT / "build/tutorial/guide_links.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    print(json.dumps({"guide_matches_source": synced, "links": len(links),
                      "reachable": sum(link["status"] == "REACHABLE" for link in links),
                      "auth_or_access_required": sum(link["status"] == "AUTH_OR_ACCESS_REQUIRED" for link in links),
                      "not_validated": sum(link["status"] == "NOT_VALIDATED" for link in links),
                      "invalid": len(invalid), "result": path.relative_to(ROOT).as_posix()}))
    if not synced or invalid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
