"""기존 SQLite의 소유자 제어·지출 원장. 금액은 보수적으로 반올림한 정수 micro-USD다."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr, StrictInt, model_validator

from .database import to_json, transaction
from .schemas import StrictModel, new_id, utc_now
from .storage import sha256_file
from .providers.base import ModelProviderError
from .providers.normalized import CAPABILITIES, CapabilityEvidence, ReasoningPolicy
from .providers.native import DEFINITIONS


DEPTHS = {
    "explore": {"label": "탐색", "hypotheses": 1, "sources": 5, "experiments": 1, "reviews": 1, "attempts": 8},
    "standard": {"label": "표준", "hypotheses": 3, "sources": 12, "experiments": 3, "reviews": 2, "attempts": 20},
    "deep": {"label": "심층", "hypotheses": 5, "sources": 25, "experiments": 6, "reviews": 3, "attempts": 40},
    "focused": {"label": "집중", "hypotheses": 8, "sources": 40, "experiments": 10, "reviews": 5, "attempts": 64},
}
ROLES = ("manager", "experiment_coordinator", "analysis_planner_worker", "verification_coordinator")
ACCOUNTING_ZONE = timezone(timedelta(hours=9), "Asia/Seoul")


def resolved_depth(name):
    proposed = DEPTHS[name]
    return {**proposed, "hypotheses": min(5, proposed["hypotheses"]), "shortlist": min(3, proposed["hypotheses"]),
            "active_hypotheses": min(2, proposed["hypotheses"]), "sources": 0,
            "experiments": min(2, proposed["experiments"]), "reviews": min(2, proposed["reviews"]),
            "attempts": min(20, proposed["attempts"])}


class ControlError(ModelProviderError):
    def __init__(self, code: str):
        super().__init__(code, retryable=False)


class ControlBoundary(BaseException):
    """안전한 저장 경계에서 중단한다. 제공사 재시도로 취급하지 않는다."""


class Connection(StrictModel):
    connection_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    display_name: str = Field(min_length=1, max_length=120)
    adapter_id: Literal["openai", "anthropic", "google_gemini", "xai", "deepseek", "mistral", "openai_compatible"] = "openai"
    base_url: str = "https://api.openai.com/v1"
    credential_env_name: str | None = Field(default="OPENAI_API_KEY", pattern=r"^[A-Z][A-Z0-9_]{1,80}$")
    endpoint_class: Literal["cloud", "loopback"] = "cloud"
    destination_approved: bool = False
    enabled: bool = True
    auth_strategy: Literal["auto", "none", "bearer", "api_key"] = "auto"
    credential_header: str = Field(default="x-api-key", pattern=r"^[A-Za-z][A-Za-z0-9-]{0,60}$")
    connect_timeout: int = Field(default=10, ge=1, le=30)
    read_timeout: int = Field(default=60, ge=1, le=120)
    models_endpoint_enabled: bool = True
    discovery_unmetered: bool = False

    @model_validator(mode="before")
    @classmethod
    def native_defaults(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            definition = DEFINITIONS.get(value.get("adapter_id", "openai"))
            if definition:
                if definition["base_url"]: value.setdefault("base_url", definition["base_url"])
                credential = "OPENAI_API_KEY" if value.get("adapter_id") == "openai_compatible" and value.get("endpoint_class", "cloud") == "cloud" else definition["credential"]
                value.setdefault("credential_env_name", credential)
        return value

    @model_validator(mode="after")
    def endpoint(self):
        validate_endpoint(self.base_url, self.endpoint_class, native=self.adapter_id != "openai_compatible")
        if self.adapter_id != "openai_compatible" and self.base_url.rstrip("/") != DEFINITIONS[self.adapter_id]["base_url"]:
            raise ValueError("native adapter requires official endpoint")
        if self.credential_header.lower() in {"host", "cookie", "content-length", "connection", "proxy-authorization"}:
            raise ValueError("credential header denied")
        if self.adapter_id != "openai_compatible" and self.auth_strategy != "auto":
            raise ValueError("native authentication is fixed")
        return self


class PriceRecord(StrictModel):
    currency: Literal["USD"] = "USD"
    input_per_million: Decimal = Field(ge=0, le=10000, allow_inf_nan=False)
    output_per_million: Decimal = Field(ge=0, le=10000, allow_inf_nan=False)
    cached_input_per_million: Decimal | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
    cache_write_per_million: Decimal | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
    source: str = Field(min_length=1, max_length=200)
    checked_at: datetime
    revision: str = Field(min_length=1, max_length=80)
    owner_verified: bool = False
    web_search_per_call: Decimal | None = Field(default=None, ge=0, le=10, allow_inf_nan=False)
    web_search_price_source: str | None = Field(default=None, max_length=500)


class ConnectionRegistration(StrictModel):
    value: Connection
    api_key: SecretStr | None = None
    expected_revision: int = Field(default=0, ge=0)


class ModelProfile(StrictModel):
    profile_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    connection_id: str
    model_id: str = Field(min_length=1, max_length=160)
    protocol: Literal["auto", "responses", "chat", "messages", "interactions", "generate_content"] = "auto"
    input_byte_limit: int = Field(default=32000, ge=256, le=200000)
    context_limit: int = Field(default=32768, ge=512, le=1000000)
    output_limit: int = Field(default=2048, ge=32, le=32768)
    task_output_limits: dict[Literal["planning", "report"], StrictInt] = Field(default_factory=dict)
    timeout_sec: int = Field(default=60, ge=1, le=120)
    concurrency: Literal[1] = 1
    reasoning_effort: str | None = None
    price: PriceRecord | None = None
    local_api_unmetered: bool = False
    capability_status: Literal["unknown", "supported", "unsupported"] = "unknown"
    capability_source: str | None = None
    capability_checked_at: str | None = None
    display_name: str | None = Field(default=None, max_length=160)
    model_alias: str | None = Field(default=None, max_length=160)
    resolved_model_id: str | None = Field(default=None, max_length=160)
    provider_model_revision: str | None = Field(default=None, max_length=160)
    max_input_tokens: int | None = Field(default=None, ge=1, le=1000000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1000000)
    input_modalities: list[Literal["text", "image", "audio", "video"]] = Field(default_factory=lambda: ["text"])
    output_modalities: list[Literal["text", "image", "audio", "video"]] = Field(default_factory=lambda: ["text"])
    capabilities: dict[str, CapabilityEvidence] = Field(default_factory=dict)
    reasoning_levels: list[ReasoningPolicy] = Field(default_factory=list)
    reasoning_policy: ReasoningPolicy = ReasoningPolicy.AUTO
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    stop: list[str] | None = Field(default=None, max_length=4)
    seed: int | None = None
    parallel_tool_calls: bool = False
    stream: bool = False
    store_preference: bool = False
    max_retries: Literal[0] = 0
    provider_settings: dict[str, str | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded(self):
        if self.output_limit >= self.context_limit:
            raise ValueError("output must fit context")
        if any(not 32 <= n <= 32768 for n in self.task_output_limits.values()):
            raise ValueError("task output limit is outside supported range")
        if self.reasoning_effort is not None:
            raise ValueError("use normalized reasoning_policy with model capability evidence")
        if set(self.capabilities) - set(CAPABILITIES): raise ValueError("unknown capability")
        if set(self.provider_settings) - {"thinking_mode", "sampling_with_reasoning", "stream_usage_parameter"}: raise ValueError("unknown provider setting")
        if self.provider_settings.get("thinking_mode") not in {None, "adaptive"}: raise ValueError("invalid thinking mode")
        if any(not x or len(x) > 200 for x in self.stop or []): raise ValueError("invalid stop")
        return self


class RoutingProfile(StrictModel):
    profile_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    display_name: str = Field(min_length=1, max_length=120)
    routing: dict[str, str]
    reviewer_profile_id: str | None = None
    fallback_policy: Literal["NONE"] = "NONE"
    max_parallel: Literal[1] = 1

    @model_validator(mode="after")
    def roles(self):
        if set(self.routing) - set(ROLES): raise ValueError("unknown role")
        return self


class Defaults(StrictModel):
    research_depth: Literal["explore", "standard", "deep", "focused"] = "standard"
    monthly_limit_usd: Decimal = Field(default=Decimal("20"), gt=0, le=1000, allow_inf_nan=False)
    request_limit_usd: Decimal = Field(default=Decimal("0.25"), gt=0, le=100, allow_inf_nan=False)
    accounting_timezone: Literal["Asia/Seoul"] = "Asia/Seoul"
    search_attempt_limit: StrictInt = Field(default=10, ge=0, le=20)


from .research_design import DetailedDesign


class NewResearch(StrictModel):
    detailed_design: DetailedDesign | None = None
    beginner_mode: bool = False
    research_profile_mode: Literal["AUTO", "DISABLED"] = "DISABLED"
    title: str = Field(default="", max_length=200)
    question: str = Field(min_length=1, max_length=4000)
    source_relative: str | None = Field(default=None, min_length=1, max_length=200)
    settings_version: Literal[1, 2] = 1
    draft_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,100}$")
    draft_revision: int = Field(default=0, ge=0)
    submission_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,100}$")
    selected_model_pool: list[str] = Field(default_factory=list, max_length=30)
    attachments: list[str] = Field(default_factory=list, max_length=10)
    analysis_attachment_id: str | None = None
    advanced_performance_profile: Literal["FAST", "BALANCED", "DEEP", "MAX"] | None = None
    model_reasoning: ReasoningPolicy | None = None
    sampling_mode: Literal["provider_default", "profile"] = "provider_default"
    report_format: Literal["pdf"] = "pdf"
    ai_report_enabled: bool = False
    execution_mode: Literal["LEGACY", "SCIENCE_AUTO"] = "LEGACY"
    science_max_decisions: StrictInt = Field(default=6, ge=1, le=20)
    science_no_progress_limit: StrictInt = Field(default=2, ge=1, le=5)
    source_fetch_attempt_limit: StrictInt = Field(default=10, ge=1, le=20)
    audience: Literal["student", "teacher"] = "student"
    science_field: Literal["general", "physics", "chemistry", "biology", "earth_science"] = "general"
    research_depth: Literal["explore", "standard", "deep", "focused"] = "standard"
    routing: dict[str, str] = Field(default_factory=dict)
    routing_profile_id: str | None = None
    run_limit_usd: Decimal = Field(default=Decimal("1"), gt=0, le=100, allow_inf_nan=False)
    max_elapsed_sec: int = Field(default=300, ge=10, le=3600)
    egress: Literal["none", "selected", "research"] = "none"
    question_only: bool = False
    verified_analysis_skills: bool = False
    verification_repair: bool = False
    ridge_arithmetic_check: bool = False
    performance_profile: Literal["FAST", "BALANCED", "DEEP", "MAX"] | None = None
    adaptive_budget: bool = True
    model_profile_id: str | None = None
    manual_role_override: bool = False
    role_reasoning: dict[str, ReasoningPolicy] = Field(default_factory=dict)
    approved_worker_profiles: list[str] = Field(default_factory=list, max_length=8)
    report_style: Literal["friendly", "technical"] = "friendly"
    max_followups: int | None = Field(default=None, ge=0, le=2)
    search_policy: Literal["AUTO", "DISABLED", "ALLOWED"] = "DISABLED"
    public_search_query: str = Field(default="", max_length=500)
    public_search_consent: bool = False
    search_required: bool = False
    search_attempt_limit: StrictInt = Field(default=10, ge=0, le=20)
    fulltext_enabled: bool = False
    openalex_archive_enabled: bool = False
    searxng_url: str | None = Field(default=None, max_length=500)

    @model_validator(mode="before")
    @classmethod
    def compatible_defaults(cls, raw):
        if not isinstance(raw, dict):
            return raw
        value = dict(raw)
        value.setdefault("title", str(value.get("question", "")).strip()[:100])
        if value.get("source_relative") == "":
            value["source_relative"] = None
        value.setdefault("settings_version", 1 if value.get("source_relative") else 2)
        if value["settings_version"] == 2:
            value.setdefault("search_required", False)
            value.setdefault("search_policy", "AUTO")
            value.setdefault("public_search_consent", True)
            value.setdefault("performance_profile", "BALANCED")
        value["report_format"] = "pdf"
        return value

    @model_validator(mode="after")
    def flags(self):
        if not self.question.strip() or not self.title.strip():
            raise ValueError('연구 질문과 이름이 필요합니다')
        if self.searxng_url:
            from .source_documents import validate_public_url
            object.__setattr__(self, 'searxng_url', validate_public_url(self.searxng_url.strip(), allow_loopback=True, root=True).rstrip('/'))
        if self.ridge_arithmetic_check and not self.verification_repair:
            raise ValueError("Ridge requires repair")
        if not set(self.routing) <= set(ROLES):
            raise ValueError("unknown role")
        if not set(self.role_reasoning) <= set(ROLES):
            raise ValueError("unknown reasoning role")
        return self


class CredentialInput(StrictModel):
    value: SecretStr = Field(min_length=1, max_length=512)


def micro(value) -> int:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ControlError("INVALID_MONEY")
    return int((amount * 1000000).to_integral_value(rounding=ROUND_CEILING))


def money(value: int) -> str:
    return str(Decimal(value) / 1000000)


def validate_endpoint(url: str, endpoint_class: str, *, native=False) -> tuple[str, int]:
    parsed = urlsplit(url)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise ControlError("ENDPOINT_INVALID") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(ord(c) < 33 for c in url)
            or parsed.path.rstrip("/") not in ({"", "/v1"} if native else {"/v1"})):
        raise ControlError("ENDPOINT_INVALID")
    host = parsed.hostname
    if endpoint_class == "loopback":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ControlError("ENDPOINT_DENIED")
        except ValueError:
            raise ControlError("LOOPBACK_LITERAL_REQUIRED") from None
    elif parsed.scheme != "https":
        raise ControlError("TLS_REQUIRED")
    return host, port


class PinnedTransport(httpx.AsyncBaseTransport):
    """DNS 주소를 검사하고 고정 IP에 TLS SNI로 연결한다. 인증과 요청은 승인된 출처를 벗어나지 않는다."""
    def __init__(self, connection: Connection):
        host, port = validate_endpoint(connection.base_url, connection.endpoint_class, native=connection.adapter_id != "openai_compatible")
        self._pin(connection.base_url, connection.endpoint_class, host, port)

    @classmethod
    def scholarly(cls, host):
        if host not in {"api.crossref.org", "api.openalex.org"}:
            raise ControlError("SEARCH_DESTINATION_DENIED")
        value = cls.__new__(cls)
        value._pin("https://" + host, "cloud", host, 443)
        return value

    @classmethod
    def public_resource(cls, url, *, allow_loopback=False):
        from urllib.parse import urlsplit
        target = urlsplit(url)
        if not target.hostname or target.username or target.password or target.fragment:
            raise ControlError("SEARCH_DESTINATION_DENIED")
        try:
            loopback = ipaddress.ip_address(target.hostname).is_loopback
        except ValueError:
            loopback = False
        if loopback:
            if not allow_loopback or target.scheme not in {"http", "https"}:
                raise ControlError("SEARCH_DESTINATION_DENIED")
        elif target.scheme != "https" or target.port not in {None, 443}:
            raise ControlError("SEARCH_DESTINATION_DENIED")
        value = cls.__new__(cls)
        value._pin(url, "loopback" if loopback else "cloud", target.hostname,
                   target.port or (443 if target.scheme == "https" else 80))
        return value

    def _pin(self, origin, endpoint_class, host, port):
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        if not addresses or any((not ipaddress.ip_address(a).is_loopback if endpoint_class == "loopback"
                                 else not ipaddress.ip_address(a).is_global) for a in addresses):
            raise ControlError("ENDPOINT_DENIED")
        self.host, self.port, self.ip = host, port, addresses[0]
        self.origin = httpx.URL(origin)
        self.transport = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        if (request.url.scheme, request.url.host, request.url.port) != (self.origin.scheme, self.origin.host, self.origin.port):
            raise ControlError("CROSS_ORIGIN_DENIED")
        request.headers["Host"] = self.origin.netloc.decode()
        request.extensions["sni_hostname"] = self.host
        request.url = request.url.copy_with(host=self.ip)
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        await self.transport.aclose()


class Credentials:
    def __init__(self, repository: Path, workspace: Path, file: Path | None = None):
        self.roots = (repository.resolve(), workspace.resolve())
        self.file = file
        if file is None:
            base = Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA") else Path.home() / ".config"
            self.file = base / "Probe" / "secrets.env"
            # 기존 보안 파일의 경로·권한을 유지하며 키를 잃지 않게 한다.
            legacy_file = base / "H-TRSA" / "secrets.env"
            if not self.file.exists() and legacy_file.exists():
                self.file = legacy_file
        from .secret_store import WindowsCredentialStore
        self.os_store = WindowsCredentialStore() if file is None else None

    def _path_boundary(self, path):
        if path.is_symlink() or any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (path, *path.parents)):
            raise ControlError("SECRET_PATH_UNSAFE")
        if any(path.resolve().is_relative_to(root) for root in self.roots):
            raise ControlError("SECRET_PATH_IN_RESEARCH_ROOT")

    def _secure(self, path: Path, directory=False):
        self._path_boundary(path)
        if os.name == "nt":
            if not path.exists():
                raise ControlError("SECRET_PATH_UNAVAILABLE")
            script = "$a=Get-Acl -LiteralPath $env:PROBE_ACL_PATH; $u=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $ok=($a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -eq $u); foreach($r in $a.Access){$s=$r.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value; if($r.AccessControlType -eq 'Allow' -and $s -notin @($u,'S-1-5-18','S-1-5-32-544','S-1-3-4')){$ok=$false}}; if($ok){'verified'}"
            result = subprocess.run([shutil.which("pwsh") or "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                    env={**__import__("probe.sandbox", fromlist=["clean_environment"]).clean_environment(), "PROBE_ACL_PATH": str(path)}, capture_output=True, timeout=10, check=False)
            if result.returncode or result.stdout.strip() != b"verified":
                raise ControlError("SECRET_ACL_UNVERIFIED")
        elif path.stat().st_mode & (0o077 if directory else 0o177):
            raise ControlError("SECRET_MODE_UNSAFE")

    def _values(self):
        if not self.file.exists():
            return {}
        self._secure(self.file.parent, True)
        self._secure(self.file)
        result = {}
        for line in self.file.read_text(encoding="utf-8", errors="strict").split("\n"):
            if not line or line.startswith("#"):
                continue
            name, sep, value = line.partition("=")
            if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", name) or any(ord(c) < 33 or ord(c) == 127 or c.isspace() for c in value):
                raise ControlError("SECRET_FILE_INVALID")
            result[name] = value
        return result

    def get(self, name):
        if name is None: return None
        value = os.environ.get(name)
        if not value and self.os_store and self.os_store.available:
            value = self.os_store.read(name)[0]
        if not value:
            value = self._values().get(name)
        if value and (len(value) > 512 or any(ord(c) < 33 or ord(c) == 127 or c.isspace() for c in value)):
            raise ControlError("SECRET_INPUT_INVALID")
        return value

    def active_secrets(self, references=()):
        names = {d["credential"] for d in DEFINITIONS.values() if d["credential"]} | {n for n in references if n} | {'OPENALEX_API_KEY'}
        values = self._values()
        protected = set(values.values()) | {os.environ[n] for n in names if os.environ.get(n)}
        if self.os_store and self.os_store.available:
            protected.update(v for n in names if (v := self.os_store.read(n)[0]))
        return tuple(protected)

    def metadata(self, name):
        if name is None: return {"configured": False, "active_source": "not_required"}
        try:
            stored, changed = self.os_store.read(name) if self.os_store and self.os_store.available else (None, None)
            file_value = self._values().get(name)
            active = "environment" if os.environ.get(name) else "os_store" if stored else "protected_file" if file_value else "unset"
            return {"configured": active != "unset", "active_source": active,
                    "storage_backend": "PROTECTED_PLAINTEXT" if file_value and not stored else "WINDOWS_CREDENTIAL_MANAGER" if self.os_store and self.os_store.available else "PROTECTED_PLAINTEXT",
                    "saved": bool(stored or file_value), "shadowed_saved_key": bool(os.environ.get(name) and (stored or file_value)),
                    "last_changed_at": changed or (datetime.fromtimestamp(self.file.stat().st_mtime, timezone.utc).isoformat() if file_value else None),
                    "provider_revoked": False}
        except (ControlError, OSError):
            return {"configured": bool(os.environ.get(name)), "active_source": "environment" if os.environ.get(name) else "file_unavailable",
                    "storage_backend": "UNAVAILABLE", "saved": None, "last_changed_at": None, "provider_revoked": False}

    def save(self, name: str, value: str | None):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", name) or (value is not None and
                (not value or len(value) > 512 or any(ord(c) < 33 or ord(c) == 127 or c.isspace() for c in value))):
            raise ControlError("SECRET_INPUT_INVALID")
        if value is not None:
            value.encode("utf-8", errors="strict")
        self._path_boundary(self.file)
        if self.os_store and self.os_store.available:
            from .secret_store import SecretStoreUnavailable
            previous_file_values = self._values()
            try:
                self.os_store.write(name, value)
            except SecretStoreUnavailable as exc:
                if exc.errno not in {1312, 50}:
                    raise
                self.os_store.available = False
                return self.save(name, value)
            try:
                found = self.os_store.read(name)[0]
            except SecretStoreUnavailable:
                raise ControlError("SECRET_READBACK_FAILED") from None
            if (value is None and found is not None) or (value is not None and (found is None or not secrets.compare_digest(found.encode("utf-8", errors="strict"), value.encode("utf-8", errors="strict")))):
                raise ControlError("SECRET_READBACK_FAILED")
            # 삭제 뒤 과거 파일 키가 다시 활성화되지 않도록 함께 제거한다.
            if previous_file_values.get(name):
                original = self.os_store
                try:
                    self.os_store = None
                    self.save(name, None)
                finally:
                    self.os_store = original
            return self.metadata(name)
        created = not self.file.parent.exists()
        self.file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created and os.name == "nt":
            sid = subprocess.run([shutil.which("pwsh") or "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                  "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
                                 capture_output=True, timeout=5, check=False, env=__import__("probe.sandbox", fromlist=["clean_environment"]).clean_environment()).stdout.decode().strip()
            if not re.fullmatch(r"S-1-5-[0-9-]+", sid):
                raise ControlError("SECRET_ACL_UNVERIFIED")
            secured = subprocess.run(["icacls", str(self.file.parent), "/inheritance:r", "/grant:r", "*" + sid + ":(OI)(CI)(F)", "*S-1-5-18:(OI)(CI)(F)"],
                                     capture_output=True, timeout=5, check=False, env=__import__("probe.sandbox", fromlist=["clean_environment"]).clean_environment())
            if secured.returncode:
                raise ControlError("SECRET_ACL_UNVERIFIED")
        self._secure(self.file.parent, True)
        values = self._values()
        if value is None:
            values.pop(name, None)
        else:
            values[name] = value
        data = "".join(f"{key}={item}\n" for key, item in values.items()).encode("utf-8", errors="strict")
        tmp = self.file.with_name(".secrets-" + secrets.token_hex(8))
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._secure(tmp)
            os.replace(tmp, self.file)
        finally:
            tmp.unlink(missing_ok=True)
        found = self._values().get(name)
        if (value is None and found is not None) or (value is not None and (found is None or not secrets.compare_digest(found.encode("utf-8", errors="strict"), value.encode("utf-8", errors="strict")))):
            raise ControlError("SECRET_READBACK_FAILED")
        return self.metadata(name)


# 정산·통신 중단은 과학 결과의 실패와 구분하고 사용자 재개를 허용한다.
REQUEST_CONTINUATION_CODES = frozenset({"TIMEOUT", "PROVIDER_TIMEOUT", "SERVER_UNAVAILABLE", "PROVIDER_ERROR",
    "USAGE_MISSING", "PRICE_CATEGORY_UNKNOWN", "SEARCH_USAGE_UNRESOLVED", "REQUEST_OUTCOME_UNKNOWN",
    "NEEDS_RECONCILIATION", "PROTOCOL_INCOMPATIBLE", "CONTEXT_LIMIT_BLOCKED"})


SCHEMA = """
CREATE TABLE IF NOT EXISTS control_schema(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO control_schema VALUES(1);
CREATE TABLE IF NOT EXISTS control_configs(kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS control_runs(research_id TEXT PRIMARY KEY,title TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 0,snapshot TEXT NOT NULL,created_at TEXT NOT NULL,started_at TEXT,pid INTEGER,error TEXT);
CREATE TABLE IF NOT EXISTS control_commands(key TEXT PRIMARY KEY,research_id TEXT NOT NULL,action TEXT NOT NULL,payload_hash TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS control_audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,research_id TEXT,kind TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spend_ledger(id TEXT PRIMARY KEY,research_id TEXT NOT NULL,connection_id TEXT NOT NULL,model TEXT NOT NULL,role TEXT NOT NULL,purpose TEXT NOT NULL,month TEXT NOT NULL,status TEXT NOT NULL,reserved INTEGER NOT NULL CHECK(reserved>=0),settled INTEGER,price_revision TEXT,created_at TEXT NOT NULL,response_id TEXT);
CREATE TABLE IF NOT EXISTS control_model_cache(key TEXT PRIMARY KEY,research_id TEXT NOT NULL,output_json TEXT,model TEXT NOT NULL,reservation_id TEXT NOT NULL,error TEXT);
"""


class ControlStore:
    def __init__(self, db: sqlite3.Connection, *, initialize_schema=True):
        self.db = db
        if not initialize_schema:
            return
        db.executescript("BEGIN IMMEDIATE;" + SCHEMA + "CREATE INDEX IF NOT EXISTS idx_runtime_research_seq ON runtime_events(research_id,seq DESC);CREATE INDEX IF NOT EXISTS idx_ui_evidence_experiment ON evidence(research_id,experiment_id);COMMIT;")

    @contextmanager
    def transaction(self):
        with transaction(self.db):
            yield

    def configs(self, kind):
        return [dict(json.loads(row["payload"]), revision=row["revision"]) for row in self.db.execute(
            "SELECT * FROM control_configs WHERE kind=? ORDER BY id", (kind,))]

    def config(self, kind, identity):
        row = self.db.execute("SELECT payload FROM control_configs WHERE kind=? AND id=?", (kind, identity)).fetchone()
        if row is None:
            raise ControlError("CONFIG_MISSING")
        return json.loads(row[0])

    def put(self, kind, identity, value, expected_revision=0, *, before_commit=None):
        payload = to_json(value)
        with self.transaction():
            old = self.db.execute("SELECT revision,payload FROM control_configs WHERE kind=? AND id=?", (kind, identity)).fetchone()
            if (old[0] if old else 0) != expected_revision:
                raise ControlError("CONFIG_STALE")
            self.db.execute("INSERT OR REPLACE INTO control_configs VALUES(?,?,?,?)", (kind, identity, expected_revision + 1, payload))
            self.audit(None, "CONFIG_UPDATED", {"kind": kind, "id": identity, "revision": expected_revision + 1,
                                                "old": json.loads(old[1]) if old else None, "new": json.loads(payload)})
            if before_commit is not None:
                before_commit()
        return {"revision": expected_revision + 1}

    def defaults(self):
        try:
            return Defaults.model_validate(self.config("defaults", "global"))
        except ControlError:
            return Defaults()

    def audit(self, rid, kind, payload):
        self.db.execute("INSERT INTO control_audit(research_id,kind,payload,created_at) VALUES(?,?,?,?)",
                        (rid, kind, to_json(payload), utc_now().isoformat()))

    def run(self, rid):
        row = self.db.execute("SELECT * FROM control_runs WHERE research_id=?", (rid,)).fetchone()
        if row is None:
            raise ControlError("RUN_NOT_CONTROLLED")
        value = dict(row)
        value["snapshot"] = json.loads(value["snapshot"])
        return value

    def ledger(self, rid=None, *, limit=None, offset=0, scope='all', role=None):
        if limit is not None and (not 1 <= limit <= 100 or not 0 <= offset <= 100000 or scope not in {'all','month','day'}):
            raise ControlError('PAGE_INVALID')
        where = ' WHERE research_id=?' if rid else ''
        params = (rid,) if rid else ()
        total = self.db.execute("SELECT COALESCE(SUM(CASE WHEN status='SETTLED' THEN settled ELSE 0 END),0),COALESCE(SUM(CASE WHEN status='UNRESOLVED' THEN reserved ELSE 0 END),0),COALESCE(SUM(CASE WHEN status NOT IN ('SETTLED','UNRESOLVED','RELEASED') THEN reserved ELSE 0 END),0) FROM spend_ledger"+where,params).fetchone()
        totals = dict(spent=total[0],unresolved=total[1],reserved=total[2])
        query, query_params = 'SELECT * FROM spend_ledger'+where, params
        if limit is not None:
            clauses = []
            if role:
                clauses.append('role=?');query_params += (role,)
            if scope == 'month':
                clauses.append('month=?');query_params += (utc_now().astimezone(ACCOUNTING_ZONE).strftime('%Y-%m'),)
            elif scope == 'day':
                clauses.append("date(created_at,'+9 hours')=?");query_params += (utc_now().astimezone(ACCOUNTING_ZONE).strftime('%Y-%m-%d'),)
            if clauses:
                query += (' AND ' if where else ' WHERE ')+' AND '.join(clauses)
            query += ' ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?'
            query_params += (limit+1,offset)
        else:
            query += ' ORDER BY created_at'
        rows = [dict(row) for row in self.db.execute(query, query_params)]
        next_offset = offset+limit if limit is not None and len(rows)>limit else None
        if limit is not None:
            rows = rows[:limit]
        month = utc_now().astimezone(ACCOUNTING_ZONE).strftime("%Y-%m")
        exposure = "CASE WHEN status='SETTLED' THEN settled WHEN status='RELEASED' THEN 0 ELSE reserved END"
        monthly = self.db.execute(f"SELECT COALESCE(SUM({exposure}),0) FROM spend_ledger WHERE month=? OR status IN ('RESERVED','DISPATCHED','UNRESOLVED')", (month,)).fetchone()[0]
        monthly_limit = micro(self.defaults().monthly_limit_usd)
        remaining = max(0, monthly_limit - monthly)
        if rid:
            run = self.db.execute("SELECT snapshot FROM control_runs WHERE research_id=?", (rid,)).fetchone()
            if run:
                from .product_policy import effective_cap, effective_snapshot
                snapshot = effective_snapshot(self, rid, json.loads(run[0]))
                remaining = min(remaining, max(0, micro(effective_cap(self, rid, snapshot)) - sum(totals.values())),
                                max(0, micro(snapshot.get("monthly_limit_usd", self.defaults().monthly_limit_usd)) - monthly))
        return {"currency": "USD", "timezone": "Asia/Seoul", **{k: money(v) for k, v in totals.items()}, "available": money(remaining), "month": month,
                "monthly_exposure": money(monthly), "requests": rows, 'offset':offset,'next_offset':next_offset,'limit':limit,
                "billing_scope": "이 앱의 예약 원장만 포함 · 제공사 청구서와 다릅니다."}

    def reserve(self, *, rid, connection, model, role, purpose, bound, run_limit, monthly_limit, request_limit,
                attempts, revision, completion_reserve=0, run_concurrency=1, request_key=None):
        now = utc_now()
        month = now.astimezone(ACCOUNTING_ZONE).strftime("%Y-%m")
        amount = micro(bound)
        if amount > micro(request_limit):
            raise ControlError("REQUEST_BUDGET_BLOCKED")
        with self.transaction():
            exposure = "CASE WHEN status='SETTLED' THEN settled WHEN status='RELEASED' THEN 0 ELSE reserved END"
            effective = self.db.execute("SELECT payload FROM control_configs WHERE kind='research_effective' AND id=?", (rid,)).fetchone()
            if effective:
                run_limit = json.loads(effective[0])["settings"]["run_limit_usd"]
            pending = self.db.execute("SELECT payload FROM control_configs WHERE kind='research_settings' AND id=?", (rid,)).fetchone()
            if pending and not json.loads(pending[0])["applied"]:
                run_limit = min(Decimal(str(run_limit)), Decimal(json.loads(pending[0])["settings"]["run_limit_usd"]))
            run_spend = self.db.execute(f"SELECT COALESCE(SUM({exposure}),0),SUM(CASE WHEN purpose='web_search' AND reserved=0 THEN 0 ELSE 1 END) FROM spend_ledger WHERE research_id=?", (rid,)).fetchone()
            # 미정산 노출은 월이 바뀌어도 유지한다.
            month_spend = self.db.execute(f"SELECT COALESCE(SUM({exposure}),0) FROM spend_ledger WHERE month=? OR status IN ('RESERVED','DISPATCHED','UNRESOLVED')", (month,)).fetchone()[0]
            free_search = purpose == 'web_search' and role == 'search' and amount == 0
            if not free_search and ((run_spend[1] or 0) >= attempts or run_spend[0] + amount > micro(run_limit) or month_spend + amount > micro(monthly_limit)):
                raise ControlError("BUDGET_BLOCKED")
            if not free_search and (run_spend[0] + amount + micro(completion_reserve) > micro(run_limit) or month_spend + amount + micro(completion_reserve) > micro(monthly_limit)):
                raise ControlError("COMPLETION_RESERVE_BLOCKED")
            claim_id = None
            if request_key:
                epoch = self.db.execute("SELECT payload FROM control_configs WHERE kind='request_epoch' AND id=?", (rid,)).fetchone()
                epoch = json.loads(epoch[0])["value"] if epoch else "initial"
                claim_id = hashlib.sha256(to_json({"rid": rid, "key": request_key, "epoch": epoch}).encode("utf-8", errors="strict")).hexdigest()
                old = self.db.execute("SELECT l.status FROM control_configs c JOIN spend_ledger l ON l.id=json_extract(c.payload,'$.reservation_id') WHERE c.kind='provider_request' AND c.id=?", (claim_id,)).fetchone()
                if old and old[0] != "RELEASED":
                    raise ControlError("REQUEST_OUTCOME_UNKNOWN" if old[0] == "UNRESOLVED" else "REQUEST_ALREADY_COMPLETED" if old[0] == "SETTLED" else "REQUEST_IN_FLIGHT")
            # 다른 과거 요청의 미정산 노출은 위 예산 합산으로 보호한다.
            capacity = min(2, max(1, run_concurrency))
            active_states = "('DISPATCHED')" if capacity == 1 else "('RESERVED','DISPATCHED')"
            active_run = self.db.execute(f"SELECT COUNT(*) FROM spend_ledger WHERE research_id=? AND status IN {active_states}", (rid,)).fetchone()[0]
            if active_run >= capacity:
                raise ControlError("REQUEST_IN_FLIGHT")
            active_connection = self.db.execute("SELECT COUNT(*),MAX(CASE WHEN research_id<>? THEN 1 ELSE 0 END) FROM spend_ledger WHERE connection_id=? AND status IN ('RESERVED','DISPATCHED')", (rid, connection)).fetchone()
            if active_connection[0] >= capacity or active_connection[1]:
                raise ControlError("SERVER_BUSY")
            identity = new_id("RSV")
            self.db.execute("INSERT INTO spend_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (identity, rid, connection, model, role, purpose, month, "RESERVED", amount, None, revision, now.isoformat(), None))
            if claim_id:
                value = {"research_id": rid, "request_key": request_key, "epoch": epoch, "reservation_id": identity}
                self.db.execute("INSERT OR REPLACE INTO control_configs VALUES('provider_request',?,1,?)", (claim_id, to_json(value)))
        return identity

    def dispatch_search(self, reservation_id, rid, limit, provider, *, snapshot=None, completion_reserve=0):
        """전송 직전 모든 역할·재시도에 공유되는 시도 수를 원자적으로 기록한다."""
        with self.transaction():
            row = self.db.execute("SELECT status,research_id FROM spend_ledger WHERE id=?", (reservation_id,)).fetchone()
            if not row or row["research_id"] != rid or row["status"] not in {"RESERVED", "DISPATCHED"}:
                raise ControlError("SEARCH_RESERVATION_INVALID")
            if snapshot is not None:
                from .product_policy import effective_cap
                current = self.defaults()
                exposure = "CASE WHEN status='SETTLED' THEN settled WHEN status='RELEASED' THEN 0 ELSE reserved END"
                run_spend = self.db.execute(f"SELECT COALESCE(SUM({exposure}),0) FROM spend_ledger WHERE research_id=?", (rid,)).fetchone()[0]
                month = utc_now().astimezone(ACCOUNTING_ZONE).strftime("%Y-%m")
                month_spend = self.db.execute(f"SELECT COALESCE(SUM({exposure}),0) FROM spend_ledger WHERE month=? OR status IN ('RESERVED','DISPATCHED','UNRESOLVED')", (month,)).fetchone()[0]
                amount = self.db.execute("SELECT reserved FROM spend_ledger WHERE id=?", (reservation_id,)).fetchone()[0]
                if amount > micro(min(current.request_limit_usd, Decimal(snapshot['request_limit_usd']))):
                    raise ControlError('REQUEST_BUDGET_BLOCKED')
                if (run_spend + micro(completion_reserve) > micro(effective_cap(self, rid, snapshot))
                        or month_spend + micro(completion_reserve) > micro(min(current.monthly_limit_usd, Decimal(snapshot['monthly_limit_usd'])))):
                    raise ControlError('COMPLETION_RESERVE_BLOCKED')
            limit = min(limit, self.defaults().search_attempt_limit)
            for kind in ("research_effective", "research_settings"):
                policy = self.db.execute("SELECT payload FROM control_configs WHERE kind=? AND id=?", (kind, rid)).fetchone()
                if policy:
                    requested = json.loads(policy[0])["settings"].get("search_attempt_limit")
                    if requested is not None:
                        limit = min(limit, requested)
            old = self.db.execute("SELECT revision,payload FROM control_configs WHERE kind='search_attempts' AND id=?", (rid,)).fetchone()
            used = json.loads(old["payload"])["used"] if old else 0
            if type(used) is not int or used < 0:
                raise ControlError("SEARCH_COUNTER_INVALID")
            if used >= limit:
                raise ControlError("SEARCH_ATTEMPT_LIMIT")
            value = {"used": used + 1, "limit": limit, "last_dispatch_at": utc_now().isoformat()}
            self.db.execute("INSERT OR REPLACE INTO control_configs VALUES('search_attempts',?,?,?)",
                            (rid, old["revision"] + 1 if old else 1, to_json(value)))
            self.db.execute("UPDATE spend_ledger SET status='DISPATCHED' WHERE id=? AND status='RESERVED'", (reservation_id,))
            self.audit(rid, "SEARCH_TRANSPORT_DISPATCHED", {**value, "provider": provider, "reservation_id":reservation_id})
        return value

    def transition(self, identity, status, *, settled=None, response_id=None):
        allowed = {"DISPATCHED": {"RESERVED"}, "RELEASED": {"RESERVED"}, "UNRESOLVED": {"DISPATCHED"}, "SETTLED": {"DISPATCHED"}}
        with self.transaction():
            row = self.db.execute("SELECT * FROM spend_ledger WHERE id=?", (identity,)).fetchone()
            if row is None or row["status"] not in allowed[status]:
                raise ControlError("LEDGER_TRANSITION_INVALID")
            amount = micro(settled) if settled is not None else None
            if status == "SETTLED" and (amount is None or amount > row["reserved"]):
                raise ControlError("USAGE_BOUND_EXCEEDED")
            self.db.execute("UPDATE spend_ledger SET status=?,settled=?,response_id=? WHERE id=?", (status, amount, response_id, identity))

    def recover_ledger(self, rid):
        with self.transaction():
            # 전송 직전 충돌한 예약도 불확실한 과금 노출로 남긴다.
            self.db.execute("UPDATE spend_ledger SET status='UNRESOLVED' WHERE research_id=? AND status IN ('RESERVED','DISPATCHED')", (rid,))

    def settle_output(self, identity, request_key, model, output, amount, error=None, response_id=None, trace=None):
        value = micro(amount)
        with self.transaction():
            row = self.db.execute("SELECT * FROM spend_ledger WHERE id=?", (identity,)).fetchone()
            if row is None or row["status"] != "DISPATCHED" or value > row["reserved"]:
                raise ControlError("USAGE_BOUND_EXCEEDED")
            self.db.execute("UPDATE spend_ledger SET status='SETTLED',settled=?,response_id=? WHERE id=?", (value, response_id, identity))
            self.db.execute("INSERT INTO control_model_cache VALUES(?,?,?,?,?,?)",
                            (request_key, row["research_id"], to_json(output) if output is not None else None, model, identity, error))
            if trace is not None: self.audit(row["research_id"], "NORMALIZED_RESPONSE_SETTLED", trace)


def admitted_cost(profile: ModelProfile, byte_count: int):
    if byte_count > profile.input_byte_limit or byte_count + profile.output_limit > profile.context_limit:
        raise ControlError("CONTEXT_LIMIT_BLOCKED")
    price = profile.price
    if price is None or not price.owner_verified or price.checked_at.tzinfo is None or not -60 <= (utc_now() - price.checked_at.astimezone(timezone.utc)).total_seconds() <= 30 * 86400:
        raise ControlError("PRICE_REQUIRED")
    # UTF-8 바이트에 프로토콜 여유를 더한 보수적 입력 상한이다.
    
    input_rate = max(price.input_per_million, price.cached_input_per_million or 0, price.cache_write_per_million or 0)
    return (Decimal(byte_count) * input_rate + Decimal(profile.output_limit) * price.output_per_million) / 1000000


def safe_source(root: Path, relative: str) -> Path:
    candidate = root / relative
    if Path(relative).is_absolute() or not candidate.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (candidate, *candidate.parents)):
        raise ControlError("SOURCE_PATH_UNSAFE")
    if candidate.suffix.lower() != ".csv" or not candidate.is_file() or candidate.stat().st_size > 5000000:
        raise ControlError("SOURCE_INVALID")
    data = candidate.read_bytes()
    data.decode("utf-8", errors="strict")
    if b"OPENAI_API_KEY" in data or re.search(rb"sk-[A-Za-z0-9_-]{12,}", data):
        raise ControlError("SECRET_IN_SOURCE")
    return candidate


def source_snapshot(root: Path, relative: str):
    path = safe_source(root, relative)
    return {"relative_path": relative, "sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def classify_http(status: int, body: dict | None = None) -> str:
    code = str((body or {}).get("error", {}).get("code", "")) if isinstance((body or {}).get("error"), dict) else ""
    if code in {"insufficient_quota", "billing_hard_limit_reached", "spend_limit_exceeded"}:
        return "PROVIDER_SPEND_LIMIT"
    return {401: "PROVIDER_AUTH", 403: "PROVIDER_PERMISSION", 404: "MODEL_NOT_FOUND", 429: "PROVIDER_RATE_LIMIT",
            400: "PROVIDER_PARAMETER", 503: "SERVER_UNAVAILABLE"}.get(status, "PROVIDER_ERROR")


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def process_identity(pid):
    """같은 PID의 다른 실행을 구별할 수 있는 운영체제 시작 값을 읽는다."""
    if not pid:
        return None
    try:
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                values = [ctypes.c_ulonglong() for _ in range(4)]
                if kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                    return str(values[0].value)
            finally:
                kernel.CloseHandle(handle)
        else:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            return fields[19]
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    return None


def process_owned(pid, identity=None):
    if not pid or not process_alive(pid):
        return False
    observed = process_identity(pid) if identity else None
    return not identity or observed is None or observed == identity


def worker_alive(store, rid, pid):
    row = store.db.execute("SELECT payload FROM control_configs WHERE kind='worker_owner' AND id=?", (rid,)).fetchone()
    if not row:
        return bool(pid and process_alive(pid))
    owner = json.loads(row[0])
    if pid:
        return process_owned(pid, owner.get("worker_birth"))
    if owner.get("worker_pid") and process_owned(owner["worker_pid"], owner.get("worker_birth")):
        return True
    age = (utc_now() - datetime.fromisoformat(owner["created_at"])).total_seconds()
    return owner.get("status") == "STARTING" and 0 <= age < 45 and process_owned(owner.get("owner_pid"), owner.get("owner_birth"))
