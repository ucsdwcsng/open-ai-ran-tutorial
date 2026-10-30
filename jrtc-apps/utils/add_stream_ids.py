# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import yaml
import argparse
import hashlib


##############################################################
def generate_uuid_from_string(input_string):
    # Create a hash from the input string (using SHA-256 or any other hash function)
    hashed_string = hashlib.sha256(input_string.encode()).hexdigest()

    return hashed_string[:32]


##############################################################
def add_stream_Ids_ch(codeletset_id, codelet, ch_type):
    if ch_type in codelet:
        for ch in codelet[ch_type]:
            if 'stream_id' not in ch:
                stream_id_raw_string = f"{codeletset_id}:{codelet['codelet_name']}:{ch_type}:{ch['name']}"
                ch['stream_id'] = str(generate_uuid_from_string(stream_id_raw_string))


##############################################################
def add_stream_Ids(input_file, output_file):
    # Read the YAML file
    with open(input_file, 'r') as file:
        data = yaml.safe_load(file)

    if 'codelet_descriptor' in data:
        for codelet in data['codelet_descriptor']:
             add_stream_Ids_ch(data['codeletset_id'], codelet, 'out_io_channel')
             add_stream_Ids_ch(data['codeletset_id'], codelet, 'in_io_channel')

    # Write the dictionary to the YAML file
    with open(output_file, 'w') as file:
        yaml.dump(data, file, default_flow_style=False)


##############################################################
if __name__ == "__main__":
    # Setup argument parser
    parser = argparse.ArgumentParser(description="Add stream_ids to YAML")
    parser.add_argument('-i', type=str, required=True, help="Path to the input YAML file")
    parser.add_argument('-o', type=str, required=True, help="Path to save the output YAML file")

    # Parse arguments
    args = parser.parse_args()

    # add stream_Ids to the YAML file, and write result to new file
    add_stream_Ids(args.i, args.o)
