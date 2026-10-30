#!/bin/bash

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

# shellcheck disable=SC1091

deactivate
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
echo "Upgrading pip..."
.venv/bin/python3 -m pip install --upgrade pip
.venv/bin/python3 -m pip install argparse pyyaml hashlib
pip install --force-reinstall PyYAML
find . -name "*.pyc" -delete
deactivate