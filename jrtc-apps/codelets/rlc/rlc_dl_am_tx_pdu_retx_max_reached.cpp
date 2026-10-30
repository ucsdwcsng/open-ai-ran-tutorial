// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

#include <linux/bpf.h>

#include "jbpf_srsran_contexts.h"

#include "../utils/misc_utils.h"
#include "../utils/hashmap_utils.h"


#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"



#define DEBUG_PRINT

extern "C" SEC("jbpf_srsran_generic")
uint64_t jbpf_main(void* state)
{
    int zero_index=0;
    struct jbpf_ran_generic_ctx *ctx = (jbpf_ran_generic_ctx *)state;
    
    const jbpf_rlc_ctx_info& rlc_ctx = *reinterpret_cast<const jbpf_rlc_ctx_info*>(ctx->data);

    // Ensure the object is within valid bounds
    if (reinterpret_cast<const uint8_t*>(&rlc_ctx) + sizeof(jbpf_rlc_ctx_info) > reinterpret_cast<const uint8_t*>(ctx->data_end)) {
        return JBPF_CODELET_FAILURE;  // Out-of-bounds access
    }

    // create explicit rbid
    int rb_id = RBID_2_EXPLICIT(rlc_ctx.is_srb, rlc_ctx.rb_id);

    // Store SDU arrival time so we can calculate delay and queue size at the rlc level
    uint32_t sn = (uint32_t) (ctx->srs_meta_data1 >> 32);
    uint32_t retx_count = (uint32_t) (ctx->srs_meta_data1 & 0xFFFFFFFF);

#ifdef DEBUG_PRINT
    jbpf_printf_debug("RLC DL AM MAX RETX REACHED: du_ue_index=%d, rb_id=%d, window_size=%d\n", 
        rlc_ctx.du_ue_index, rb_id, retx_count);
    jbpf_printf_debug("  retx_count=%d sn=%d\n", 
        retx_count, sn);
#endif

    return JBPF_CODELET_SUCCESS;
}
