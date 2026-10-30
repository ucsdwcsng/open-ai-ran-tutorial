#!/bin/sh

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

# This is script called to load a codeletSet's codelets.
# It has the options to using a Docker container or bare metal.

REVERSE_PROXY_PORT=30450
CURRENT_DIR=$( cd "$(dirname "${BASH_SOURCE[0]}")" ; pwd -P )
source $CURRENT_DIR/../set_vars.sh

set -e

# default args
lcm_addr=/tmp/jbpf/jbpf_lcm_ipc

Usage()
{
   # Display Help
   echo "Load a codeletSet"
   echo "options:"
   echo "-c      Mandatory.  Full path of codeletSet yaml file"
   echo "[-a]    Optional.   LCM address of Jbpf agent,  Default: '$lcm_addr'"
   echo
}

# Get the options
while getopts "c:a:" option; do
	case $option in
		c) # codeletSet yaml file
			codeletSet_yaml="$OPTARG";;
		a) # Set image tag
			lcm_addr="$OPTARG";;
		\?) # Invalid option
			echo "Error: Invalid option"
			Usage
			exit 1;;
	esac
done

# Check if codeletSet_yaml is set
if [ -z "$codeletSet_yaml" ]; then
    echo "Error: -c <codeletSet yaml file> is mandatory."
    Usage
    exit 1
fi
# check it exists
if [ ! -e "$codeletSet_yaml" ]; then
    echo "$codeletSet_yaml does not exist."
    exit 1
fi


if [ "$SRS_JBPF_DOCKER" -eq 1 ]; then
    
    echo "Loading codeletSet yaml file: $codeletSet_yaml using reverse proxy"

    # For all input and output maps defined in the yaml, generate a stream id
    $CURRENT_DIR/add_stream_ids.sh $codeletSet_yaml tmp.yaml

    # install yq
    # sudo wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/local/bin/yq
    # sudo chmod a+x /usr/local/bin/yq

    # convert yaml to json
    yq '.' tmp.yaml -o json > tmp.json

    # download to reverse-proxy
    echo "Sending CURL for $codeletSet_yaml..."
    curl -X POST -H "Content-Type: application/json" \
        http://localhost:$REVERSE_PROXY_PORT \
        --data @"$CURRENT_DIR/tmp.json"
    ret=$?
    if [ "$ret" -ne 0 ]; then
        echo "ERROR: CURL response received: $ret"
    fi

    rm -f tmp.yaml
    rm -f tmp.json

    exit $ret
else
    echo "Loading codeletSet yaml file: $codeletSet_yaml using local socket, LCM address: $lcm_addr"
    $JBPF_LCM_CLI_BIN -a $lcm_addr -l -c $codeletSet_yaml
fi

exit 0
