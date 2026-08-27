"""Command-line entrypoint for the four-architecture ablation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from benchmark.ablation import AblationRunner, full_run_blocker, load_ablation_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--smoke", action="store_true", help="run deterministic wiring smoke"
    )
    parser.add_argument(
        "--full", action="store_true", help="require the exact 60-case selection"
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--run-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.smoke and args.full:
        raise SystemExit("--smoke and --full are mutually exclusive")
    config = load_ablation_config(args.config, smoke=args.smoke, run_id=args.run_id)
    if args.resume:
        config = config.model_copy(update={"resume": True})
    if args.full and not config.is_full_selection:
        raise SystemExit("--full requires exactly F01-001 through F12-005")
    if args.full and config.model_provider == "mock":
        print("FULL RUN = BLOCKED: mock provider is reserved for deterministic smoke")
        return 2
    if args.full:
        blocker = full_run_blocker(config)
        if blocker is not None:
            print(f"FULL RUN = BLOCKED: {blocker}")
            return 2
    result = AblationRunner(config).run()
    print(f"artifacts: {result.run_dir}")
    print(f"pairs: {result.fairness.attempted_pairs}/{result.fairness.expected_pairs}")
    print(
        f"fairness: {result.fairness.complete_pair_matrix and result.fairness.same_db_hash and not result.fairness.gt_runtime_leakage}"
    )
    return 0 if result.fairness.complete_pair_matrix else 2


if __name__ == "__main__":
    raise SystemExit(main())
