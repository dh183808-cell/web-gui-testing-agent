"""CLI entrypoint for the six-agent prototype.

Examples:
  python run_demo.py --target fixture --mode template
  python run_demo.py --target fixture-bug --mode template
  python run_demo.py --target arxiv --mode llm --allow-live-site
  python run_demo.py --target fixture --only-generate
"""
from __future__ import annotations

import argparse
import json
import sys
from web_agent.pipeline import DEFAULT_REQUIREMENT, run_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="Six Agent arXiv Web GUI Testing Demo v0.3")
    parser.add_argument("--target", choices=["fixture", "fixture-bug", "arxiv"], default="fixture")
    parser.add_argument("--mode", choices=["template", "llm"], default="template")
    parser.add_argument("--provider", choices=["deepseek", "openrouter", "openai"], default=None,
                        help="LLM provider when --mode llm; default from LLM_PROVIDER or deepseek")
    parser.add_argument("--model", default=None,
                        help="override provider model (OpenRouter: provider/model slug)")
    parser.add_argument("--requirement", default=DEFAULT_REQUIREMENT)
    parser.add_argument("--show-browser", action="store_true", help="show Chrome window")
    parser.add_argument("--demo-delay", type=float, default=0.0, help="visual pause in seconds between major steps (0-3)")
    parser.add_argument("--allow-live-site", action="store_true",
                        help="you confirmed permission and acceptable low-frequency use of the target site")
    parser.add_argument("--only-generate", action="store_true", help="do not start a Selenium test")
    parser.add_argument('--max-probes', type=int, choices=[0, 1], default=1,
                        help='maximum active evidence re-observations (0 or 1)')
    parser.add_argument('--max-repairs', type=int, choices=[0, 1], default=1,
                        help='maximum locator-only code repair attempts (0 or 1)')
    parser.add_argument('--enable-live-probes', action='store_true',
                        help='explicit extra consent for an additional arXiv detail observation')
    parser.add_argument('--enable-live-recovery', action='store_true',
                        help='explicit extra consent for one re-run against arXiv')
    parser.add_argument('--demo-evidence-gap', action='store_true',
                        help='fixture only: deliberately omit detail.title and test Agent5 probe')
    parser.add_argument('--demo-broken-locator', action='store_true',
                        help='fixture only: inject an invalid keyword locator and test Agent6 recovery')
    args = parser.parse_args()
    try:
        r = run_pipeline(requirement=args.requirement, target=args.target,
                         mode=args.mode, headless=not args.show_browser,
                         demo_delay=args.demo_delay, allow_live_site=args.allow_live_site, only_generate=args.only_generate,
                         provider=args.provider, model=args.model,
                         max_probes=args.max_probes, max_repairs=args.max_repairs,
                         enable_live_probes=args.enable_live_probes,
                         enable_live_recovery=args.enable_live_recovery,
                         demo_evidence_gap=args.demo_evidence_gap,
                         demo_broken_locator=args.demo_broken_locator)
    except Exception as e:
        print(f"Pipeline error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"status": r["verdict"]["status"], "message": r["verdict"]["message"],
                      "report": r["report"], "run_dir": r["run_dir"],
                      "dom_source": r["inspector"]["inspector_source"],
                      "workflow": r["contract"]["case_id"],
                      "review_attempts": len(r.get("reviews", [])),
                      "probe_count": r.get("probe_count", 0),
                      "repair_count": r.get("repair_count", 0)}, ensure_ascii=False, indent=2))
    return 0 if args.only_generate or r["verdict"]["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
