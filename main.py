"""Capitol Radar — entry point.

Usage:
  python main.py                   Start the scheduler (default)
  python main.py --run-now         Run the full pipeline immediately
  python main.py --update-outcomes Run outcome updater immediately
  python main.py --leaderboard     Post the leaderboard to Telegram immediately
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml

from storage.db import Database
from scheduler import (
    run_daily_pipeline,
    run_outcome_updater,
    run_weekly_leaderboard,
    start_scheduler,
)


def _load_config(config_path: Path) -> dict:
    """Load and return the YAML config file; exit on failure."""
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        print("Copy config.example.yaml to config.yaml and fill in your credentials.", file=sys.stderr)
        sys.exit(1)
    with config_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _setup_logging(level_str: str) -> None:
    """Configure root logger with timestamp and level from config."""
    level = getattr(logging, level_str.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    """Parse CLI flags, initialise DB, and dispatch the requested action."""
    parser = argparse.ArgumentParser(
        prog="capitol-radar",
        description="Track and alert on congressional stock trades.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config YAML file (default: config.yaml)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--run-now",
        action="store_true",
        help="Run the full scrape → filter → score → alert pipeline immediately",
    )
    group.add_argument(
        "--update-outcomes",
        action="store_true",
        help="Run the outcome updater immediately",
    )
    group.add_argument(
        "--leaderboard",
        action="store_true",
        help="Post the leaderboard to Telegram immediately",
    )

    args = parser.parse_args()

    config = _load_config(Path(args.config))
    _setup_logging(config.get("log_level", "INFO"))

    logger = logging.getLogger(__name__)
    logger.info("Capitol Radar starting")

    db_path = config.get("db_path", "./data/capitol_radar.db")
    db = Database(db_path)
    db.init_db()

    if args.run_now:
        logger.info("--run-now: executing full pipeline")
        run_daily_pipeline(db, config)

    elif args.update_outcomes:
        logger.info("--update-outcomes: running outcome updater")
        run_outcome_updater(db)

    elif args.leaderboard:
        logger.info("--leaderboard: posting leaderboard to Telegram")
        run_weekly_leaderboard(db, config)

    else:
        logger.info("Starting scheduler (press Ctrl+C to stop)")
        start_scheduler(db, config)


if __name__ == "__main__":
    main()
