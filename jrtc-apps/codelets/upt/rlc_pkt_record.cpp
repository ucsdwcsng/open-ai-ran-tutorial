// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.
//
// rlc_pkt_record - lossless per-packet RLC DL telemetry, batched.
//
// HOOK: rlc_dl_sdu_send_started
//
// One record per SDU (pdcp_sn, latency_ns, is_retx, ue, rb, queue_bytes),
// appended to a fixed array in a map. The array is flushed as one IO message
// when it fills (UPT_PKT_BATCH records) or when a bucket boundary passes,
// whichever comes first. That makes the IO rate packet_rate / UPT_PKT_BATCH
// instead of packet_rate, which is the difference between this working and the
// mempool exhaustion that streaming every packet hit (deck slides 67-70) -
// while keeping every packet's measurement, unlike the aggregating codelets.
//
// The bucket-boundary flush bounds staleness under light load, so the xApp
// sees records within ~one bucket (134 ms) even at low packet rates - important
// because this stream is the sensor for the buffer control loop.
//
// This runs on the SAME hook as rlc_queueing (the aggregate stream); the two
// coexist - keep both, or drop rlc_queueing once the xApp derives the same
// aggregate from these records.

#include <linux/bpf.h>

#include "jbpf_srsran_contexts.h"

#include "upt_helpers.h"
#include "rlc_pkt_records.pb.h"

#include "../utils/misc_utils.h"
#include "../utils/hashmap_utils.h"
#include "../utils/stats_utils.h"

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

jbpf_ringbuf_map(out_rlc_pkts, rlc_pkt_records, 64);

UPT_DEFINE_STATS_MAP(rlcp_batch_map, rlc_pkt_records)
UPT_DEFINE_BUCKET_MAP(rlcp_bucket_map)

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

    rlc_pkt_records* out = (rlc_pkt_records*)jbpf_map_lookup_elem(&rlcp_batch_map, &zero_index);
    if (!out) {
        return JBPF_CODELET_FAILURE;
    }

    uint64_t* last_bucket = (uint64_t*)jbpf_map_lookup_elem(&rlcp_bucket_map, &zero_index);
    if (!last_bucket) {
        return JBPF_CODELET_FAILURE;
    }

    uint64_t now    = jbpf_time_get_ns();
    uint64_t bucket = now >> UPT_BUCKET_SHIFT;

    // ---- flush if the batch is full, or a bucket boundary passed ----
    bool bucket_rolled = (*last_bucket != 0) && (*last_bucket != bucket);
    if (out->pkts_count >= UPT_PKT_BATCH || bucket_rolled) {
        if (out->pkts_count > 0) {
            out->timestamp = now;
            jbpf_ringbuf_output(&out_rlc_pkts, (void*)out, sizeof(rlc_pkt_records));
            out->pkts_count = 0;
        }
    }
    if (*last_bucket != bucket) {
        *last_bucket = bucket;
    }

    // ---- append one record ----
    // srs_meta_data1 = pdcp_sn << 32 | is_retx ; srs_meta_data2 = latency_ns
    uint32_t pdcp_sn    = (uint32_t)(ctx->srs_meta_data1 >> 32);
    uint32_t is_retx    = (uint32_t)(ctx->srs_meta_data1 & 0xFFFFFFFF);
    uint64_t latency_ns = ctx->srs_meta_data2;

    uint32_t queue_bytes = 0;
    if ((rlc_ctx.rlc_mode == JBPF_RLC_MODE_AM) && (rlc_ctx.u.am_tx.sdu_queue_info.used)) {
        queue_bytes = rlc_ctx.u.am_tx.sdu_queue_info.num_bytes;
    } else if ((rlc_ctx.rlc_mode == JBPF_RLC_MODE_UM) && (rlc_ctx.u.um_tx.sdu_queue_info.used)) {
        queue_bytes = rlc_ctx.u.um_tx.sdu_queue_info.num_bytes;
    } else if ((rlc_ctx.rlc_mode == JBPF_RLC_MODE_TM) && (rlc_ctx.u.tm_tx.sdu_queue_info.used)) {
        queue_bytes = rlc_ctx.u.tm_tx.sdu_queue_info.num_bytes;
    }

    uint32_t i = out->pkts_count;
    if (i < UPT_PKT_BATCH) {
        out->pkts[i % UPT_PKT_BATCH].du_ue_index = rlc_ctx.du_ue_index;
        out->pkts[i % UPT_PKT_BATCH].is_srb      = rlc_ctx.is_srb;
        out->pkts[i % UPT_PKT_BATCH].rb_id       = rlc_ctx.rb_id;
        out->pkts[i % UPT_PKT_BATCH].pdcp_sn     = pdcp_sn;
        out->pkts[i % UPT_PKT_BATCH].latency_ns  = latency_ns;
        out->pkts[i % UPT_PKT_BATCH].is_retx     = is_retx;
        out->pkts[i % UPT_PKT_BATCH].queue_bytes = queue_bytes;
        out->pkts_count = i + 1;
    }

    return JBPF_CODELET_SUCCESS;
}
