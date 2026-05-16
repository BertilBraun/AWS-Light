from __future__ import annotations

import logging
import sys


def configure(service_name: str, level: int = logging.INFO) -> None:
    """Set up stdout logging with timestamps for Docker log consumption."""
    root = logging.getLogger()
    root.setLevel(level)

    if root.handlers:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            fmt=f"%(asctime)s [{service_name}] %(name)s %(levelname)-8s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root.addHandler(handler)
