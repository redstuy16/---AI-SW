"""문서·설명 주석의 한국어 변경을 UTF-8 검사 후 적용한다."""
from pathlib import Path
import ast
import io
import json
import re
import tokenize


ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    "agent_cli":"명시적으로 활성화한 Agent 실행·복구 명령.","agent_context":"역할별 제한된 문맥을 구성한다.",
    "agent_policy":"결정적 계약·예산·상위 판단 정책.","agent_runtime":"고정 도구와 StateService를 사용하는 제한된 Agent 실행.",
    "agent_schemas":"잠정 출력과 상위 판단의 구조화 스키마.","analysis_skills":"버전과 범위가 고정된 수치 분석. 모델 출력을 코드로 실행하지 않는다.",
    "api":"읽기 전용 대시보드 API의 호환 진입점.","autonomous_loop":"가설·실험·비판·후속 실험의 제한된 연구 순환.",
    "context_compiler":"정본·계획 기록에서 예산에 맞는 문맥을 검색한다.","control_plane":"기존 SQLite의 소유자 제어·지출 원장. 금액은 보수적으로 반올림한 정수 micro-USD다.",
    "control_runtime":"확정된 정책과 지속되는 요청 원장을 사용하는 기존 연구 실행기.","dashboard":"StateService와 검증된 산출물을 조회하는 읽기 전용 대시보드. 브라우저에 DB 쓰기를 노출하지 않는다.",
    "database":"SQLite 초기화와 결정적 JSON 직렬화.","demo":"Day 4B 기능 동결을 위한 결정적 오프라인 데모.",
    "final_report":"검증된 기록의 참조를 해석한 최종 연구 보고서.","generate_schemas":"Pydantic 모델에서 JSON Schema를 생성한다.",
    "literature":"보수적인 문헌 선별·인용 검증·종합.","literature_runtime":"기존 자율 실험 순환에 연결하는 Day 4A 문헌 검토.",
    "mock":"실제 인터페이스를 사용하는 결정적 모의 Agent와 도구.","preflight":"환경 검사와 명시적으로 요청한 출시 검증.",
    "real_runtime":"로컬 CSV를 신뢰할 수 있는 통계 도구로 분석하고 검증 후 정본에 반영한다.","real_schemas":"로컬 데이터·과학 도구의 구조화 계약.",
    "real_tools":"등록된 결정적 도구. 결과는 검증 전 대기 상태로 기록한다.","recovery":"지속되는 재개 정보와 명시적 충돌 주입 경계.",
    "release":"종료된 연구의 출시 후보 내보내기.","reliability":"단일 종합 점수 없이 오프라인 비교와 원시 건수를 기록한다.",
    "research_schemas":"계획·비판·자율 순환 기록의 스키마.","research_slice":"기존 StateService·SQLite·의존 그래프의 선택적 확장. 검증된 필드·인용 연결만 제한된 의미 지원을 제공한다.",
    "research_slice_schemas":"실행 검증·과학적 참과 구분되는 주장의 의미 출처.","research_tree":"저장된 연구 그래프의 읽기 전용 투영.",
    "runtime":"네트워크와 모델 호출이 없는 첫 결정적 실행 경로.","sandbox":"신뢰하지 않는 Python은 Docker에서만 실행한다. 호스트 실행 대체 경로는 없다.",
    "schemas":"외부 데이터 스키마의 기준은 Pydantic이다.","scholarly":"제한된 문헌 검색. 제공사 텍스트는 신뢰하지 않는 자료다.",
    "scientific_verifier":"잠정 과학 결과와 파일 출처를 독립적으로 검사한다.","service":"저장된 연구 상태의 유일한 쓰기 경계.",
    "storage":"연구 범위 안의 경로 해석과 해시 계산.","verification_repair":"기존 상태 서비스를 사용하는 선택적·제한적 검증 복구.",
    "workbench":"동일 출처의 한국어 연구 작업대. python -m probe.workbench로 실행한다.",
    "base":"제공사와 독립적인 구조화 모델 실행.","fake":"실행 순서 검증에 사용하는 결정적 모의 제공사.",
    "native":"기존 HTTP·예산·외부 전송 경계의 단일 어댑터 등록소. 도구 실행과 정본 쓰기는 Probe가 담당한다.",
    "normalized":"정본 상태와 자격 증명을 포함하지 않는 공통 생성 계약.","openai_agents":"OpenAI Agents SDK 어댑터. SDK 객체는 이 경계를 넘지 않는다.",
    "__main__":"출시·대시보드 명령의 진입점.","__init__":"Probe 구성 요소.",
    "f3p_eval":"기존 실행기와 QA 도구의 별도 F3-P 합성 오류 평가.","f3p_export_probe":"F3-P 내보내기와 비밀 검사용 값의 유출 차단을 검증한다.",
    "f3p_live_ablation":"실제 Agent 쌍 비교의 기록 수집. 결과 판정은 별도 수행한다.","f3p_recovery_probe":"새 프로세스에서 F3-P 충돌·복구를 검사한다.",
    "gui_lowest_depth_probe":"최소 연구 깊이에서도 필수 검사와 미해결 비판을 보존한다.","gui_visual_fixture":"별도의 DEMO 작업 공간에 실제 오프라인 복구 기록을 만든다.",
    "gui_worker_probe":"실제 작업 프로세스와 로컬 모의 HTTP를 검사한다. 실서비스 모델 검증은 아니다.",
    "literature_tamper_probe":"복사한 종료 연구의 DOI·출처 연결을 변조하여 검사한다.","performance_smoke":"연구 상태를 바꾸지 않고 제한된 조회·재시도 시간을 측정한다.",
    "qa_day1":"동결된 Day 1 파괴적 검사를 수행하고 기계 판독 결과를 저장한다.","reliability_lab":"기존 실행기와 고정 정답 자료의 선택적 오프라인 쌍 비교.",
    "research_slice_export_probe":"실제 실행의 정상·비밀 값·변조·오래된 상태 내보내기를 검사한다.",
    "research_slice_recovery_probe":"기존 F3-P 자료와 실행기의 새 프로세스 충돌 검사.","secret_scan":"출시·추적·Agent·도구·대시보드의 비밀 검사용 값 유출을 검사한다.",
    "verified_analysis_eval":"개발 테스트와 분리한 오프라인 검증 사례.",
}
DOCS = {
    "A known next call would exceed the hard research budget.":"다음 요청의 알려진 비용이 연구의 최대 예산을 넘는다.",
    "Mandatory context does not fit; no required reference is discarded.":"필수 문맥이 한도를 넘는다. 필수 참조는 버리지 않는다.",
    "Cooperative checkpoint stop; never treated as a provider retry.":"안전한 저장 경계에서 중단한다. 제공사 재시도로 취급하지 않는다.",
    "Resolve once, validate every address, connect to the pinned IP with TLS SNI.":"DNS 주소를 검사하고 고정 IP에 TLS SNI로 연결한다. 인증과 요청은 승인된 출처를 벗어나지 않는다.",
    "A single backend broker for every GUI model attempt, including settings.":"GUI·설정의 모든 모델 요청을 중개한다. 불확실한 사용량·수락 상태는 원장에 남기고 자동 재요청하지 않는다.",
    "Build a tree-shaped view from relations; no separate tree truth is stored.":"기존 관계에서 트리 화면을 만든다. 별도의 정본 트리는 저장하지 않는다.",
    "Return the small desktop-first dashboard shell used by the demo server.":"데모 서버의 대시보드 화면을 반환한다.",
    "Projection API used by tests, the demo shell, and the optional HTTP server.":"테스트·데모·HTTP 서버에서 사용하는 읽기 API.",
    "A versioned SQLite migration failed atomically.":"SQLite 마이그레이션이 실패하여 원자적으로 되돌려졌다.",
    "The known-good fixture did not satisfy its manifest.":"고정 검증 자료가 선언된 검사 조건을 충족하지 못했다.",
    "The proposed report cannot be resolved to verified records.":"보고서의 주장을 검증된 기록에 연결할 수 없다.",
    "Narrow semantic gate: accept only a literal sentence with explicit polarity cues.":"명시적인 지원·반박 표현이 있는 원문 문장만 받는다.",
    "Use a budgeted structured model call before deterministic source verification.":"결정적 출처 검증 전에 예산 안에서 구조화 모델 검토를 수행한다.",
    "Map provider failures to dashboard-safe readiness states.":"제공사 실패를 대시보드에 안전한 준비 상태로 변환한다.",
    "Read the opt-in scholarly validation marker without making a network call.":"네트워크 요청 없이 문헌 검증 표식을 읽는다.",
    "A logical tool is unknown or not in the contract allowlist.":"등록되지 않았거나 계약에서 허용하지 않은 도구다.",
    "A tool request cannot produce a valid result.":"도구 요청으로 유효한 결과를 만들 수 없다.",
    "One charged Tool call around a fixed, trusted numerical procedure.":"고정된 수치 절차를 도구 호출 하나로 실행한다.",
    "Logical dispatch; duplicate idempotency keys are rejected, including failed calls.":"논리 도구 실행. 실패한 호출을 포함해 중복 요청 키를 거절한다.",
    "Simulates process termination without normal exception recovery.":"일반 예외 복구 없이 프로세스 종료를 모사한다.",
    "A release could not be assembled without unsafe or unresolved files.":"안전하지 않거나 참조가 해석되지 않은 파일 때문에 내보내기를 중단한다.",
    "Escape display CSV only; retain the byte-identical research archive.":"표시용 CSV만 이스케이프하고 원본 바이트는 보존한다.",
    "Include portable replay artifacts only for opted-in, verified Skill runs.":"명시적으로 활성화하고 검증한 Skill의 재현 산출물만 포함한다.",
    "Export one terminal research run without credentials or workspace escapes.":"자격 증명을 제외하고 연구 범위 안에서 종료된 연구를 내보낸다.",
    "Composed writer: all mutations are called through StateService.":"모든 변경은 StateService를 통해 호출한다.",
    "Docker is absent or its daemon cannot be reached.":"Docker가 없거나 데몬에 연결할 수 없다.",
    "Docker CLI needs OS/runtime paths, never provider credentials.":"Docker CLI에 운영체제 경로만 전달하고 제공사 자격 증명은 제외한다.",
    "A provider response could not be trusted or retrieved within policy.":"정책 안에서 신뢰할 수 있는 제공사 응답을 가져오지 못했다.",
    "The staged base version or other commit precondition changed.":"잠정 결과의 기준 버전 또는 반영 전제 조건이 바뀌었다.",
    "The mutation has no passing verification.":"변경에 대한 통과 검증이 없다.",
    "The requested transition is invalid for this mutation status.":"현재 변경 상태에서 허용되지 않은 전이다.",
    "The mutation was committed already.":"이미 반영한 변경이다.",
    "A required persisted entity does not exist.":"필요한 저장 기록이 없다.",
    "Execution or task metadata violates its contract.":"실행·작업 정보가 계약을 위반한다.",
    "An idempotency key has already been reserved.":"이미 예약한 중복 방지 키다.",
    "The same bounded action was attempted three times.":"동일한 제한 작업을 세 번 시도했다.",
    "The configured action cap was reached.":"설정한 작업 수 상한에 도달했다.",
    "One synchronous writer; callers never receive a mutable database handle.":"쓰기 담당자는 하나이며 호출자에게 변경 가능한 DB 연결을 제공하지 않는다.",
    "Persist operational task/contract metadata before result commits.":"결과 반영 전에 운영 작업·계약 정보를 저장한다.",
    "Store an execution trace. This is not a canonical result.":"실행 추적을 저장한다. 정본 연구 결과로 취급하지 않는다.",
    "Atomically validate/write a typed planning change and advance research version.":"계획 변경을 원자적으로 검증·저장하고 연구 버전을 올린다.",
    "A path escapes its research workspace.":"경로가 해당 연구의 작업 공간을 벗어난다.",
    "A stored dataset no longer matches its registered digest.":"데이터가 등록된 해시와 일치하지 않는다.",
    "An artifact no longer matches its registered digest.":"산출물이 등록된 해시와 일치하지 않는다.",
    "Independent NumPy reference for the two fixed association methods only.":"고정된 두 연관 분석에만 NumPy 참조값을 사용한다. 제한된 수치 비교이며 과학적 타당성 증명은 아니다.",
    "Immutable, hash-registered evidence; never a canonical scientific result.":"변경 불가·해시 등록된 실패 근거. 정본 과학 결과가 아니다.",
    "Check the v1 augmented design with an unpenalized intercept and alpha=1.":"벌점 없는 절편·alpha=1인 v1 설계를 검사한다. 정규방정식·절편 잔차를 정규화하고 float64 허용오차 1e-10을 적용한다. 불량 조건수·비유한 값은 참조 검사가 필요하다.",
    "Fixed public codes only: never interpolate provider body/header secrets.":"고정된 공개 오류 코드만 사용하고 응답·헤더의 비밀을 넣지 않는다.",
    "A failed live test cannot be erased by static/metadata assumptions.":"실제 검사 실패를 정적 규칙·메타데이터 추정으로 지우지 않는다.",
}
COMMENTS = {
    "# A demo or embedding application may provide a larger read-only context":"# 데모·내장 호출은 정본 계약을 유지하며 읽기 문맥 예산만 늘릴 수 있다.",
    "# budget without changing the canonical contract or default policy.":"",
    "# All historical unresolved liabilities carry across month boundaries.":"# 미정산 노출은 월이 바뀌어도 유지한다.",
    "# RESERVED is also uncertain after a crash just before durable dispatch.":"# 전송 직전 충돌한 예약도 불확실한 과금 노출로 남긴다.",
    "# UTF-8 byte count is a conservative admitted-input token bound for this":"# UTF-8 바이트에 프로토콜 여유를 더한 보수적 입력 상한이다.",
    "# protocol; framing/schema allowance is included by the gateway caller.":"",
    "# Cursors already persist at existing safe action boundaries. Never":"# 안전 경계에서 중단하며 실행 중인 도구와 전송된 과금 노출은 보존한다.",
    "# cancel a synchronous tool halfway or erase a dispatched liability.":"",
    "# A test edge is stored experiment -> hypothesis, but the visual tree is hypothesis -> experiment.":"# 저장 관계는 실험→가설이며 화면은 가설→실험으로 표시한다.",
    "# Markdown is displayed as escaped preformatted text.  Raw HTML and scripts are data.":"# Markdown을 이스케이프하여 표시하고 HTML·스크립트는 실행하지 않는다.",
    "# Stable names for embedding applications and small test clients.":"",
    "# SQLite is intentionally opened with the repository's synchronous":"# 기존 동기식 SQLite 정책에 따라 서버는 단일 스레드로 실행한다.",
    "# connection policy, so the small dashboard server stays single-threaded.":"",
    "# Concurrent validation commands must not remove another run's fixtures.":"# 병행 검증에서 다른 실행의 자료를 삭제하지 않는다.",
    "# The manifest is intentionally not self-referential; every other exported":"# 선언 파일 자체의 해시는 별도로 반환한다.",
    "# file has a digest and the caller receives the manifest digest separately.":"",
    "# Like the opt-in control-plane migration, preserve the six core migrations.":"# 기존 핵심 마이그레이션 6개는 유지한다.",
    "# Existing source infrastructure currently supports retrieved abstracts only.":"# 현재 출처 검증은 수집한 초록만 지원한다.",
    "# An omitted difficult numeric slot cannot improve coverage.":"# 어려운 수치 항목을 생략해도 검증 비율이 좋아지지 않게 한다.",
    "# Traverse the existing dependency graph; semantic relations are not derivations.":"# 기존 의존 그래프를 순회한다. 의미 관계를 도출 근거로 취급하지 않는다.",
    "# Integrity errors also revoke current support; never resurrect stale bindings.":"# 무결성 오류는 현재 지원을 취소하며 오래된 연결을 되살리지 않는다.",
    "# Read projections fail closed even if external tampering bypassed the writer.":"# 외부 변조로 쓰기 경계를 우회해도 조회 시 무결성을 검사한다.",
    "# Defaults cannot rewrite the explicitly selected depth.":"# 기본값으로 사용자가 선택한 깊이를 바꾸지 않는다.",
    "# Broker process is trusted; analysis tools/containers get no key mounts.":"# 분석 도구·컨테이너에 키를 마운트하지 않는다.",
    "# Consume a bounded valid body before closing a rejected request. Windows":"# Windows에서 403 전달 전 연결이 초기화되지 않도록 제한된 본문을 읽는다.",
    "# otherwise may reset the connection before the client receives the 403.":"",
    "# Read only known code/type scalars, never return provider messages.":"# 알려진 오류 코드만 읽고 제공사 메시지는 노출하지 않는다.",
    "# Exact provider continuity is private protocol state, never public trace.":"# 정확한 프로토콜 연속성은 비공개 상태로만 보존한다.",
    "# Set before importing/initializing SDK agents or its default trace exporter.":"# SDK 초기화 전에 외부 추적을 끈다.",
    "# Existing two-argument runner integrations retain their call shape.":"# 기존 인자 2개의 실행기 호출 형태를 유지한다.",
    "# Decide before dispatch; a TypeError must never cause a paid retry.":"# 전송 전 호출 형태를 결정하며 TypeError로 유료 요청을 반복하지 않는다.",
    "# The oracle is used only after runtime completion by the evaluator.":"# 정답 기준값은 실행 완료 후 평가기만 사용한다.",
    "# Use a fresh task identity and directory for every evaluation invocation.":"# 평가마다 새 작업 ID와 디렉터리를 사용한다.",
    "# Missing pricing must remain unknown rather than becoming a zero-cost claim.":"# 가격 누락을 무료로 취급하지 않는다.",
    "# Direct worker CLI retains the same unpriced paid-dispatch boundary.":"# 직접 작업 CLI에도 같은 가격 확인 경계를 적용한다.",
    "# Research identity counters reset per paired trial. Full original contracts,":"# 쌍 비교마다 ID를 초기화해 계약·계획·데이터를 맞추고 디렉터리는 분리한다.",
    "# plans and dataset IDs are identical across arms; directories remain isolated.":"",
    "# Required names come from the manifest, independent of emitted claims.":"# 필수 항목은 생성된 주장과 독립적인 선언 파일에서 가져온다.",
    "# Closing an interrupted connection rolls back an uncommitted transaction.":"# 중단된 연결을 닫으면 미반영 트랜잭션이 되돌려진다.",
    "# Replay of the same invalidation cannot append another revision.":"# 같은 무효화 요청의 재실행으로 수정본을 중복 생성하지 않는다.",
}


def apply():
    changed = []
    for folder in ("src", "qa", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            if "__pycache__" in path.parts or path.resolve() == Path(__file__).resolve(): continue
            original = path.read_text(encoding="utf-8")
            tree = ast.parse(original)
            edits = []
            lines = original.splitlines(keepends=True)
            for node in ast.walk(tree):
                if not isinstance(node,(ast.Module,ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)): continue
                doc = ast.get_docstring(node,clean=False)
                if not doc or re.search(r"[가-힣]",doc): continue
                first = doc.strip().splitlines()[0]
                translated = DOCS.get(first)
                if isinstance(node,ast.Module):
                    translated = "기존 동작과 검증 경계를 확인하는 회귀 테스트." if path.parent.name == "tests" else MODULES.get(path.stem,translated)
                if not translated: raise ValueError(f"주석 번역 누락: {path}:{first}")
                expression = node.body[0]
                edits.append((expression.lineno-1,expression.end_lineno,' ' * expression.col_offset + '"""'+translated+'"""\n'))
            for start,end,value in sorted(edits,reverse=True): lines[start:end]=[value]
            text = ''.join(lines)
            for token in list(tokenize.generate_tokens(io.StringIO(text).readline)):
                if token.type != tokenize.COMMENT: continue
                comment = token.string
                if comment in COMMENTS: text = text.replace(comment,COMMENTS[comment])
            if text != original:
                data = text.encode("utf-8",errors="strict")
                ast.parse(text)
                path.write_bytes(data)
                changed.append(str(path.relative_to(ROOT)))
    print(json.dumps({"changed":changed},ensure_ascii=True))


if __name__ == "__main__": apply()
