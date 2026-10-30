// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

#include <linux/bpf.h>

#include "jbpf_srsran_contexts.h"
#include "pdcp_helpers.h"
#include "pdcp_dl_stats.pb.h"

#include "../utils/misc_utils.h"
#include "../utils/hashmap_utils.h"


#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"


#define MAX_NUM_UE_RB (256)

jbpf_ringbuf_map(output_map_dl, dl_stats, 1000);

// We store stats in this (single entry) map across runs
struct jbpf_load_map_def SEC("maps") stats_map_dl = {
    .type = JBPF_MAP_TYPE_ARRAY,
    .key_size = sizeof(int),
    .value_size = sizeof(dl_stats),
    .max_entries = 1,
};

DEFINE_PROTOHASH_64(dl_hash, MAX_NUM_UE_RB);



//#define DEBUG_PRINT

extern "C" SEC("jbpf_srsran_generic")
uint64_t jbpf_main(void* state)
{
    int zero_index=0;
    struct jbpf_ran_generic_ctx *ctx = (jbpf_ran_generic_ctx *)state;
    
    const jbpf_pdcp_ctx_info& pdcp_ctx = *reinterpret_cast<const jbpf_pdcp_ctx_info*>(ctx->data);

    // Ensure the object is within valid bounds
    if (reinterpret_cast<const uint8_t*>(&pdcp_ctx) + sizeof(jbpf_pdcp_ctx_info) > reinterpret_cast<const uint8_t*>(ctx->data_end)) {
        return JBPF_CODELET_FAILURE;  // Out-of-bounds access
    }

    // create explicit rbid
    int rb_id = RBID_2_EXPLICIT(pdcp_ctx.is_srb, pdcp_ctx.rb_id);    

#ifdef DEBUG_PRINT
    jbpf_printf_debug("pdcp_dl_deletion: cu_ue_index=%d, rb_id=%d\n", 
        ue_index, rb_id);
#endif

    // When a bearer context is setup, we need to reset the last acked
    // At the beginning, 0 is not acked so set to "-1".
    int new_val = 0;
    dl_stats *out = (dl_stats *)jbpf_map_lookup_elem(&stats_map_dl, &zero_index);
    if (!out)
        return JBPF_CODELET_FAILURE;
    uint32_t ind = JBPF_PROTOHASH_LOOKUP_ELEM_64(out, stats, dl_hash, rb_id, pdcp_ctx.cu_ue_index, new_val);

    PDCP_DL_STATS_INIT(out->stats[ind % MAX_NUM_UE_RB], pdcp_ctx.cu_ue_index, pdcp_ctx.is_srb, 
                        pdcp_ctx.rb_id, pdcp_ctx.rlc_mode);
    out->stats[ind % MAX_NUM_UE_RB].rlc_mode = JBPF_RLC_MODE_MAX;


    return JBPF_CODELET_SUCCESS;
}