#!/bin/bash
# Loaded by Bash before the upstream installer. Optional network probes fail fast.
# Docker/Compose are installed by the launcher, so upstream need not fetch them.
curl() { return 1; }
wget() { return 1; }
export -f curl wget
