// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.
//
// gtp_arrival - DL user-plane arrival rate, per UE, per radio bearer.
//
// HOOK: pdcp_dl_new_sdu
//
// NOTE ON THE HOOK CHOICE:
// this srsRAN build (xfoukas/srsRAN_Project_jbpf) declares no GTP-U hooks at
// all -- there is no gtpu_dl_enq/gtpu_dl_deq.  The DL path is
// GTP-U -> SDAP -> PDCP -> RLC, and pdcp_dl_new_sdu is the first per-packet DL
// hook that exists, firing once per SDU as it enters PDCP.  Every packet that
// arrives over GTP-U for an established DRB reaches this point, so the *rate*
// and *volume* measured here equal the GTP-U arrival rate; only the absolute
// timestamp is shifted by the (sub-microsecond) SDAP/PDCP entry cost.
// For true GTP-level fidelity a gtpu_dl_enq hook has to be added to srsRAN and
// the gNB rebuilt; this codelet then only needs its hook name changed.

#include <linux/bpf.h>

#include "jbpf_srsran_contexts.h"

#include "upt_helpers.h"
#include "gtp_arrival_stats.pb.h"

#include "../utils/misc_utils.h"
#include "../utils/hashmap_utils.h"
#include "../utils/stats_utils.h"

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

// Output stream: one message per bucket.
jbpf_ringbuf_map(out_gtp_arrival, gtp_arrival_stats, 16);

UPT_DEFINE_STATS_MAP(gtp_stats_map, gtp_arrival_stats)
UPT_DEFINE_BUCKET_MAP(gtp_bucket_map)

DEFINE_PROTOHASH_64(gtp_hash, UPT_MAX_UE_RB)

extern "C" SEC("jbpf_srsran_generic")
uint64_t jbpf_main(void* state)
{
    int zero_index = 0;
    struct jbpf_ran_generic_ctx* ctx = (jbpf_ran_generic_ctx*)state;

    const jbpf_pdcp_ctx_info& pdcp_ctx = *reinterpret_cast<const jbpf_pdcp_ctx_info*>(ctx->data);

    // Bounds check before touching the context.
    if (reinterpret_cast<const uint8_t*>(&pdcp_ctx) + sizeof(jbpf_pdcp_ctx_info) >
        reinterpret_cast<const uint8_t*>(ctx->data_end)) {
        return JBPF_CODELET_FAILURE;
    }

    gtp_arrival_stats* out = (gtp_arrival_stats*)jbpf_map_lookup_elem(&gtp_stats_map, &zero_index);
    if (!out) {
        return JBPF_CODELET_FAILURE;
    }

    uint64_t* last_bucket = (uint64_t*)jbpf_map_lookup_elem(&gtp_bucket_map, &zero_index);
    if (!last_bucket) {
        return JBPF_CODELET_FAILURE;
    }

    uint64_t now    = jbpf_time_get_ns();
    uint64_t bucket = now >> UPT_BUCKET_SHIFT;

    // ---- bucket rollover: close out the previous bucket and start a new one ----
    if (*last_bucket != bucket) {
        if (*last_bucket != 0 && out->stats_count > 0) {
            out->timestamp = now;
            out->bucket_id = *last_bucket;
            jbpf_ringbuf_output(&out_gtp_arrival, (void*)out, sizeof(gtp_arrival_stats));
        }
        JBPF_HASHMAP_CLEAR(&gtp_hash);
        jbpf_map_clear(&gtp_stats_map);
        *last_bucket = bucket;
    }

    // sdu_length is the low 32 bits of srs_meta_data1 (count is the high 32).
    uint32_t sdu_length = (uint32_t)(ctx->srs_meta_data1 & 0xFFFFFFFF);

    int rb_id = RBID_2_EXPLICIT(pdcp_ctx.is_srb, pdcp_ctx.rb_id);

    int new_val = 0;
    uint32_t ind =
        JBPF_PROTOHASH_LOOKUP_ELEM_64(out, stats, gtp_hash, pdcp_ctx.cu_ue_index, rb_id, new_val);
    if (new_val) {
        out->stats[ind % UPT_MAX_UE_RB].cu_ue_index = pdcp_ctx.cu_ue_index;
        out->stats[ind % UPT_MAX_UE_RB].is_srb      = pdcp_ctx.is_srb;
        out->stats[ind % UPT_MAX_UE_RB].rb_id       = pdcp_ctx.rb_id;
        out->stats[ind % UPT_MAX_UE_RB].pkt_count   = 0;
        out->stats[ind % UPT_MAX_UE_RB].byte_count  = 0;
    }

    out->stats[ind % UPT_MAX_UE_RB].pkt_count += 1;
    out->stats[ind % UPT_MAX_UE_RB].byte_count += sdu_length;

    return JBPF_CODELET_SUCCESS;
}
