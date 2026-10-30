#!/bin/bash

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

export SRSRAN_APPS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd)"


unset JRTC_APPS
unset JRTC_CTL_BIN

source $SRSRAN_APPS_DIR/.env

if [ -f "$SRSRAN_APPS_DIR/.env.local" ]; then
    source $SRSRAN_APPS_DIR/.env.local
fi

export JBPF_CODELETS=$SRSRAN_APPS_DIR/codelets

if [ "$SRS_JBPF_DOCKER" -ne 1 ]; then
    # If we don't use containers, we need to set up 
    # the paths and the environment variables

    required_env_vars="SRSRAN_DIR"
    for env_var in $required_env_vars; do
        if [[ -z ${!env_var} ]]; then
            echo "ERROR:  $env_var is undefined !! "
            return
        fi
    done

    if [ "${USE_JRTC}" == "1" ]; then
        required_env_vars="JRTC_PATH"
        for env_var in $required_env_vars; do
            if [[ -z ${!env_var} ]]; then
                echo "ERROR:  $env_var is undefined !! "
                return
            fi
        done
        source $JRTC_PATH/setup_jrtc_env.sh

        export JRTC_APPS=$SRSRAN_APPS_DIR/jrtc_apps

        export JRTC_CTL_BIN=$JRTC_OUT_DIR/bin/jrtc-ctl

        if [ ! -f "$JRTC_CTL_BIN" ]; then
            echo "Error: JBPF_LCM_CLI_BIN : $JRTC_CTL_BIN does not exist"
        fi
    fi

    export CPP_INC=/usr/include/c++/13.2.0

    source $SRSRAN_APPS_DIR/jbpf_protobuf/setup_jbpfp_env.sh

    export JBPF_OUT_DIR=$SRSRAN_DIR/out
    export SRSRAN_INC_DIR=$SRSRAN_DIR/include
    export SRSRAN_EXTERNAL_DIR=$SRSRAN_DIR/external
    export VERIFIER_BIN=$SRSRAN_DIR/out/bin/srsran_verifier_cli
    export JBPF_LCM_CLI_BIN=$JBPF_OUT_DIR/bin/jbpf_lcm_cli
    export JBPF_PROTOBUF_CLI_BIN=$JBPFP_PATH/out/bin/jbpf_protobuf_cli


    # check various binary files exist
    if [ ! -f "$JBPF_LCM_CLI_BIN" ]; then
        echo "Error: JBPF_LCM_CLI_BIN : $JBPF_LCM_CLI_BIN does not exist"
    fi 

    if [ ! -f "$JBPF_PROTOBUF_CLI_BIN" ]; then
        echo "Error: JBPF_PROTOBUF_CLI_BIN : $JBPF_PROTOBUF_CLI_BIN does not exist"
    fi 

fi
