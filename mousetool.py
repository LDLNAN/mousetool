#!/usr/bin/env python3
"""Thin CLI entry-point shim delegating to the mousetool package."""
import mousetool as _pkg
from mousetool.cli import main


def __getattr__(name):
    return getattr(_pkg, name)


if __name__ == "__main__":
    main()
