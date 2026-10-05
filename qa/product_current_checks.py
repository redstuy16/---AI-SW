"""현재 core 검증 뒤 동결 소스의 전체 회귀 또는 출시 검사를 실행한다."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

import qa_day1 as qa
from probe.preflight import _source_fingerprint, _validation_marker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["full", "freeze"])
    mode = parser.parse_args().mode
    fingerprint = _source_fingerprint()
    started = time.monotonic()
    while True:
        marker = _validation_marker("core") or {}
        if marker.get("passed") and marker.get("source_fingerprint") == fingerprint:
            break
        if time.monotonic() - started > 900:
            raise RuntimeError("CURRENT_CORE_VALIDATION_REQUIRED")
        if _source_fingerprint() != fingerprint:
            raise RuntimeError("SOURCE_CHANGED_DURING_VALIDATION")
        time.sleep(1)
    if mode == "freeze":
        subprocess.run([sys.executable, str(qa.ROOT / "qa/product_frozen_validation.py")], check=True)
        return
    qa.QA = qa.ROOT / "build/product-current"
    result = qa.pytest_run("full-current", [])
    unchanged = fingerprint == _source_fingerprint()
    qa.save("regression.json", {"pytest": result, "source_fingerprint": fingerprint,
                               "source_unchanged": unchanged, "passed": result["passed"] and unchanged})
    print(json.dumps({"pytest": result, "source_unchanged": unchanged}, ensure_ascii=True))
    if not result["passed"] or not unchanged:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
