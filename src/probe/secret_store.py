"""Windows 자격 증명 관리자의 일반 자격 증명. 키는 메타데이터로 반환하지 않는다."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import os


class SecretStoreUnavailable(OSError):
    pass


class WindowsCredentialStore:
    def __init__(self, namespace="Probe"):
        self.namespace = namespace
        # 이전 프로젝트에서 저장한 키는 읽되 새 키는 Probe에 저장한다.
        self.legacy_namespaces = ("H-TRSA",) if namespace == "Probe" else ()
        self.available = os.name == "nt"
        if not self.available:
            return
        class Credential(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
        self.Credential = Credential
        self.dll = ctypes.WinDLL("advapi32", use_last_error=True)
        self.dll.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
        self.dll.CredReadW.restype = wintypes.BOOL
        self.dll.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        self.dll.CredWriteW.restype = wintypes.BOOL
        self.dll.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.dll.CredDeleteW.restype = wintypes.BOOL
        self.dll.CredFree.argtypes = [ctypes.c_void_p]

    def read(self, name):
        for namespace in (self.namespace, *self.legacy_namespaces):
            value, changed = self._read_namespace(name, namespace)
            if value is not None:
                return value, changed
        return None, None

    def _read_namespace(self, name, namespace):
        if not self.available:
            return None, None
        pointer = ctypes.POINTER(self.Credential)()
        if not self.dll.CredReadW(namespace + "/" + name, 1, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == 1168:
                return None, None
            if error in {1312, 50}:
                self.available = False
                return None, None
            raise SecretStoreUnavailable(error, "OS 자격 증명 읽기 실패")
        try:
            entry = pointer.contents
            value = ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize).decode("utf-8", errors="strict")
            ticks = (entry.LastWritten.dwHighDateTime << 32) | entry.LastWritten.dwLowDateTime
            changed = datetime.fromtimestamp((ticks - 116444736000000000) / 10000000, timezone.utc).isoformat()
            return value, changed
        finally:
            self.dll.CredFree(pointer)

    def write(self, name, value):
        if not self.available:
            raise SecretStoreUnavailable("OS 자격 증명 관리자를 사용할 수 없음")
        target = self.namespace + "/" + name
        if value is None:
            # 이전 저장소의 키도 제거해 삭제한 키가 다시 활성화되지 않게 한다.
            for namespace in (self.namespace, *self.legacy_namespaces):
                if not self.dll.CredDeleteW(namespace + "/" + name, 1, 0) and ctypes.get_last_error() != 1168:
                    raise SecretStoreUnavailable(ctypes.get_last_error(), "OS 자격 증명 삭제 실패")
            return
        data = value.encode("utf-8", errors="strict")
        blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        entry = self.Credential(Type=1, TargetName=target, CredentialBlobSize=len(data), CredentialBlob=blob,
                                Persist=2, UserName="Probe")
        try:
            if not self.dll.CredWriteW(ctypes.byref(entry), 0):
                raise SecretStoreUnavailable(ctypes.get_last_error(), "OS 자격 증명 저장 실패")
        finally:
            ctypes.memset(blob, 0, len(data))
