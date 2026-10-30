#!/bin/bash

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

git submodule update --init --recursive

pushd . > /dev/null
cd jbpf_protobuf
./init_submodules.sh
popd > /dev/null

# patch jbpf
pushd . > /dev/null
cd containers/Docker/srsRAN_Project/external/jbpf
./init_and_patch_submodules.sh
popd > /dev/null
