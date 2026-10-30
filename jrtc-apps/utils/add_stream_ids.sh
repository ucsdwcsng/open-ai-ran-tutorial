#!/bin/bash

# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

# shellcheck disable=SC1091

# This is a script to add stream IDs to a codeletSet yaml file.

# The generated stream is a hash of "<codeletset_id>:<codelet_name>:<ch_type>:<ch_name>".
# Therefore the same stream_id is generated each time the script is run for a specific input yaml file.


HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd)"

pushd . > /dev/null > /dev/null
cd "$HERE" || exit 1

source ../set_vars.sh

if [ ! -d ".venv" ]; then
    echo "Creating virtualenv."
    ./init_venv.sh
fi

source .venv/bin/activate

python3 add_stream_ids.py -i $1 -o $2

ret=$?

deactivate

popd > /dev/null  > /dev/null || exit 1

exit $ret