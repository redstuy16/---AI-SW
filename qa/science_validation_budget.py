"""검증 세션 사이 누적 과금과 중복 실행을 보호하는 읽기 전용 예산 경계."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
MICRO_USD = Decimal(1000000)
PROTECTED = {"RESERVED", "DISPATCHED", "UNRESOLVED"}
STATUSES = PROTECTED | {"SETTLED", "RELEASED"}
MOCK_EXECUTIONS = {"MOCK", "MOCK_SIMULATION"}


class LiveValidationBudgetError(ValueError):
    """비밀값이나 DB 내용을 포함하지 않는 검증 중단 코드."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _checked_path(path):
    path = Path(path).absolute()
    if any(part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction())
           for part in (path, *path.parents)):
        raise LiveValidationBudgetError("LIVE_VALIDATION_PATH_UNSAFE")
    resolved = path.resolve()
    if not resolved.is_relative_to(WORKSPACE_ROOT):
        raise LiveValidationBudgetError("LIVE_VALIDATION_PATH_UNSAFE")
    return resolved


def _micro(value):
    if type(value) is not int or value < 0:
        raise LiveValidationBudgetError("LIVE_VALIDATION_LEDGER_INVALID")
    return value


class LiveValidationBudget:
    """root 아래 모든 이전 실제 원장을 확인하고 현재 세션 전체에 파일 잠금을 유지한다.

    root는 build/science-live 같은 공통 세션 폴더이며 current_folder는 그 직접 자식이다.
    acquire 성공 뒤 available_usd를 새 원장의 monthly_limit_usd로 사용한다.
    summary.json.execution이 명시적으로 MOCK 또는 MOCK_SIMULATION일 때만 제외한다.
    복제 원장의 같은 요청은 한 번만 합산하며, 같은 ID의 서로 다른 기록은 중단한다.
    close는 잠금 파일을 지우지 않으며, 프로세스가 종료되어도 운영체제가 잠금을 해제한다.
    """
    def __init__(self, root, current_folder, cap, *, conservative_upper_bounds=False):
        self.conservative_upper_bounds=conservative_upper_bounds
        self._active_protected=False
        self.root = _checked_path(root)
        self.current_folder = _checked_path(current_folder)
        if self.current_folder.parent != self.root:
            raise LiveValidationBudgetError("LIVE_VALIDATION_PATH_UNSAFE")
        try:
            self.cap = Decimal(str(cap))
        except Exception:
            raise LiveValidationBudgetError("LIVE_VALIDATION_CAP_INVALID") from None
        if not self.cap.is_finite() or self.cap <= 0:
            raise LiveValidationBudgetError("LIVE_VALIDATION_CAP_INVALID")
        self._handle = None
        self._acquired = False
        self._available = None
        self._metadata = {"status": "NOT_ACQUIRED", "cap_usd": str(self.cap),
                          "previous_spent_usd": None, "available_usd": None,
                          "root": str(self.root), "current_folder": self.current_folder.name,
                          "accounting": "이 검증 폴더의 누적 원장 · 모든 이전 월 포함"}

    @property
    def available_usd(self):
        if not self._acquired:
            raise LiveValidationBudgetError("LIVE_VALIDATION_BUDGET_NOT_ACQUIRED")
        return self._available

    @property
    def metadata(self):
        return deepcopy(self._metadata)

    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        _checked_path(self.root)
        path = _checked_path(self.root / ".live-validation-budget.lock")
        if path.exists() and not path.is_file():
            raise LiveValidationBudgetError("LIVE_VALIDATION_PATH_UNSAFE")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        self._handle = os.fdopen(os.open(path, flags, 0o600), "r+b")
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.close()
            self._handle = None
            raise LiveValidationBudgetError("LIVE_VALIDATION_ALREADY_RUNNING") from None
        if os.fstat(self._handle.fileno()).st_size == 0:
            self._handle.write("0".encode("utf-8", errors="strict"))
            self._handle.flush()

    def _explicit_mock(self, folder):
        marker = _checked_path(folder / "summary.json")
        if not marker.exists():
            return False
        if not marker.is_file() or marker.stat().st_size > 10 * 1024 * 1024:
            raise LiveValidationBudgetError("LIVE_VALIDATION_SESSION_METADATA_INVALID")
        try:
            value = json.loads(marker.read_text(encoding="utf-8", errors="strict"))
        except (OSError, ValueError, UnicodeError):
            raise LiveValidationBudgetError("LIVE_VALIDATION_SESSION_METADATA_INVALID") from None
        if not isinstance(value, dict):
            raise LiveValidationBudgetError("LIVE_VALIDATION_SESSION_METADATA_INVALID")
        return value.get("execution") in MOCK_EXECUTIONS

    def _read_ledger(self, path, seen):
        path = _checked_path(path)
        if not path.is_file():
            raise LiveValidationBudgetError("LIVE_VALIDATION_LEDGER_UNREADABLE")
        db = None
        spent = protected = protected_count = duplicate_count = 0
        try:
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
            db.execute("PRAGMA query_only=ON")
            for request_id, status, reserved, settled in db.execute("SELECT id,status,reserved,settled FROM spend_ledger"):
                if not isinstance(request_id, str) or not request_id.strip() or status not in STATUSES:
                    raise LiveValidationBudgetError("LIVE_VALIDATION_LEDGER_INVALID")
                reserved = _micro(reserved)
                settled = _micro(settled) if settled is not None else None
                if status == "SETTLED" and settled is None:
                    raise LiveValidationBudgetError("LIVE_VALIDATION_LEDGER_INVALID")
                if status == "RELEASED" and settled not in (None, 0):
                    raise LiveValidationBudgetError("LIVE_VALIDATION_LEDGER_INVALID")
                record = (status, reserved, settled)
                if request_id in seen:
                    if seen[request_id] != record:
                        raise LiveValidationBudgetError("LIVE_VALIDATION_HISTORY_CONFLICT")
                    duplicate_count += 1
                    continue
                seen[request_id] = record
                if status == "SETTLED":
                    spent += settled
                elif status in PROTECTED:
                    protected += reserved
                    protected_count += 1
                    if status!='UNRESOLVED':self._active_protected=True
        except sqlite3.Error:
            raise LiveValidationBudgetError("LIVE_VALIDATION_LEDGER_UNREADABLE") from None
        finally:
            if db is not None:
                db.close()
        return spent, protected, protected_count, duplicate_count

    def acquire(self):
        if self._acquired:
            return self
        try:
            self._lock()
            spent = protected = protected_count = duplicate_count = 0
            seen = {}
            runs, excluded = [], []
            for entry in sorted(self.root.iterdir(), key=lambda item: item.name):
                if entry.name == ".live-validation-budget.lock":
                    continue
                folder = _checked_path(entry)
                if folder == self.current_folder or not folder.is_dir():
                    continue
                database = _checked_path(folder / "state.sqlite")
                if not database.exists():
                    continue
                if self._explicit_mock(folder):
                    excluded.append(folder.name)
                    continue
                run_spent, run_protected, run_count, run_duplicates = self._read_ledger(database, seen)
                spent += run_spent
                protected += run_protected
                protected_count += run_count
                duplicate_count += run_duplicates
                runs.append({"folder": folder.name, "spent_usd": str(Decimal(run_spent) / MICRO_USD),
                    "protected_usd": str(Decimal(run_protected) / MICRO_USD), "protected_requests": run_count,
                    "duplicate_requests": run_duplicates})
            held=protected if self.conservative_upper_bounds else 0
            self._available = max(Decimal(0), self.cap - Decimal(spent+held) / MICRO_USD)
            self._metadata.update(previous_spent_usd=str(Decimal(spent) / MICRO_USD), available_usd=str(self._available),
                previous_protected_usd=str(Decimal(protected) / MICRO_USD), protected_requests=protected_count,
                previous_runs=runs, excluded_mock_runs=excluded, checked_runs=len(runs),
                duplicate_requests=duplicate_count, unique_requests=len(seen))
            self._metadata['unresolved_policy']='최대 예약액 전액 차감 · 미정산 기록 유지' if self.conservative_upper_bounds else '정산 전 중단'
            if protected_count and (not self.conservative_upper_bounds or self._active_protected):
                raise LiveValidationBudgetError("NEEDS_RECONCILIATION")
            if self._available <= 0:
                raise LiveValidationBudgetError("GLOBAL_BUDGET_EXHAUSTED")
            self._acquired = True
            self._metadata["status"] = "ACQUIRED"
            return self
        except Exception as exc:
            self.close()
            code = exc.code if isinstance(exc, LiveValidationBudgetError) else "LIVE_VALIDATION_BUDGET_UNAVAILABLE"
            self._metadata.update(status="BLOCKED", error_code=code)
            if isinstance(exc, LiveValidationBudgetError):
                raise
            raise LiveValidationBudgetError(code) from None

    def close(self):
        handle, self._handle = self._handle, None
        was_acquired, self._acquired = self._acquired, False
        if handle is not None:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                handle.close()
        if was_acquired:
            self._metadata["status"] = "CLOSED"

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback):
        self.close()
