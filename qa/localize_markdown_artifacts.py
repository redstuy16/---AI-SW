"""생성된 Markdown 원문을 보존하고 번역 수정본의 해시를 따로 기록한다."""
from pathlib import Path
from hashlib import sha256
import json

from probe.locale_ko import translate_markdown
from probe.schemas import utc_now


def main():
    root = Path(__file__).resolve().parents[1]
    originals = root / "build/markdown-originals"
    originals.mkdir(exist_ok=True)
    records = []
    for path in (root / "build").rglob("*.md"):
        source = path.read_bytes()
        text = source.decode("utf-8-sig",errors="strict")
        translation = translate_markdown(text)
        data = translation.encode("utf-8",errors="strict")
        if data == source: continue
        digest = sha256(source).hexdigest()
        original = originals / (digest + ".txt")
        if not original.exists():
            source.decode("utf-8",errors="strict").encode("utf-8",errors="strict")
            original.write_bytes(source)
        path.write_bytes(data)
        records.append({"path":path.relative_to(root).as_posix(),"original_path":original.relative_to(root).as_posix(),
            "original_sha256":digest,"translated_sha256":sha256(data).hexdigest(),"historical_validation":"NOT_VALIDATED_AFTER_DOCUMENT_TRANSLATION"})
    audit = {"created_at":utc_now().isoformat(),"translated_files":len(records),"records":records,
        "note":"이 번역 수정본에 과거 검증을 승계하지 않는다. 원본 복원 또는 기존 검증 경계에서 새 보고서 생성이 필요하다."}
    target=root/"qa/results/markdown_translation_audit.json"
    target.write_bytes((json.dumps(audit,ensure_ascii=False,indent=2)+'\n').encode("utf-8",errors="strict"))
    print(json.dumps({"translated_files":len(records),"originals":len(list(originals.glob('*.txt')))},ensure_ascii=True))


if __name__ == "__main__": main()
