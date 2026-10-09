#!/usr/bin/env python3
"""Check a custom archive with its exact producer contract before any repacking."""
import os
from pathlib import Path
import subprocess
import sys
from upstream_validation_contract import validator_root


def validate(path, version, arch):
    selected = validator_root(version)
    code = ('import pathlib,sys;sys.path.insert(0,sys.argv[1]);'
            'from validate_artifacts import validate_package;'
            'validate_package(pathlib.Path(sys.argv[2]),sys.argv[3],sys.argv[4]);'
            'print("Exact upstream producer contract verified")')
    subprocess.run([sys.executable, '-c', code, str(selected / 'scripts'),
                    str(Path(path).resolve()), version, arch], check=True,
                   env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))


if __name__ == '__main__':
    validate(*sys.argv[1:4])
