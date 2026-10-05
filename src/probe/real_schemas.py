"""로컬 데이터·과학 도구의 구조화 계약."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .schemas import StrictModel
from .schemas import ContextRef


class DataImportArgs(StrictModel):
    source_path: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    expected_format: Literal["csv"] = "csv"


class DatasetRecord(StrictModel):
    dataset_id: str
    research_id: str
    source_type: Literal["local_file"] = "local_file"
    original_name: str
    stored_path: str
    sha256: str
    size_bytes: int = Field(ge=0)
    format: Literal["csv"] = "csv"
    row_count: int | None = None
    column_count: int | None = None
    column_schema: dict[str, Any] | None = None
    created_at: str
    status: Literal["IMPORTED", "PROFILED", "INVALID"] = "IMPORTED"


class DatasetRefArgs(StrictModel):
    dataset_id: str = Field(min_length=1)


class StatsArgs(StrictModel):
    dataset_id: str
    method: Literal["descriptive", "pearson_correlation", "spearman_correlation", "independent_t_test", "mann_whitney_u", "linear_regression", "two_period_comparison"]
    variables: dict[str, str]
    parameters: dict[str, Any] = Field(default_factory=dict)


class PlotArgs(StrictModel):
    dataset_id: str
    plot_type: Literal["scatter", "line", "histogram", "bar"]
    x: str
    y: str | None = None
    title: str = ""
    x_label: str = ""
    y_label: str = ""


class EvidenceArgs(StrictModel):
    claim: str = Field(min_length=1)
    polarity: Literal["support", "contradict", "neutral"]
    source_type: Literal["artifact", "experiment"]
    source_ref: str


class PythonExecuteArgs(StrictModel):
    code: str = Field(min_length=1, max_length=100_000)
    input_artifact_refs: list[ContextRef] = Field(default_factory=list)
    timeout_sec: int = Field(default=120, gt=0, le=300)
