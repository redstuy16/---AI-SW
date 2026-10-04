"""등록된 결정적 도구. 결과는 검증 전 대기 상태로 기록한다."""
from __future__ import annotations

import csv
import io
import math
import scipy
import statistics
import time
from pathlib import Path

from pydantic import ValidationError
from scipy import stats

from .real_schemas import DataImportArgs, DatasetRecord, DatasetRefArgs, StatsArgs, PlotArgs, EvidenceArgs
from .schemas import ContextRef, RefType, ToolRequest, ToolResult, new_id, utc_now
from .service import StateService, ContractViolationError
from .storage import sha256_file, sha256_bytes, write_json, DatasetIntegrityError, ArtifactIntegrityError
from .analysis_skills import SkillApplicabilityError, SkillRequest, execute_skill
from .period_comparison import SelectedDataError


class ToolNotAllowedError(Exception):
    """등록되지 않았거나 계약에서 허용하지 않은 도구다."""


class ToolExecutionError(Exception):
    """도구 요청으로 유효한 결과를 만들 수 없다."""


def _failure(request: ToolRequest, code: str, detail: str) -> ToolResult:
    return ToolResult(ok=False, request_id=request.request_id, tool_name=request.tool_name,
                      error=f"{code}: {detail}")


def _rows(state: StateService, request: ToolRequest, dataset_id: str) -> tuple[list[str], list[dict[str, str]], object]:
    record = state.dataset_record(dataset_id, request.research_id)
    path = state.workspace.path(request.research_id, record["stored_path"])
    data = path.read_bytes()
    if sha256_bytes(data) != record["sha256"] or len(data) != record["size_bytes"]:
        raise DatasetIntegrityError(dataset_id)
    with io.StringIO(data.decode("utf-8-sig", errors="strict"), newline="") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        if not headers or len(set(headers)) != len(headers) or any(not h.strip() for h in headers):
            raise ToolExecutionError("invalid CSV header")
        rows = list(reader)
    if not rows or any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ToolExecutionError("empty or irregular CSV")
    return headers, rows, record


def _numeric(values: list[str]) -> tuple[list[float], int]:
    parsed: list[float] = []
    missing = 0
    for item in values:
        if item.strip() == "":
            missing += 1
            continue
        number = float(item)
        if not math.isfinite(number):
            raise ToolExecutionError("non-finite numeric value")
        parsed.append(number)
    return parsed, missing


class DataImportTool:
    name = "data.import"
    version = "1.0"

    def __init__(self, state: StateService, max_size_bytes: int = 10_000_000, *, allowed_source_paths=None):
        self.state = state
        self.max_size_bytes = max_size_bytes
        self.allowed_source_paths = None if allowed_source_paths is None else {Path(p).resolve() for p in allowed_source_paths}

    def run(self, request: ToolRequest) -> ToolResult:
        try:
            args = DataImportArgs.model_validate(request.args)
            if args.research_id != request.research_id:
                raise ToolExecutionError("research ID mismatch")
            source = Path(args.source_path)
            if self.allowed_source_paths is not None and (source.resolve() not in self.allowed_source_paths or
                    any(p.is_symlink() or p.is_junction() for p in [source, *source.parents])):
                raise ToolExecutionError("SOURCE_NOT_AUTHORIZED")
            if not source.is_file() or source.suffix.lower() != ".csv":
                raise ToolExecutionError("CSV source is missing")
            data = source.read_bytes()
            if not data or len(data) > self.max_size_bytes:
                raise ToolExecutionError("CSV size limit exceeded")
            decoded = data.decode("utf-8-sig", errors="strict")
            if "\x00" in decoded:
                raise ToolExecutionError("CSV contains NUL")
            with io.StringIO(decoded, newline="") as stream:
                reader = csv.reader(stream)
                header = next(reader, [])
                if not header or len(set(header)) != len(header) or any(not cell.strip() for cell in header):
                    raise ToolExecutionError("invalid CSV header")
                count = 0
                for row in reader:
                    if len(row) != len(header):
                        raise ToolExecutionError("irregular CSV row")
                    count += 1
                if count == 0:
                    raise ToolExecutionError("empty CSV")
            dataset_id = new_id("D")
            relative = f"inputs/datasets/{dataset_id}.csv"
            target = self.state.workspace.path(request.research_id, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            digest = sha256_file(target)
            record = DatasetRecord(dataset_id=dataset_id, research_id=request.research_id,
                                   original_name=source.name, stored_path=relative, sha256=digest,
                                   size_bytes=target.stat().st_size, created_at=utc_now().isoformat())
            self.state.register_dataset(record)
            return ToolResult(ok=True, request_id=request.request_id, tool_name=self.name,
                              result={"dataset_id": dataset_id, "sha256": digest, "size_bytes": record.size_bytes},
                              provenance={"dataset_id": dataset_id, "dataset_sha256": digest})
        except (ValidationError, ToolExecutionError, UnicodeError, OSError, ValueError) as exc:
            return _failure(request, "INVALID_CSV", str(exc))


class DataProfileTool:
    name = "data.profile"
    version = "1.0"

    def __init__(self, state: StateService, contract_id: str):
        self.state, self.contract_id = state, contract_id

    def run(self, request: ToolRequest) -> ToolResult:
        try:
            args = DatasetRefArgs.model_validate(request.args)
            headers, rows, dataset = _rows(self.state, request, args.dataset_id)
            columns = {}
            for name in headers:
                values = [row[name] for row in rows]
                nonmissing = [v for v in values if v.strip()]
                item = {"missing_count": len(values) - len(nonmissing),
                        "missing_ratio": (len(values) - len(nonmissing)) / len(values),
                        "unique_count": len(set(nonmissing)), "all_null": not nonmissing,
                        "constant": bool(nonmissing) and len(set(nonmissing)) == 1}
                try:
                    numeric, _ = _numeric(values)
                    item["dtype"] = "numeric" if numeric else "empty"
                    if numeric:
                        item.update({"min": min(numeric), "max": max(numeric),
                                     "mean": statistics.mean(numeric),
                                     "std": statistics.stdev(numeric) if len(numeric) > 1 else None})
                except ValueError:
                    item["dtype"] = "text"
                columns[name] = item
            profile = {"dataset_id": args.dataset_id, "dataset_sha256": dataset["sha256"],
                       "tool_call_id": request.request_id, "row_count": len(rows),
                       "column_count": len(headers), "columns": columns,
                       "duplicate_rows": len(rows) - len({tuple(row[h] for h in headers) for row in rows})}
            artifact_id = new_id("ART")
            relative = f"artifacts/{artifact_id}-dataset_profile.json"
            digest = write_json(self.state.workspace.path(request.research_id, relative), profile)
            self.state.register_file_artifact(artifact_id, request.research_id, self.contract_id,
                                              "DATA_PROFILE", relative, "tool", request.request_id, digest)
            self.state.set_dataset_profile(args.dataset_id, request.research_id, profile)
            return ToolResult(ok=True, request_id=request.request_id, tool_name=self.name,
                              result={"dataset_id": args.dataset_id, "artifact_id": artifact_id,
                                      "row_count": len(rows), "column_count": len(headers)},
                              artifacts=[ContextRef(type=RefType.artifact, id=artifact_id)],
                              provenance={"dataset_id": args.dataset_id, "dataset_sha256": dataset["sha256"]})
        except (ValidationError, ToolExecutionError, ValueError, OSError) as exc:
            return _failure(request, "INVALID_PROFILE_INPUT", str(exc))


class StatsTool:
    name = "stats.run"
    version = "1.0"

    def __init__(self, state: StateService, contract_id: str):
        self.state, self.contract_id = state, contract_id

    def run(self, request: ToolRequest) -> ToolResult:
        try:
            args = StatsArgs.model_validate(request.args)
            headers, rows, dataset = _rows(self.state, request, args.dataset_id)
            if any(name not in headers for name in args.variables.values()):
                raise ToolExecutionError("unknown column")
            result = _compute_stats(args, rows)
            artifact_id = new_id("ART")
            relative = f"results/{artifact_id}-stats_result.json"
            document = {"dataset_id": args.dataset_id, "dataset_sha256": dataset["sha256"],
                        "tool_call_id": request.request_id, "result": result}
            digest = write_json(self.state.workspace.path(request.research_id, relative), document)
            self.state.register_file_artifact(artifact_id, request.research_id, self.contract_id,
                                              "STATS_RESULT", relative, "tool", request.request_id, digest)
            return ToolResult(ok=True, request_id=request.request_id, tool_name=self.name, result=result,
                              artifacts=[ContextRef(type=RefType.artifact, id=artifact_id)],
                              provenance={"dataset_id": args.dataset_id, "dataset_sha256": dataset["sha256"],
                                          "stats_artifact_id": artifact_id,
                                          "scipy_version": scipy.__version__})
        except SelectedDataError as exc:
            return _failure(request, exc.code, str(exc))
        except (ValidationError, ToolExecutionError, ValueError, KeyError, OSError, ZeroDivisionError) as exc:
            return _failure(request, "INVALID_METHOD_INPUT", str(exc))


class VerifiedAnalysisSkillTool:
    """고정된 수치 절차를 도구 호출 하나로 실행한다."""
    name = "analysis.skill"
    version = "1.0.0"

    def __init__(self, state: StateService, contract_id: str):
        self.state, self.contract_id = state, contract_id

    def run(self, request: ToolRequest) -> ToolResult:
        fig = None
        try:
            if not getattr(self.state, "verified_analysis_skills_enabled", False):
                raise ToolExecutionError("verified analysis Skills are disabled")
            args = SkillRequest.model_validate(request.args)
            plan = args.plan
            if (args.research_id, args.task_id, args.contract_id) != (
                    request.research_id, request.task_id, self.contract_id):
                raise ToolExecutionError("skill request identity mismatch")
            if args.plan_ref != self.contract_id or args.plan_hash != plan.fingerprint():
                raise ToolExecutionError("skill plan fingerprint mismatch")
            contract, _ = self.state.contract(self.contract_id)
            if not any(ref.type == RefType.dataset and ref.id == plan.dataset_id for ref in contract.inputs):
                raise ToolExecutionError("dataset is absent from contract inputs")
            headers, rows, dataset = _rows(self.state, request, plan.dataset_id)
            if dataset["sha256"] != plan.dataset_sha256:
                raise DatasetIntegrityError(plan.dataset_id)
            try:
                outcome = execute_skill(plan, headers, rows)
            except SkillApplicabilityError as exc:
                from .analysis_skills import SkillResult
                outcome = SkillResult(applicability=exc.applicability, execution="not_run",
                                      reason_codes=[exc.code],
                                      missing_requirements=[exc.requirement] if exc.requirement else [],
                                      skill_id=plan.skill_id, skill_version=plan.skill_version,
                                      method=plan.method, plan_fingerprint=plan.fingerprint(), n=0,
                                      provenance={"dataset_id": plan.dataset_id,
                                                  "dataset_sha256": dataset["sha256"]})
                return ToolResult(ok=False, request_id=request.request_id, tool_name=self.name,
                                  result=outcome.model_dump(mode="json"), error=f"SKILL_{exc.code}")
            outcome.provenance = {"dataset_id": plan.dataset_id,
                                  "dataset_sha256": dataset["sha256"],
                                  "plan_fingerprint": plan.fingerprint(),
                                  "tool_call_id": request.request_id,
                                  "contract_id": self.contract_id,
                                  "seed": plan.seed,
                                  "replay": "resolve dataset hash, frozen plan and Skill version; run with recorded NumPy/SciPy environment"}
            plan_id, result_id, figure_id = (new_id("ART") for _ in range(3))
            plan_path = f"artifacts/{plan_id}-skill_plan.json"
            result_path = f"results/{result_id}-skill_result.json"
            figure_path = f"figures/{figure_id}.png"
            document = {"dataset_id": plan.dataset_id, "dataset_sha256": dataset["sha256"],
                        "tool_call_id": request.request_id, "plan_artifact_id": plan_id,
                        "plan_fingerprint": plan.fingerprint(), "result": outcome.model_dump(mode="json")}
            for artifact_id, kind, path, content in (
                    (plan_id, "SKILL_PLAN", plan_path, {"plan": plan.model_dump(mode="json"),
                                                       "fingerprint": plan.fingerprint(),
                                                       "dataset_sha256": dataset["sha256"]}),
                    (result_id, "SKILL_RESULT", result_path, document)):
                digest = write_json(self.state.workspace.path(request.research_id, path), content)
                self.state.register_file_artifact(artifact_id, request.research_id, self.contract_id,
                                                  kind, path, "tool", request.request_id, digest)
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(5, 4), dpi=100)
            if plan.skill_id == "tabular_association_v1":
                ax.scatter([float(row[plan.variables["x"]]) for row in rows
                            if row[plan.variables["x"]].strip() and row[plan.variables["y"]].strip()
                            and math.isfinite(float(row[plan.variables["x"]]))
                            and math.isfinite(float(row[plan.variables["y"]]))],
                           [float(row[plan.variables["y"]]) for row in rows
                            if row[plan.variables["x"]].strip() and row[plan.variables["y"]].strip()
                            and math.isfinite(float(row[plan.variables["x"]]))
                            and math.isfinite(float(row[plan.variables["y"]]))])
                ax.set(xlabel=plan.variables["x"], ylabel=plan.variables["y"])
            elif plan.skill_id == "timeseries_backtest_v1":
                ax.plot([item["prediction"] for item in outcome.trace], label="candidate")
                ax.plot([item["baseline_prediction"] for item in outcome.trace], label="baseline")
                ax.legend()
                ax.set(xlabel="forecast origin", ylabel=plan.units[plan.variables["target"]])
            else:
                ax.bar(["baseline", "candidate"],
                       [outcome.metrics[name]["mae"] for name in ("baseline", "candidate")])
                ax.set(ylabel="MAE")
            ax.set(title=plan.skill_id)
            figure_file = self.state.workspace.path(request.research_id, figure_path)
            fig.savefig(figure_file, metadata={"Software": "H-TRSA"})
            self.state.register_file_artifact(figure_id, request.research_id, self.contract_id,
                                              "FIGURE", figure_path, "tool", request.request_id,
                                              sha256_file(figure_file))
            return ToolResult(ok=True, request_id=request.request_id, tool_name=self.name,
                              result=outcome.model_dump(mode="json"),
                              artifacts=[ContextRef(type=RefType.artifact, id=item)
                                         for item in (plan_id, result_id, figure_id)],
                              provenance={"dataset_id": plan.dataset_id, "dataset_sha256": dataset["sha256"],
                                          "plan_artifact_id": plan_id, "stats_artifact_id": result_id,
                                          "figure_artifact_id": figure_id,
                                          "plan_fingerprint": plan.fingerprint(),
                                          "numpy_version": __import__("numpy").__version__,
                                          "scipy_version": scipy.__version__})
        except ValidationError:
            return _failure(request, "INVALID_SKILL_REQUEST", "schema validation failed")
        except (ToolExecutionError, ValueError, KeyError, OSError) as exc:
            return _failure(request, "INVALID_SKILL_REQUEST", str(exc))
        finally:
            if fig is not None:
                plt.close(fig)


def _compute_stats(args: StatsArgs, rows: list[dict[str, str]]) -> dict:
    if args.method == "two_period_comparison":
        from .period_comparison import compare_periods
        return compare_periods(rows, args.variables, args.parameters["periods"])
    def paired(left: str, right: str) -> tuple[list[float], list[float], int]:
        x, y, missing = [], [], 0
        for row in rows:
            a, b = row[args.variables[left]].strip(), row[args.variables[right]].strip()
            if not a or not b:
                missing += 1
                continue
            av, bv = float(a), float(b)
            if not math.isfinite(av) or not math.isfinite(bv):
                raise ToolExecutionError("non-finite values")
            x.append(av)
            y.append(bv)
        return x, y, missing

    method = args.method
    if method == "descriptive":
        values, missing = _numeric([row[args.variables["x"]] for row in rows])
        if len(values) < 2:
            raise ToolExecutionError("need at least two observations")
        return {"method": method, "n": len(values), "missing_excluded": missing,
                "mean": statistics.mean(values), "std": statistics.stdev(values),
                "min": min(values), "max": max(values)}
    if method in {"pearson_correlation", "spearman_correlation", "linear_regression"}:
        x, y, missing = paired("x", "y")
        if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
            raise ToolExecutionError("need three pairs and nonconstant variables")
        if method == "pearson_correlation":
            value = stats.pearsonr(x, y)
            ci = value.confidence_interval(confidence_level=0.95)
            return {"method": method, "n": len(x), "missing_excluded": missing,
                    "estimate": float(value.statistic), "statistic": float(value.statistic),
                    "p_value": float(value.pvalue), "confidence_interval": [float(ci.low), float(ci.high)]}
        if method == "spearman_correlation":
            value = stats.spearmanr(x, y)
            return {"method": method, "n": len(x), "missing_excluded": missing,
                    "estimate": float(value.statistic), "statistic": float(value.statistic),
                    "p_value": float(value.pvalue)}
        value = stats.linregress(x, y)
        return {"method": method, "n": len(x), "missing_excluded": missing,
                "coefficients": {"intercept": float(value.intercept), "slope": float(value.slope)},
                "standard_errors": {"intercept": float(value.intercept_stderr), "slope": float(value.stderr)},
                "test_statistics": {"slope_t": float(value.slope / value.stderr) if value.stderr else 0.0},
                "p_values": {"slope": float(value.pvalue)}, "r_squared": float(value.rvalue**2),
                "adjusted_r_squared": float(1 - (1 - value.rvalue**2) * (len(x)-1) / (len(x)-2))}
    if method in {"independent_t_test", "mann_whitney_u"}:
        value_name, group_name = args.variables["value"], args.variables["group"]
        groups: dict[str, list[float]] = {}
        missing = 0
        for row in rows:
            value, group = row[value_name].strip(), row[group_name].strip()
            if not value or not group:
                missing += 1
                continue
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ToolExecutionError("non-finite values")
            groups.setdefault(group, []).append(numeric)
        if len(groups) != 2 or any(len(v) < 2 for v in groups.values()):
            raise ToolExecutionError("need exactly two groups of at least two")
        a, b = [groups[k] for k in sorted(groups)]
        if method == "independent_t_test":
            value = stats.ttest_ind(a, b, equal_var=False)
            if not math.isfinite(float(value.statistic)):
                raise ToolExecutionError("undefined t statistic")
            effect = (statistics.mean(a)-statistics.mean(b)) / math.sqrt((statistics.variance(a)+statistics.variance(b))/2)
            return {"method": method, "n": len(a)+len(b), "missing_excluded": missing,
                    "statistic": float(value.statistic), "p_value": float(value.pvalue), "effect_size": effect}
        value = stats.mannwhitneyu(a, b, alternative="two-sided")
        return {"method": method, "n": len(a)+len(b), "missing_excluded": missing,
                "statistic": float(value.statistic), "p_value": float(value.pvalue)}
    raise ToolExecutionError("unsupported method")


class VisualizationTool:
    name = "visualization.render"
    version = "1.0"

    def __init__(self, state: StateService, contract_id: str):
        self.state, self.contract_id = state, contract_id

    def run(self, request: ToolRequest) -> ToolResult:
        fig = None
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            args = PlotArgs.model_validate(request.args)
            headers, rows, dataset = _rows(self.state, request, args.dataset_id)
            if args.x not in headers or (args.y and args.y not in headers):
                raise ToolExecutionError("unknown plot column")
            y = None
            if args.plot_type == "bar":
                if args.y is None:
                    raise ToolExecutionError("bar plot requires a value column")
                pairs = [(row[args.x].strip(), row[args.y].strip()) for row in rows]
                x = [label for label, value in pairs if label and value]
                y = [float(value) for label, value in pairs if label and value]
                if not x or any(not math.isfinite(value) for value in y):
                    raise ToolExecutionError("bar plot requires finite values")
            elif args.y:
                x, y, _ = _paired_plot(rows, args.x, args.y)
            else:
                x, _ = _numeric([row[args.x] for row in rows])
            fig, ax = plt.subplots(figsize=(5, 4), dpi=100)
            if args.plot_type == "scatter" and y is not None:
                ax.scatter(x, y)
            elif args.plot_type == "line" and y is not None:
                ax.plot(x, y)
            elif args.plot_type == "histogram":
                ax.hist(x)
            elif args.plot_type == "bar" and y is not None:
                ax.bar(x, y)
            else:
                raise ToolExecutionError("plot requires two columns")
            ax.set(title=args.title, xlabel=args.x_label or args.x, ylabel=args.y_label or (args.y or "count"))
            if any("\uac00" <= c <= "\ud7a3" for c in args.title + (args.x_label or "") + (args.y_label or "")):
                from matplotlib.font_manager import FontProperties
                font = next((p for p in (Path("C:/Windows/Fonts/malgun.ttf"), Path("/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf")) if p.is_file()), None)
                if font:
                    for label in (ax.title, ax.xaxis.label, ax.yaxis.label):
                        label.set_fontproperties(FontProperties(fname=str(font)))
            artifact_id = new_id("ART")
            relative = f"figures/{artifact_id}.png"
            path = self.state.workspace.path(request.research_id, relative)
            fig.savefig(path, metadata={"Software": "H-TRSA"})
            digest = sha256_file(path)
            self.state.register_file_artifact(artifact_id, request.research_id, self.contract_id,
                                              "FIGURE", relative, "tool", request.request_id, digest)
            return ToolResult(ok=True, request_id=request.request_id, tool_name=self.name,
                              result={"artifact_id": artifact_id, "sha256": digest},
                              artifacts=[ContextRef(type=RefType.artifact, id=artifact_id)],
                              provenance={"dataset_id": args.dataset_id, "dataset_sha256": dataset["sha256"]})
        except (ValidationError, ToolExecutionError, ValueError, OSError) as exc:
            return _failure(request, "INVALID_PLOT_INPUT", str(exc))
        finally:
            if fig is not None:
                plt.close(fig)


def _paired_plot(rows: list[dict[str, str]], x_name: str, y_name: str):
    x, y, missing = [], [], 0
    for row in rows:
        if not row[x_name].strip() or not row[y_name].strip():
            missing += 1
            continue
        x.append(float(row[x_name]))
        y.append(float(row[y_name]))
    if not x or any(not math.isfinite(v) for v in x+y):
        raise ToolExecutionError("no finite pairs")
    return x, y, missing


class EvidenceTool:
    name = "evidence.record"
    version = "1.0"

    def __init__(self, state: StateService):
        self.state = state

    def run(self, request: ToolRequest) -> ToolResult:
        try:
            args = EvidenceArgs.model_validate(request.args)
            if args.source_type == "artifact":
                self.state.file_artifact(args.source_ref, request.research_id)
            return ToolResult(ok=True, request_id=request.request_id, tool_name=self.name,
                              result=args.model_dump(mode="json"))
        except (ValidationError, ValueError) as exc:
            return _failure(request, "INVALID_EVIDENCE", str(exc))


class ToolRegistry:
    """논리 도구 실행. 실패한 호출을 포함해 중복 요청 키를 거절한다."""
    def __init__(self, state: StateService):
        self.state = state
        self._tools: dict[str, object] = {}

    def register(self, tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def dispatch(self, contract_id: str, request: ToolRequest) -> ToolResult:
        controlled = self.state._db.execute("SELECT 1 FROM sqlite_master WHERE name='control_schema'").fetchone()
        if controlled and request.tool_name in {'stats.run', 'analysis.skill', 'visualization.render'}:
            from .control_plane import ControlStore
            from .resource_queue import ResourcePool
            with ResourcePool(ControlStore(self.state._db)).sync_lease(request.research_id, request.actor_id):
                return self._dispatch_ready(contract_id, request)
        return self._dispatch_ready(contract_id, request)

    def _dispatch_ready(self, contract_id: str, request: ToolRequest) -> ToolResult:
        from .research_design import check_tool
        contract, _ = self.state.contract(contract_id)
        if request.research_id != contract.research_id:
            raise ToolNotAllowedError("연구의 소유 범위가 다릅니다.")
        if check_tool(self.state, contract, request):
            raise ToolNotAllowedError("RESEARCH_DESIGN_ACTION_BLOCKED")
        tool = self._tools.get(request.tool_name)
        if tool is None:
            raise ToolNotAllowedError(request.tool_name)
        try:
            self.state.reserve_tool_request(contract_id, request)
        except ContractViolationError as exc:
            raise ToolNotAllowedError(str(exc)) from exc
        started_at, started_clock = utc_now(), time.perf_counter()
        try:
            result = tool.run(request)
        except MemoryError:
            result = _failure(request, 'RESOURCE_EXHAUSTED', '분석에 사용할 메모리가 부족합니다.')
        except DatasetIntegrityError as exc:
            result = _failure(request, "DATASET_INTEGRITY_ERROR", str(exc))
        except ArtifactIntegrityError as exc:
            result = _failure(request, "ARTIFACT_INTEGRITY_ERROR", str(exc))
        except Exception as exc:
            result = _failure(request, "TOOL_EXECUTION_ERROR", str(exc))
        finished_at = utc_now()
        from .research_design import record_tool_outcome
        record_tool_outcome(self.state, request, result)
        provenance = {**result.provenance, "tool_name": tool.name, "tool_version": tool.version,
                      "started_at": started_at.isoformat(), "finished_at": finished_at.isoformat(),
                      "latency_ms": (time.perf_counter() - started_clock) * 1000,
                      "estimated_cost_usd": 0}
        result = result.model_copy(update={"provenance": provenance})
        contract, _ = self.state.contract(contract_id)
        self.state.record_execution(contract_id, contract.assigned_role, request, result)
        self.state.finish_tool_request(request, result)
        return result
