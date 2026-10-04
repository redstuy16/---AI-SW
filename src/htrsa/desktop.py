"""pythonw와 Windows 실행 파일에서 기존 작업대를 콘솔 없이 시작한다."""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import sqlite3
import webbrowser

from .storage_errors import storage_error_code


ROOT = Path(__file__).resolve().parents[2]


def launch_paths(data_dir):
    path = Path(data_dir)
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT.resolve()) or any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (path, *path.parents)):
        raise ValueError("앱 자료는 저장소 내부의 실제 경로를 사용하세요.")
    return resolved / "state.sqlite", resolved / "workspace"


def notify(message, *, question=False):
    if os.name != "nt":
        return False
    return ctypes.windll.user32.MessageBoxW(None, message, "H-TRSA", 1 if question else 0) == 1


def fallback_link(url, *, opener=None, dialog=None):
    dialog = dialog or notify
    if not dialog("브라우저를 자동으로 열지 못했습니다.\n발급 후 60초 동안 한 번만 쓰는 연결입니다. 확인을 누르면 다시 엽니다.\n취소하면 앱을 종료합니다. 이미 실행한 연구는 별도 중지해야 합니다.", question=True):
        return False
    try:
        opened = bool((opener or webbrowser.open)(url, new=2, autoraise=True))
    except Exception:
        opened = False
    if not opened:
        dialog("브라우저 연결을 완료하지 못했습니다. 기본 브라우저 설정을 확인하고 H-TRSA를 다시 실행해 주세요.")
    return opened


def startup_error_message(error):
    if isinstance(error, ImportError):
        return "앱 실행에 필요한 패키지가 없습니다. 가상환경 설치를 확인해 주세요."
    if isinstance(error, ValueError):
        return "앱 자료 경로와 설정을 확인해 주세요."
    messages = {
        "STORAGE_FULL": "저장 공간이 부족합니다. 공간을 확보한 뒤 앱을 다시 실행해 주세요.",
        "STORAGE_ACCESS_DENIED": "앱 자료 폴더에 접근할 수 없습니다. 접근 권한을 확인해 주세요.",
        "DATABASE_BUSY": "다른 작업이 연구 자료를 사용 중입니다. 잠시 후 다시 실행해 주세요.",
        "DATABASE_INVALID": "연구 자료 DB를 읽을 수 없습니다. 백업과 DB 상태를 확인해 주세요.",
        "DATABASE_UNAVAILABLE": "연구 자료 DB를 열 수 없습니다. 자료 경로와 접근 권한을 확인해 주세요.",
    }
    return messages.get(storage_error_code(error), "앱을 시작하지 못했습니다. 자료 경로와 실행 환경을 확인해 주세요.")


def main(argv=None, *, runner=None, dialog=None):
    parser = argparse.ArgumentParser(description="H-TRSA 콘솔 없는 실행")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "build/workbench")
    args = parser.parse_args(argv)
    dialog = dialog or notify
    try:
        database, workspace = launch_paths(args.data_dir)
        database.parent.mkdir(parents=True, exist_ok=True)
        if runner is None:
            from .workbench import main as runner
        runner([str(database), str(workspace)],
               fallback=lambda url: fallback_link(url, dialog=dialog), quiet=True)
        return 0
    except (OSError, ValueError, sqlite3.Error, ImportError) as error:
        dialog(startup_error_message(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
