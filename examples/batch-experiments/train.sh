#!/usr/bin/env bash
set -euo pipefail
# Replace this example with your project's training command, for example:
# python train.py --data "$DATASET_0" "$@"
printf 'Training example. Arguments:'
printf ' %s' "$@"
printf '\n'
mkdir -p outputs
printf '{"example":true}\n' > outputs/model.json
