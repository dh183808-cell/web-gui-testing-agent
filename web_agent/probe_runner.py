"""Subprocess entrypoint for Agent4 re-observation in a fresh browser session."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .probe import execute_probe


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--show-browser', action='store_true')
    parser.add_argument('--allow-live', action='store_true')
    parser.add_argument('--demo-delay', type=float, default=0.0)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding='utf-8'))
    result = execute_probe(request, args.base_url, Path(args.output), headless=not args.show_browser,
                           allow_live=args.allow_live, demo_delay=args.demo_delay)
    return 0 if result['status'] == 'COMPLETE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
