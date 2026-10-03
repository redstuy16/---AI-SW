"""실제 Agent 쌍 비교의 기록 수집. 결과 판정은 별도 수행한다."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter

from htrsa.database import initialize
from htrsa.preflight import api_preflight


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--hard-limit-usd", type=float, default=1.0)
    parser.add_argument("--timeout-sec", type=int, default=300)
    args = parser.parse_args()
    if not api_preflight()["configured"]:
        raise SystemExit("Live LLM is UNCONFIGURED; F3-P live efficacy remains NOT_VALIDATED")
    if args.repeats < 1 or args.timeout_sec < 1 or args.hard_limit_usd <= 0:
        parser.error("repeats, timeout and budget must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    snapshot = args.csv.read_bytes()
    digest = sha256(snapshot).hexdigest()
    records = []
    for repeat in range(args.repeats):
        for arm, enabled in (("A_skills_on_f3p_off", "0"), ("B_skills_on_f3p_on", "1")):
            folder = args.output / f"{repeat + 1}_{arm}"
            folder.mkdir(parents=True, exist_ok=False)
            source = folder / "snapshot.csv"
            source.write_bytes(snapshot)
            environment = dict(os.environ, HTRSA_VERIFIED_ANALYSIS_SKILLS_ENABLED="1",
                               HTRSA_VERIFICATION_REPAIR_ENABLED=enabled, HTRSA_F3P_RIDGE_ARITHMETIC_CHECK="0")
            command = [sys.executable, "-m", "htrsa.agent_cli", str(folder / "state.sqlite"),
                       str(folder / "workspace"), str(source), "--verified-analysis-skills",
                       "--goal", args.goal, "--target-usd", str(args.hard_limit_usd / 4),
                       "--soft-limit-usd", str(args.hard_limit_usd * 0.75),
                       "--hard-limit-usd", str(args.hard_limit_usd)]
            start = perf_counter()
            try:
                process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                         env=environment, timeout=args.timeout_sec, check=False)
                exit_code, output = process.returncode, process.stdout
            except subprocess.TimeoutExpired:
                exit_code, output = None, ""
            db = initialize(folder / "state.sqlite")
            usage = dict(db.execute("SELECT SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,"
                                    "SUM(estimated_cost_usd) AS estimated_cost_usd FROM agent_runs WHERE provider IS NOT NULL").fetchone())
            usage["tool_executions"] = db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
            usage["repair_calls"] = db.execute("SELECT COUNT(*) FROM runtime_steps WHERE step_key LIKE 'repair_decision:%'").fetchone()[0]
            # 가격 누락을 무료로 취급하지 않는다.
            if db.execute("SELECT COUNT(*) FROM agent_runs WHERE provider IS NOT NULL AND estimated_cost_usd IS NULL").fetchone()[0]:
                usage["estimated_cost_usd"] = None
            db.close()
            records.append({"arm": arm, "repeat": repeat + 1, "snapshot_sha256": digest,
                            "exit_code": exit_code, "wall_clock_sec": perf_counter() - start,
                            "usage": usage, "runtime_output": output,
                            "correct_supported_completion": None, "wrong_to_right": None,
                            "right_to_wrong": None, "unnecessary_abstention": None,
                            "unfinished_recovery_budget": None})
            if sha256(source.read_bytes()).hexdigest() != digest:
                raise RuntimeError("dataset snapshot changed during ablation")
    report = {"live_efficacy": "NOT_VALIDATED", "reason": "requires independent final outcome adjudication",
              "provider_seed": "not configurable in existing adapter", "records": records}
    content = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8", errors="strict")
    (args.output / "paired_observations.json").write_bytes(content)


if __name__ == "__main__":
    main()
