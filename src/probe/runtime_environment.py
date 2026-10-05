"""앱·PDF·계산 자식에 같은 실행기와 제한된 환경을 적용한다."""
from functools import lru_cache
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from .sandbox import clean_environment


def child_environment():
    value = clean_environment()
    value.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return value


@lru_cache(maxsize=1)
def interpreter_preflight():
    dependencies = {name: importlib.util.find_spec(name) is not None for name in ("pypdf", "reportlab", "numpy", "scipy", "httpx")}
    child = False
    try:
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "probe.runtime_environment"],
            capture_output=True, check=False, timeout=5, env=child_environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        output = json.loads(result.stdout.decode("utf-8", errors="strict"))
        child = result.returncode == 0 and Path(output["executable"]).resolve() == Path(sys.executable).resolve() and output["prefix"] == sys.prefix and all(output["dependencies"].values())
    except (OSError, ValueError, KeyError, UnicodeError, subprocess.TimeoutExpired):
        pass
    ready = all(dependencies.values()) and child
    return {"status": "READY" if ready else "BLOCKED", "dependencies": dependencies,
            "child_started": child, "executable": sys.executable, "error": None if ready else "PYTHON_ENVIRONMENT_BLOCKED"}


if __name__ == "__main__":
    value = {"executable": sys.executable, "prefix": sys.prefix,
             "dependencies": {name: importlib.util.find_spec(name) is not None for name in ("pypdf", "reportlab", "numpy", "scipy", "httpx")}}
    text = json.dumps(value, ensure_ascii=False)
    text.encode("utf-8", errors="strict")
    print(text)
