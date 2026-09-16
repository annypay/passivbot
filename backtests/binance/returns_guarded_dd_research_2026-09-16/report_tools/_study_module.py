#!/usr/bin/env python3
"""Compatibility helper: export the module under test without importing the study.

`run_study.py` is a script, not a package member, so it is loaded by path. This keeps
the smoke tests independent of the study's `report_tools` being importable.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/returns_guarded_dd_research_2026-09-16"
STUDY_TOOLS = STUDY / "report_tools"
REPORT_SPEC = REPO / "backtests" / "report_spec"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_study_runner():
    return _load("returns_guarded_run_study", STUDY_TOOLS / "run_study.py")


def load_renderer():
    return _load("returns_guarded_render_annual_report", STUDY_TOOLS / "render_annual_report.py")


def load_report_spec():
    if str(REPORT_SPEC) not in sys.path:
        sys.path.insert(0, str(REPORT_SPEC))
    import annual_analysis  # noqa: PLC0415

    return annual_analysis
