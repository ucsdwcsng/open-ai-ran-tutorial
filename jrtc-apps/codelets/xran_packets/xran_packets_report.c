// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

#include "jbpf_defs.h"
#include "jbpf_helper.h"
#include "xran_packet_info.pb.h"
#include "jbpf_srsran_contexts.h"
#include "xran_format.h"
#include "../utils/misc_utils.h"

// output map for stats
jbpf_ringbuf_map(output_map, packet_stats, 256);

// map to store data before sending to output_map
struct jbpf_load_map_def SEC("maps") output_tmp_map = {
    .type = JBPF_MAP_TYPE_ARRAY,
    .key_size = sizeof(int),
    .value_size = sizeof(packet_stats),
    .max_entries = 1,
};

//#define DEBUG_PRINT

SEC("jbpf_stats")
uint64_t
jbpf_main(void* state)
{
    int zero_index=0;
    packet_stats *out = (packet_stats *)jbpf_map_lookup_elem(&output_tmp_map, &zero_index);
    if (!out) {
        return JBPF_CODELET_FAILURE;    
    }

    uint64_t timestamp = jbpf_time_get_ns();

    out->timestamp = timestamp;
        
    int ret = jbpf_ringbuf_output(&output_map, (void *)out, sizeof(packet_stats));

    // zero the stats
    jbpf_map_clear(&output_tmp_map);
    
    if (ret < 0) {
#ifdef DEBUG_PRINT
        jbpf_printf_debug("xran_packets_report: Failure: jbpf_ringbuf_output\n");
#endif
        return JBPF_CODELET_FAILURE;
    }

    return JBPF_CODELET_SUCCESS;
}

