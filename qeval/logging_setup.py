"""Plain log file per run, plus warnings on the console."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.logging import RichHandler

FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(log_file: Path | None = None, *, verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    console = RichHandler(level=logging.DEBUG if verbose else logging.WARNING, show_path=False)
    root.addHandler(console)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(FMT))
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
