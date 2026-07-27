#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from homekey_controller.app import HomeKeyController
from homekey_controller.config import load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone multi-reader Home Key controller"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "config" / "controller.json",
    )
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format=(
            "[%(asctime)s] [%(levelname)8s] "
            "%(name)-28s %(message)s"
        ),
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    config = load_config(args.config)
    HomeKeyController(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
