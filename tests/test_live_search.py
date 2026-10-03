"""기존 동작과 검증 경계를 확인하는 회귀 테스트."""
from __future__ import annotations

import asyncio
import os

import pytest

from htrsa.scholarly import CrossrefProvider, OpenAlexProvider, ScholarlyError, SearchRequest


@pytest.mark.live_search
def test_openalex_search_and_crossref_doi_lookup():
    if os.getenv("HTRSA_LIVE_SEARCH") != "1":
        pytest.skip("set HTRSA_LIVE_SEARCH=1 to enable public scholarly network smoke")

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
