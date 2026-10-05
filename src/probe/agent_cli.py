"""명시적으로 활성화한 Agent 실행·복구 명령."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from .agent_policy import PricingRegistry
from .agent_runtime import AgentRuntime
from .autonomous_loop import AutonomousResearchLoop
from .database import initialize, to_json
from .providers.openai_agents import OpenAIAgentsProvider
from .service import StateService
from .storage import Workspace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("csv", type=Path, nargs="?")
    parser.add_argument("--goal", default="Analyze the association between temperature and growth.")
    parser.add_argument("--resume", metavar="RESEARCH_ID")
    parser.add_argument("--autonomous", action="store_true", help="run the bounded hypothesis/critic research loop")
    parser.add_argument("--literature", action="store_true", help="search scholarly metadata before the autonomous experiment loop and export a verified report")
    parser.add_argument("--target-usd", type=float, default=0.25)
    parser.add_argument("--soft-limit-usd", type=float, default=0.75)
    parser.add_argument("--hard-limit-usd", type=float, default=1.0)
    parser.add_argument("--verified-analysis-skills", action="store_true", default=None,
                        help="opt in to versioned verified numerical Skills")
    parser.add_argument("--verification-repair", action="store_true", default=None,
                        help="opt in to bounded F3-P v0.3 repair and full revalidation")
    parser.add_argument("--ridge-arithmetic-check", action="store_true", default=None,
                        help="opt in to the experimental fixed Ridge numerical check (requires repair)")
    parser.add_argument("--claim-evidence-provenance", action="store_true", default=None)
    parser.add_argument("--verifier-dependency-catalog", action="store_true", default=None)
    args = parser.parse_args()
    if args.literature:
        parser.error("AI 지원 검색은 승인된 모델·가격·공유 검색 상한이 설정된 Workbench에서 실행해야 합니다. 무료 검색 엔진 대체 호출은 지원하지 않습니다.")
    if not args.resume and args.csv is None:
        parser.error("csv is required unless --resume is given")
    configured_pricing = os.environ.get("PROBE_PRICING_FILE")
    default_pricing = Path(__file__).resolve().parents[2] / "config" / "pricing.json"
    if configured_pricing:
        pricing = PricingRegistry.from_json_file(configured_pricing)
    else:
        pricing = PricingRegistry.from_json_file(default_pricing) if default_pricing.is_file() else PricingRegistry()
    db = initialize(args.database)
    try:
        state = StateService(db, Workspace(args.workspace))
        runtime_class = AutonomousResearchLoop if args.autonomous else AgentRuntime
        runtime = runtime_class(state, OpenAIAgentsProvider(), pricing=pricing,
                                verified_analysis_skills_enabled=args.verified_analysis_skills,
                                verification_repair_enabled=args.verification_repair,
                                ridge_arithmetic_check_enabled=args.ridge_arithmetic_check,
                                claim_evidence_provenance_enabled=args.claim_evidence_provenance,
                                verifier_dependency_catalog_enabled=args.verifier_dependency_catalog)
        if args.resume:
            result = asyncio.run(runtime.resume(args.resume))
        else:
            result = asyncio.run(runtime.run(args.goal, args.csv, target_usd=args.target_usd,
                                             soft_limit_usd=args.soft_limit_usd,
                                             hard_limit_usd=args.hard_limit_usd))
        print(to_json(result))
    finally:
        db.close()


if __name__ == "__main__":
    main()
