#!/usr/bin/env python3
"""Consume only a complete authenticated per-run source inventory."""
from pathlib import Path
from runtime_contract import require_selected
ROOT = Path(__file__).resolve().parents[1]


def resolved_matrix(version, root=ROOT):
    return require_selected(version, root)['inventory']['matrix']


def native_rows(version, root=ROOT):
    return require_selected(version, root)['inventory']['native_rows']
