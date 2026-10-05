"""고정 보고서 문구를 번역한다. 식별자·수치·인용 원문은 보존한다."""
PHRASES = {
    "## Research Question":"## 연구 질문", "## Background / Literature":"## 배경과 문헌", "## Hypotheses":"## 가설",
    "## Data":"## 데이터", "## Methods":"## 방법", "## Results":"## 결과", "## Verification and Criticism":"## 검증과 비판",
    "## Follow-up Experiments":"## 후속 실험", "## Conclusion":"## 결론", "## Limitations":"## 한계",
    "## Reproducibility":"## 재현", "## References":"## References · 참고문헌", "## Material Claim Trace":"## 주요 주장 추적",
    "# Probe Release Candidate":"# Probe 출시 후보", "# Reproduce":"# 재현",
    "Verified experiments indicate an association; retrieved abstracts contain both supporting and contradictory statements. Causality remains unresolved.":"검증된 실험에서 연관성이 나타났지만 초록에는 지원과 반박이 모두 있다. 인과성은 미해결이다.",
    "Verified experimental results are mixed despite supporting abstract statements. Causality remains unresolved.":"초록의 지원에도 검증된 실험 결과는 일치하지 않는다. 인과성은 미해결이다.",
    "Verified experiments and retrieved abstracts indicate an association, but invalidated results were excluded. Causality remains unresolved.":"검증된 실험과 초록에서 연관성이 나타났다. 무효 결과는 제외했고 인과성은 미해결이다.",
    "Verified experiments and retrieved abstracts indicate an association. Causality remains unresolved.":"검증된 실험과 초록에서 연관성이 나타났다. 인과성은 미해결이다.",
    "Verified experimental evidence contradicts the proposed association; a supporting conclusion is not warranted.":"검증된 실험이 제안된 연관성을 반박하므로 지원 결론을 낼 수 없다.",
    "Verified experiments are available, but verified literature support is insufficient for a combined conclusion.":"검증된 실험은 있지만 문헌의 지원이 부족해 종합 결론을 내릴 수 없다.",
    "Available verified literature does not establish a combined experimental conclusion.":"현재 검증된 문헌만으로 실험을 종합한 결론을 낼 수 없다.",
    "No verified literature statement is available.":"검증된 문헌 문장이 없다.", "No testable hypothesis was recorded.":"검증 가능한 가설이 기록되지 않았다.",
    "No dataset was recorded.":"데이터가 기록되지 않았다.", "No verified experiment was recorded.":"검증된 실험이 기록되지 않았다.",
    "No verified numeric result is available.":"검증된 수치 결과가 없다.", "No verified experiment critique was recorded.":"검증된 실험 비판이 기록되지 않았다.",
    "No verified literature references.":"검증된 문헌 참조 없음.", "None recorded.":"기록 없음.",
    "Numeric values resolve from verified stats artifacts and field provenance.":"수치는 검증된 통계 산출물과 필드 출처에서 해석한다.",
    "Literature statements resolve to exact retrieved abstract text and stored hashes.":"문헌 문장은 정확한 초록 원문과 저장 해시로 확인한다.",
    "Execution verification does not establish scientific truth.":"실행 검증은 과학적 참을 입증하지 않는다.",
    "Exact result fields, source spans, historical revisions and obligations: research_slice.json.":"정확한 필드·인용 범위·과거 수정본·검증 의무: research_slice.json.",
    "Invalidated experiment results were excluded from support":"무효 실험은 지원 근거에서 제외했다.",
    "Contradictory abstract evidence remains unresolved":"초록의 반박 근거가 미해결이다.",
    "Abstract evidence and observational statistics do not establish causality.":"초록 근거와 관찰 통계는 인과성을 입증하지 않는다.",
    "Unmeasured confounding":"측정하지 않은 교란", "Unmeasured factors":"측정하지 않은 요인", "Endpoint influence":"끝점의 영향",
    "This folder is a credential-free export of one terminal research run.":"종료된 연구 하나를 자격 증명 없이 내보낸 폴더다.",
    "The `research_output/` files are projections and verified artifacts from the canonical state.":"research_output/은 정본 조회 결과와 검증된 산출물이다.",
    "Mode and environment validation are recorded in `environment_status.json` and the release manifest.":"모드와 환경 검증은 environment_status.json과 출시 선언 파일에 기록한다.",
    "Run from the repository root with the same Python environment:":"저장소 루트에서 같은 Python 환경으로 실행한다.",
    "The demo uses a fixed local fixture and a fake scholarly provider. It does not claim live provider validation.":"데모는 고정 로컬 자료와 모의 문헌 제공사를 사용한다. 실제 제공사 검증은 아니다.",
    "Spreadsheet display CSV cells are escaped when necessary. Byte-identical originals then live in `canonical_text/*.csv.raw.txt` as inert text; do not import these raw archives into a spreadsheet. The portable artifact manifest records the original and display hashes separately. Canonical workspace data is unchanged.":"표시용 CSV는 필요할 때 수식 시작 문자를 이스케이프한다. 원본 바이트는 canonical_text/*.csv.raw.txt에 보존하며 스프레드시트로 가져오지 않는다. 선언 파일은 원본·표시 해시를 구분하고 정본 데이터를 유지한다.",
    "Support level:":"지원 수준:", "Research ID:":"연구 ID:", "State version:":"상태 버전:", "Stop reason:":"종료 이유:",
    "verified stats artifact":"검증된 통계 산출물", "reviewer verdict":"검토 판정", "issues ":"문제 ",
    "numeric slots; created from":"수치 항목; 생성 출처", "(abstract; limitation: abstract only)":"(초록; 한계: 초록만 검토)",
}


def translate_markdown(text):
    for original, translated in PHRASES.items():
        if original == "## References":
            text = text.replace("## References\n",translated+"\n")
        else: text = text.replace(original,translated)
    import re
    text = re.sub(r"To inspect this export, serve the read-only dashboard and pass `(\?research_id=[^`]+)`\.",r"읽기 전용 대시보드에서 `\1`로 조회한다.",text)
    return text
