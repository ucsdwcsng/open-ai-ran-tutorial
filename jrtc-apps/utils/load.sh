#!/bin/sh

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

CURRENT_DIR=$( cd "$(dirname "${BASH_SOURCE[0]}")" ; pwd -P )
source $CURRENT_DIR/../set_vars.sh

set -e

# default args
# lcm_addr is needed for bare metal
lcm_addr=/tmp/jbpf/jbpf_lcm_ipc

Usage()
{
   # Display Help
   echo "Load a codeletSet and schemas"
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

    # If the environment variable SRS_JBPF_DOCKER is set to 1, we are using a Docker container
    # In this mode the RAN will be running with a reverse proxy
    echo "Loading codeletSet yaml file: $codeletSet_yaml using reverse proxy"

    # load the schemas    
    ./load_schemas.sh -c $codeletSet_yaml

    # load codeletSet
    ./load_codeletSet.sh -c $codeletSet_yaml
    ret=$?

else

    # Load for bare metal using local socket
    echo "Loading codeletSet yaml file: $codeletSet_yaml using local socket, LCM address: $lcm_addr"
    tmp_yaml=$CURRENT_DIR/tmp.yaml 

    # For all input and output maps defined in the yaml, generate a stream id
    $CURRENT_DIR/add_stream_ids.sh $codeletSet_yaml $tmp_yaml

    # load the schemas
    ./load_schemas.sh -c $tmp_yaml

    # load codeletSet
    ./load_codeletSet.sh -c $tmp_yaml -a $lcm_addr
    ret=$?

    rm -f $tmp_yaml

fi

if [ "$ret" -eq 0 ]; then
    echo "CodeletSet and schemas loaded successfully."
else
    echo "Error: CodeletSet and schemas loading failed."
fi

exit $ret
