#!/bin/sh

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

CURRENT_DIR=$( cd "$(dirname "${BASH_SOURCE[0]}")" ; pwd -P )
source $CURRENT_DIR/../set_vars.sh

if [ "$SRS_JBPF_DOCKER" -eq 1 ]; then
    $DOCKER_CMD rm -f jbpf_decoder
fi
