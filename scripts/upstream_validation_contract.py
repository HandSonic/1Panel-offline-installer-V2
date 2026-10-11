#!/usr/bin/env python3
"""Recheck producer metadata against authenticated runtime source inputs."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]


def validate_manifest_contract(data, version, arch, control, root=ROOT):
    from runtime_contract import manifest_contract
    return manifest_contract(data, control, version, arch, root)
