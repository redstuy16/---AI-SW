"""튜토리얼 단일 원본과 제한된 학습 진행 상태."""
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
import json
from typing import Literal
from pydantic import Field, field_validator, model_validator

from .schemas import StrictModel


@lru_cache(maxsize=1)
def tutorial_catalog():
    source = files("htrsa").joinpath("workbench_static/tutorial_content.js").read_text(encoding="utf-8")
    prefix = "window.HtrsaTutorialContent = "
    return json.loads(source.split(prefix, 1)[1].strip().removesuffix(";"))


def tutorial_lesson(step, provider_id="openai", local_tool="LM_STUDIO"):
    catalog = tutorial_catalog()
    provider = next((value for value in catalog["providers"] if value["id"] == provider_id), catalog["providers"][0])
    tool = next((value for value in catalog["local_tools"] if value["id"] == local_tool), catalog["local_tools"][0])
    material = {**step, **provider.get("lessons", {}).get(step["id"], {})}

    def fill(value):
        return value.replace("{local_tool}", tool["name"]).replace("{local_address}", tool["address"]) if isinstance(value, str) else value

    return {key: [fill(item) for item in value] if isinstance(value, list) else fill(value) for key, value in material.items()}


class TutorialProgress(StrictModel):
    course_version: Literal[2, 3, 4] = 2
    chapter_id: str = Field(default="basics", max_length=32, pattern=r"^[a-z_]+$")
    step_id: str = Field(default="overview", max_length=48, pattern=r"^[a-z_]+$")
    mode: Literal["WELCOME", "GUIDED"] = "WELCOME"
    status: Literal["IN_PROGRESS", "DISMISSED", "COMPLETED"] = "IN_PROGRESS"
    completed_steps: list[str] = Field(default_factory=list, max_length=30)
    provider_id: Literal["openai", "anthropic", "google_gemini", "xai", "deepseek", "mistral", "openai_compatible"] = "openai"
    local_tool: Literal["LM_STUDIO", "OLLAMA"] = "LM_STUDIO"

    @field_validator("mode", mode="before")
    @classmethod
    def migrate_legacy_mode(cls, value):
        return "GUIDED" if value == "EXAMPLE" else value

    @model_validator(mode="after")
    def known_steps_only(self):
        catalog = tutorial_catalog()
        allowed = {step["id"]: chapter["id"] for chapter in catalog["chapters"] for step in chapter["steps"]}
        if allowed.get(self.step_id) != self.chapter_id:
            raise ValueError("학습 과정과 단계가 일치하지 않습니다.")
        if self.course_version == 4:
            required = {step["id"] for step in catalog["quick_start"]}
        elif self.course_version == 3:
            required = set(catalog["legacy_quick_steps"])
        else:
            required = set(allowed)
        completed = set(self.completed_steps)
        if len(completed) != len(self.completed_steps) or not completed <= required:
            raise ValueError("완료 단계는 정의된 안내 항목만 사용할 수 있습니다.")
        if self.status == "COMPLETED" and (completed != required or self.mode == "WELCOME"):
            raise ValueError("필수 안내를 읽은 뒤 학습을 완료할 수 있습니다.")
        return self


def render_tutorial_guide():
    catalog = tutorial_catalog()
    tick = chr(96)
    lines = ["# H-TRSA 사용 안내", "", f"안내 버전: {catalog['course_version']} · 공식 문서 확인: {catalog['verified_at']}", "",
             "화면의 튜토리얼·사용 안내와 같은 원본에서 생성한다. 참여 선택 팝업에서 보기를 선택하면 실제 화면의 입력칸·버튼을 화살표와 안내 상자로 가리킨다. API 발급은 선택한 제공사의 절차를 자세히 읽고, 나머지는 실제 화면에서 따라 한다.",
             "학습 완료는 핵심 안내를 읽었다는 뜻이며 실제 API·Agent 성능·출시 검증과 별개다.", "",
             f"## 핵심 튜토리얼 — {len(catalog['quick_start'])}단계", ""]
    for index, step in enumerate(catalog["quick_start"], 1):
        lines += [f"### {index}. {step['action_title']}", "", step["summary"], ""]
        lines += [f"{i}. {value}" for i, value in enumerate(step["actions"], 1)]
        lines.append("")
    lines += ["자세한 발급·오류 해결·고급 기능은 아래 사용 안내에서 찾아본다.", ""]
    invitation = catalog["invitation"]
    lines += ["## 시작할 때 선택", "", invitation["title"], "", invitation["summary"], "",
                  f"- {invitation['accept']}: 튜토리얼을 실행한다.",
                  f"- {invitation['decline']}: 이번 실행에서 건너뛴다.",
                  f"- {invitation['never']}: 다음 시작부터 묻지 않는다.", "", invitation["reminder"], "",
                  "## 화살표와 안내 상자", "", "입력칸·버튼을 한 번에 하나씩 가리킨다. 안내 이동으로 키 입력·연결 검사·유료 연구를 자동 실행하지 않는다.", ""]
    for route in catalog["coach_targets"].values():
        lines += [f"### {route['title']}", ""]
        for target in route["targets"]:
            lines.append(f"- {target['text']}")
            if target.get("detail"):
                lines.append(f"  {target['detail']}")
            if target.get("local_text"):
                lines.append(f"- 로컬 서버: {target['local_text']}")
        lines.append("")
    lines += ["## 전체 사용 안내 목차", ""]
    lines += [f"- {i}. {chapter['title']}" for i, chapter in enumerate(catalog["chapters"], 1)]
    for index, chapter in enumerate(catalog["chapters"], 1):
        lines += ["", f"## {index}. {chapter['title']}", ""]
        for step in chapter["steps"]:
            lines += [f"### {step['title']}", "", "**준비물**", ""]
            lines += [f"- {value}" for value in step["ready"]]
            lines += ["", "**실제 조작**", ""]
            lines += [f"{i}. {value}" for i, value in enumerate(step["actions"], 1)]
            lines += ["", "**입력 예시**", "", step["example"], "", "**정상 결과**", ""]
            lines += [f"- {value}" for value in step["expected"]]
            lines += ["", "**문제가 생겼을 때**", ""]
            lines += [f"- {value}" for value in step["trouble"]]
            if step["extra"]:
                lines += ["", "**더 알아보기**", ""]
                lines += [f"- {value}" for value in step["extra"]]
    lines += ["", "## 제공사별 API 준비", "", "공식 링크는 직접 열며 앱이 계정·키·결제 작업을 자동 수행하지 않는다.", ""]
    for provider in catalog["providers"]:
        lines += [f"### {provider['name']}", "", "**먼저 할 일**", "", provider.get("key_intro", ""), ""]
        if provider["console"]:
            lines += [f"- [API 키 발급 화면 열기]({provider['keys']})", ""]
        lines += [f"{i}. {value}" for i, value in enumerate(provider["key_steps"], 1)]
        lines += ["", provider.get("key_finish", ""), "", provider.get("essential_notice", ""), "",
                  "<details>", "<summary>결제·계정·키 관리 등 추가 안내</summary>", "", "**계정과 이용 조건**", ""]
        lines += [f"- {value}" for value in provider["account"]]
        lines += ["", provider.get("key_terms", ""), "", provider["billing"], "", "**문제 해결**", "", provider["trouble"], ""]
        if provider["console"]:
            lines += [f"- [계정·결제 화면]({provider['console']})"]
        lines += [f"- [{title}]({url})" for title, url in provider["sources"]]
        lines += ["", "</details>", ""]
    for tool in catalog["local_tools"]:
        lines += [f"### {tool['name']}", "", f"로컬 서버 주소 예시: {tick}{tool['address']}{tick}", ""]
        lines += [f"{i}. {value}" for i, value in enumerate(tool["steps"], 1)]
        lines += ["", f"[공식 설치 파일 받기]({tool['download']})", "", f"[공식 연결 안내]({tool['source']})", ""]
        for step_id in ("provider_account", "provider_key", "save_connection"):
            base = next(step for chapter in catalog["chapters"] for step in chapter["steps"] if step["id"] == step_id)
            step = tutorial_lesson(base, "openai_compatible", tool["id"])
            lines += [f"#### {step['title']}", "", "**준비물**", ""]
            lines += [f"- {value}" for value in step["ready"]]
            lines += ["", "**실제 조작**", ""]
            lines += [f"{i}. {value}" for i, value in enumerate(step["actions"], 1)]
            lines += ["", "**입력 예시**", "", step["example"], "", "**정상 결과**", ""]
            lines += [f"- {value}" for value in step["expected"]]
            lines += ["", "**문제가 생겼을 때**", ""]
            lines += [f"- {value}" for value in step["trouble"]]
            lines += ["", "**더 알아보기**", ""]
            lines += [f"- {value}" for value in step["extra"]]
            lines.append("")
    lines += ["## 오류별 해결", ""]
    for issue in catalog["issues"]:
        lines += [f"### {issue['title']}", "", "오류명: " + " · ".join(tick + code + tick for code in issue["codes"]),
                  "", issue["cause"], "", f"다음 조치: {issue['fix']}", ""]
    lines += ["## 용어와 기능 41개", ""]
    step_titles = {step["id"]: step["title"] for chapter in catalog["chapters"] for step in chapter["steps"]}
    for topic in catalog["topics"]:
        lines += [f"### {topic['title']}", "", topic["summary"], "", f"자세한 절차: {step_titles[topic['step_id']]}", ""]
    from .research_design import guide_lines
    guide = guide_lines()
    lines += ["## " + guide[0], ""] + [value + "\n" for value in guide[1:]]
    lines += ["## 화면 구성 참고", ""]
    lines += [f"- [{title}]({url})" for title, url in catalog["benchmarks"]]
    result = "\n".join(lines).rstrip() + "\n"
    result.encode("utf-8", errors="strict")
    return result
