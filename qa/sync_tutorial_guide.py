"""튜토리얼 원본과 한국어 사용 안내의 일치를 검사한다."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from htrsa.tutorial_guide import render_tutorial_guide


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    path = ROOT / "docs/guides/BEGINNER_GUIDE.md"
    data = render_tutorial_guide().encode("utf-8", errors="strict")
    if args.write:
        path.write_bytes(data)
    elif path.read_bytes() != data:
        raise SystemExit("사용 안내가 원본과 다릅니다. --write로 동기화하세요.")
    print("사용 안내 동기화 PASS")


if __name__ == "__main__":
    main()
