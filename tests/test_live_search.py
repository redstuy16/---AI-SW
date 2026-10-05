"""명시적으로 켠 레거시 무료 학술 네트워크 검사이며 현행 AI 검색 검증과 분리한다."""
from __future__ import annotations

import asyncio
import os

import pytest

from probe.scholarly import CrossrefProvider, OpenAlexProvider, ScholarlyError, SearchRequest


@pytest.mark.live_search
def test_openalex_search_and_crossref_doi_lookup():
    if os.getenv("PROBE_LEGACY_LIVE_SEARCH") != "1":
        pytest.skip("LEGACY 검사에는 PROBE_LEGACY_LIVE_SEARCH=1이 필요합니다. 현행 AI 검색 출시 표식을 만들지 않습니다.")

    async def run():
        result = await OpenAlexProvider().search(SearchRequest(
            research_id="R-live-smoke", query="research reproducibility", limit=3, require_doi=True))
        assert result.provider == "scholarly.openalex"
        source = next((item for item in result.sources if item.doi), None)
        if source is None:
            pytest.skip("OpenAlex returned no DOI-bearing work for the smoke query")
        crossref = await CrossrefProvider().get_work(source.doi)
        assert crossref.doi == source.doi and crossref.title

    try:
        asyncio.run(run())
    except ScholarlyError as exc:
        pytest.skip(f"public scholarly provider unavailable: {exc}")
