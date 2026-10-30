// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.
//
// rlc_queueing - DL RLC queuing latency, per UE, per radio bearer.
//
// HOOK: rlc_dl_sdu_send_started
//
// The measurement is genuinely per packet: srsRAN timestamps each SDU when it
// arrives at RLC and passes the elapsed time to the hook as latency_ns
// ("ns from sdu-arrival time to start of SDU transmission") in srs_meta_data2.
//
// This is why no PDCP-SN -> enqueue-timestamp shared map is needed here.  The
// classic design (rlc_enqueue writes a map keyed by PDCP SN, rlc_dequeue looks
// it up and subtracts) exists because some forks do not expose latency_ns.
// Reading it directly removes a per-packet map insert + lookup + delete on the
// datapath and removes the map-pressure failure mode entirely.
//
// The per-packet latencies are reduced to count/sum/min/max per bucket, so the
// consumer can plot a mean and a max envelope without per-packet streaming.

#include <linux/bpf.h>

#include "jbpf_srsran_contexts.h"

#include "upt_helpers.h"
#include "rlc_queue_stats.pb.h"

#include "../utils/misc_utils.h"
#include "../utils/hashmap_utils.h"
#include "../utils/stats_utils.h"

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

jbpf_ringbuf_map(out_rlc_queue, rlc_queue_stats, 16);

UPT_DEFINE_STATS_MAP(rlcq_stats_map, rlc_queue_stats)
UPT_DEFINE_BUCKET_MAP(rlcq_bucket_map)

DEFINE_PROTOHASH_64(rlcq_hash, UPT_MAX_UE_RB)

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

    rlc_queue_stats* out = (rlc_queue_stats*)jbpf_map_lookup_elem(&rlcq_stats_map, &zero_index);
    if (!out) {
        return JBPF_CODELET_FAILURE;
    }

    uint64_t* last_bucket = (uint64_t*)jbpf_map_lookup_elem(&rlcq_bucket_map, &zero_index);
    if (!last_bucket) {
        return JBPF_CODELET_FAILURE;
    }

    uint64_t now    = jbpf_time_get_ns();
    uint64_t bucket = now >> UPT_BUCKET_SHIFT;

    if (*last_bucket != bucket) {
        if (*last_bucket != 0 && out->stats_count > 0) {
            out->timestamp = now;
            out->bucket_id = *last_bucket;
            jbpf_ringbuf_output(&out_rlc_queue, (void*)out, sizeof(rlc_queue_stats));
        }
        JBPF_HASHMAP_CLEAR(&rlcq_hash);
        jbpf_map_clear(&rlcq_stats_map);
        *last_bucket = bucket;
    }

    // srs_meta_data1 = pdcp_sn << 32 | is_retx ; srs_meta_data2 = latency_ns
    uint32_t is_retx    = (uint32_t)(ctx->srs_meta_data1 & 0xFFFFFFFF);
    uint64_t latency_ns = ctx->srs_meta_data2;

    int rb_id = RBID_2_EXPLICIT(rlc_ctx.is_srb, rlc_ctx.rb_id);

    int new_val = 0;
    uint32_t ind =
        JBPF_PROTOHASH_LOOKUP_ELEM_64(out, stats, rlcq_hash, rlc_ctx.du_ue_index, rb_id, new_val);
    if (new_val) {
        out->stats[ind % UPT_MAX_UE_RB].du_ue_index = rlc_ctx.du_ue_index;
        out->stats[ind % UPT_MAX_UE_RB].is_srb      = rlc_ctx.is_srb;
        out->stats[ind % UPT_MAX_UE_RB].rb_id       = rlc_ctx.rb_id;
        UPT_LAT_INIT(out->stats[ind % UPT_MAX_UE_RB]);
    }

    UPT_LAT_UPDATE(out->stats[ind % UPT_MAX_UE_RB], latency_ns);
    if (is_retx) {
        out->stats[ind % UPT_MAX_UE_RB].retx_count += 1;
    }

    return JBPF_CODELET_SUCCESS;
}
