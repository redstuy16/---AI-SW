"""pythonw와 Windows 실행 파일에서 기존 작업대를 콘솔 없이 시작한다."""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import webbrowser


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


def main(argv=None, *, runner=None, dialog=None):
    from .workbench import main as workbench_main
    parser = argparse.ArgumentParser(description="H-TRSA 콘솔 없는 실행")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "build/workbench")
    args = parser.parse_args(argv)
    dialog = dialog or notify
    try:
        database, workspace = launch_paths(args.data_dir)
        database.parent.mkdir(parents=True, exist_ok=True)
        (runner or workbench_main)([str(database), str(workspace)],
                                  fallback=lambda url: fallback_link(url, dialog=dialog), quiet=True)
        return 0
    except (OSError, ValueError):
        dialog("H-TRSA를 시작하지 못했습니다. 가상환경 설치와 자료 경로 접근 권한을 확인해 주세요. 연구 자료는 삭제하지 않았습니다.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
