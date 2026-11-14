from __future__ import annotations

import argparse
import json
import sys

from src.models.quant_audit import run_quant_audit


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Purged walk-forward quant audit and champion/challenger training")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--coin", choices=["SOL", "HYPE"], action="append")
    parser.add_argument("--horizon", choices=["15m", "1h", "4h"], action="append")
    parser.add_argument("--no-promote", action="store_true", help="Save challengers but never replace a champion")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = arguments()
    coins = args.coin or ["SOL", "HYPE"]
    horizons = args.horizon or ["15m", "1h", "4h"]
    report, path = run_quant_audit(
        args.config,
        [(coin, horizon) for coin in coins for horizon in horizons],
        promote=not args.no_promote,
        progress=lambda message: print(message, flush=True),
    )
    summary = {
        key: {
            "selected_barrier": value["selected_barrier"],
            "selected_variant": value["selected_variant"],
            "selected_weights": value["selected_weights"],
            "model_status": value["model_status"],
            "test_metrics": value["untouched_test_metrics"],
        }
        for key, value in report["tasks"].items()
    }
    print(json.dumps({"report": str(path), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
