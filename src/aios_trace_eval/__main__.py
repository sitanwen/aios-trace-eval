"""AIOS Trace Eval command line."""
from __future__ import annotations

import argparse
import os
from typing import List


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AIOS OpenClaw observability and evaluation platform")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="start the unified web, API and OTLP service")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    if args.command == "serve":
        os.environ["AIOS_HOST"] = args.host
        os.environ["AIOS_PORT"] = str(args.port)
        from .server import main as serve_main
        serve_main()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
