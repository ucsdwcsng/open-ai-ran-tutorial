// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.
//
// rlc_buffer (enqueue side) - HOOK: rlc_dl_new_sdu
//
// Owns the output channel for the rlc_buffer stream and flushes the closed
// bucket.  Under DL load new SDUs arrive continuously, so the flush happens
// promptly after each bucket boundary; the very last bucket of a flow is
// emitted when the next SDU arrives.

#include <linux/bpf.h>

#include "jbpf_srsran_contexts.h"

#include "rlc_buffer_common.h"
#include "rlc_buffer_stats.pb.h"

#include "../utils/misc_utils.h"
#include "../utils/hashmap_utils.h"
#include "../utils/stats_utils.h"

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

jbpf_ringbuf_map(out_rlc_buffer, rlc_buffer_stats, 16);

UPT_DEFINE_STATS_MAP(rlcb_stats_map, rlc_buffer_stats)
UPT_DEFINE_BUCKET_MAP(rlcb_bucket_map)

DEFINE_PROTOHASH_64(rlcb_hash, UPT_MAX_UE_RB)

extern "C" SEC("jbpf_srsran_generic")
uint64_t jbpf_main(void* state)
{
    int zero_index = 0;
    struct jbpf_ran_generic_ctx* ctx = (jbpf_ran_generic_ctx*)state;

    const jbpf_rlc_ctx_info& rlc_ctx = *reinterpret_cast<const jbpf_rlc_ctx_info*>(ctx->data);

    if (reinterpret_cast<const uint8_t*>(&rlc_ctx) + sizeof(jbpf_rlc_ctx_info) >
        reinterpret_cast<const uint8_t*>(ctx->data_end)) {
        return JBPF_CODELET_FAILURE;
    }

    rlc_buffer_stats* out = (rlc_buffer_stats*)jbpf_map_lookup_elem(&rlcb_stats_map, &zero_index);
    if (!out) {
        return JBPF_CODELET_FAILURE;
    }

    uint64_t* last_bucket = (uint64_t*)jbpf_map_lookup_elem(&rlcb_bucket_map, &zero_index);
    if (!last_bucket) {
        return JBPF_CODELET_FAILURE;
    }

    uint64_t now    = jbpf_time_get_ns();
    uint64_t bucket = now >> UPT_BUCKET_SHIFT;

    if (*last_bucket != bucket) {
        if (*last_bucket != 0 && out->stats_count > 0) {
            out->timestamp = now;
            out->bucket_id = *last_bucket;
            jbpf_ringbuf_output(&out_rlc_buffer, (void*)out, sizeof(rlc_buffer_stats));
        }
        JBPF_HASHMAP_CLEAR(&rlcb_hash);
        jbpf_map_clear(&rlcb_stats_map);
        *last_bucket = bucket;
    }

    int rb_id = RBID_2_EXPLICIT(rlc_ctx.is_srb, rlc_ctx.rb_id);

    int new_val = 0;
    uint32_t ind =
        JBPF_PROTOHASH_LOOKUP_ELEM_64(out, stats, rlcb_hash, rlc_ctx.du_ue_index, rb_id, new_val);
    if (new_val) {
        UPT_RLCB_ITEM_INIT(out->stats[ind % UPT_MAX_UE_RB], rlc_ctx);
    }

    out->stats[ind % UPT_MAX_UE_RB].enq_pkts += 1;

    const jbpf_queue_info_t* qi;
    UPT_RLCB_GET_QUEUE(rlc_ctx, qi);
    if (qi) {
        UPT_RLCB_SAMPLE(out->stats[ind % UPT_MAX_UE_RB], qi);
    }

    return JBPF_CODELET_SUCCESS;
}
