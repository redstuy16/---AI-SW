"""출시·대시보드 명령의 진입점."""
from __future__ import annotations

import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "agent"
    if command == "export":
        from .release import main as run
        sys.argv.pop(1)
        run()
    elif command == "dashboard":
        from .dashboard import main as run
        sys.argv.pop(1)
        run()
    elif command == "demo":
        from .demo import main as run
        sys.argv.pop(1)
        run()
    else:
        from .agent_cli import main as run
        run()


if __name__ == "__main__":
    main()
