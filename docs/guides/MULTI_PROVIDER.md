# 다중 제공사 API·모델 설정 v1

기존 작업대·작업 CLI의 `RoutedGateway`가 하나의 공통 생성 계약과 제공사 등록소를 사용한다. 새 연구 엔진이나 자동 대체 경로는 추가하지 않는다.

## 실행과 활성화

```powershell
# 저장소 폴더에서 실행
.\.venv\Scripts\python.exe -m htrsa.workbench build/workbench/state.sqlite build/workbench/workspace
```

설정 → **API 연결**에서 제공사·API 키를 함께 저장한 뒤 **모델**에서 사용할 모델과 연결을 선택한다. 상단 5개와 접힌 제공사별 더보기를 제공하며 추천·속도·성능 배지는 표시하지 않는다. 직접 모델 ID와 역할별 구성은 **고급 설정**, 실제 기능 검사는 **API 검사**에 있다. 저장은 모델 API를 호출하지 않으며 검사에는 비용 동의가 필요하다. 기존 Agent CLI의 OpenAI Agents SDK 경로도 유지한다.

새 연결의 키 참조는 연결별로 자동 생성한다. 아래 표의 환경변수는 기존 기본 연결에서 계속 사용할 수 있다. 공식 주소는 자동 설정하고 고급 항목에 숨긴다. 로컬 서버는 loopback 주소로 시작하지만 외부 전달 차단이 검증된 것으로 표시하지 않는다.

2026-10-02 공식 문서 확인 모델은 `src/htrsa/product_catalog.json`에 기록했다. Gemini 3.8 Flash 단가는 확인되지 않아 자동으로 입력하지 않는다. 확인한 가격을 등록하기 전 유료 요청은 기존 `PRICE_REQUIRED` 기준으로 차단한다.

| 제공사 | 기본 프로토콜 | 자격 증명 참조 |
| --- | --- | --- |
| OpenAI | Responses | `OPENAI_API_KEY` |
| Anthropic | Messages | `ANTHROPIC_API_KEY` |
| Google Gemini | 안정판 Interactions, 명시적 generateContent 호환 경로 | `GEMINI_API_KEY` |
| xAI | Responses, 명시적 Chat | `XAI_API_KEY` |
| DeepSeek | Chat | `DEEPSEEK_API_KEY` |
| Mistral | Chat | `MISTRAL_API_KEY` |
| 제한된 OpenAI 호환 서버 | 명시적 Chat/Responses | 사용자 참조, 로컬 기본 인증 없음 |

공식 제공사 주소·인증 형식은 고정한다. 임의 주소는 호환 어댑터로만 설정한다. DNS 검사·고정 IP·TLS·단일 승인 출처·리다이렉트 차단은 기존 구현을 재사용한다. 로컬 주소만으로 외부 전달 차단을 보증하지 않는다.

## 기능과 매개변수

기능 상태는 `SUPPORTED / UNSUPPORTED / UNKNOWN`이다. 출처는 실제 기능 검사·제공사 메타데이터·정적 규칙·사용자 선언으로 구분한다. 실패한 실제 검사는 메타데이터나 사용자 선언으로 지우지 않는다. 재검사는 별도 동의를 요구한다. 모델 이름으로 기능을 추정하지 않는다.

숙고는 `AUTO / DISABLED / LOW / MEDIUM / HIGH / EXTRA_HIGH / MAX`를 사용한다. AUTO는 제공사 기본값이며 다른 수준은 선택한 모델의 근거와 네이티브 매핑이 모두 있어야 한다. Gemini는 낮음/보통/높음, DeepSeek는 비활성/낮음/높음/최대, xAI는 낮음/보통/높음/매우 높음, Mistral은 고정 reasoning 모드의 높음만 매핑한다. Anthropic·OpenAI도 실제 모델 지원 수준을 별도 확인한다. 제공사 간 동일한 수준의 의미를 가정하지 않는다.

문맥·입력 바이트·최대 출력·제한 시간·샘플링·숙고·저장 설정을 기록한다. 기능 근거가 없는 선택 매개변수는 전송 전에 차단한다. 샘플링과 숙고의 동시 사용은 별도 모델 근거가 필요하다. 자동 재시도는 0, 동시성은 1, 대체 경로는 NONE이다.

구조화 결과는 확인한 네이티브 JSON Schema, JSON 모드, 앱 검증 순으로 사용하며 항상 기존 Pydantic 검증을 통과해야 한다. DeepSeek 안정판에서 전체 JSON Schema 강제는 제공하지 않는다. 비엄격 네이티브 스키마와 앱 검증을 엄격한 제공사 보장으로 표시하지 않는다. 도구 호출·결과는 공통 형식으로 변환하지만 어댑터가 도구를 실행하지 않는다. 실제 연구는 기존 고정 도구·계약·Verifier·StateService 경계를 사용한다.

스트리밍은 2 MB 이하 응답을 비밀 검사한 뒤 공통 이벤트로 전달한다. 실시간 토큰 표시를 보장하지 않는다. 중간 종료·최종 사용량 누락은 미완료/미정산이며 자동 재전송하지 않는다. 비공개 추론·서명은 같은 어댑터의 프로토콜 연속성에만 사용하고 화면·정산 추적·공개 캐시에 넣지 않는다. 도구 응답을 공개 캐시에서 재개할 때 필요한 비공개 상태가 없으면 명시적으로 차단한다.

## 정산과 재개

기존 micro-USD 원장이 요청 전 월·연구·요청·횟수 한도를 검사한다. 일반 입력·출력·캐시 읽기·캐시 쓰기의 확인한 USD 단가만 사용한다. 가격 메타데이터는 확인 전 후보 값이며 활성 단가가 아니다. 캐시 사용량이 있으나 해당 단가가 없으면 미정산으로 남긴다. 추론 토큰이 이미 출력에 포함된 제공사는 중복 합산하지 않는다. Gemini의 별도 추론 사용량은 출력에 한 번 합산한다. 누락값은 미상이다.

요청·응답 ID, 요청 모델/별칭, 실제 응답 모델·수정본·시각, 프로필/가격/경로 수정본, 적용 매개변수와 근거를 기존 운영 감사 기록에 저장한다. 성공 결과·정산·추적은 같은 트랜잭션에 기록한다. 재개는 정산된 결과를 재사용하고 수락·사용량이 불확실한 요청은 `UNRESOLVED`로 보존한다. 원격 과금의 정확히 한 번 처리를 보장하지 않는다.

## 구현 근거

- [OpenAI Responses](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create), [모델 목록](https://platform.claude.com/docs/en/api/models/list)
- [Gemini Interactions](https://ai.google.dev/api/interactions-api-v1), [구조화 출력](https://ai.google.dev/gemini-api/docs/structured-output), [모델 목록](https://ai.google.dev/api/models)
- [xAI 숙고](https://docs.x.ai/developers/model-capabilities/text/reasoning)
- [DeepSeek Chat](https://api-docs.deepseek.com/api/create-chat-completion/)
- [Mistral Chat](https://docs.mistral.ai/api/endpoint/chat)

화면은 [VS Code 모델 관리](https://code.visualstudio.com/docs/agent-customization/language-models)와 [Carbon 선택 폼](https://carbondesignsystem.com/components/dropdown/usage/)을 웹 검색으로 확인해 참고했다. 제품 자산은 복사하지 않았다.

오프라인 모의 응답 검증은 실제 모델의 기능·연구 품질·성능·가격 검증이 아니다. 제공사·모델의 실제 성공 상태는 명시적으로 수행한 검사만 반영한다.
