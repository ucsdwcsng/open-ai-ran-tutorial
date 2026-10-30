// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.
//
// Shared helpers for the user-plane telemetry (upt) codelets.
//
// Design note (vs. the "stream every packet" approach):
// these codelets aggregate per-packet events into fixed time buckets *inside*
// the codelet and only push one message per bucket per stream. Streaming every
// packet costs ~130 MB/s per UE at 50 Mbps and exhausts the jbpf IO mempool
// (jbpf_mbuf_alloc -> "error dequeuing memory from the mempool"). Bucketing in
// the codelet keeps the per-packet accuracy of the measurement while making the
// output rate independent of the traffic rate.

#ifndef JRTC_UPT_HELPERS_H
#define JRTC_UPT_HELPERS_H

#include "../utils/stats_utils.h"

// Max (UE, radio-bearer) pairs tracked per bucket.
// MUST equal max_count in the .options files.
#define UPT_MAX_UE_RB (16)

// Per-packet record batching: records per output message. MUST equal max_count
// in rlc_pkt_records.options. Output rate = packet_rate / UPT_PKT_BATCH.
#define UPT_PKT_BATCH (64)

// Bucket width, as a power-of-two shift on the ns timestamp.
// A shift is used instead of a division because 64-bit division is awkward for
// the eBPF verifier.  1 << 27 ns = 134.217728 ms  (the "~100 ms" of the design).
// The exact width is re-derived on the consumer side from the same constant.
#define UPT_BUCKET_SHIFT (27)
#define UPT_BUCKET_NS    (1ULL << UPT_BUCKET_SHIFT)

// Explicit radio-bearer id: SRBs and DRBs share the small rb_id space, so fold
// is_srb into the key to keep SRB1 and DRB1 in distinct slots.
#ifndef RBID_2_EXPLICIT
#define RBID_2_EXPLICIT(__is_srb, __rb_id) ((__is_srb) ? (__rb_id) : ((__rb_id) + 8))
#endif

// One-entry array map holding the id of the bucket currently being accumulated.
#define UPT_DEFINE_BUCKET_MAP(__name)                    \
    struct jbpf_load_map_def SEC("maps") __name = {      \
        .type = JBPF_MAP_TYPE_ARRAY,                     \
        .key_size = sizeof(int),                         \
        .value_size = sizeof(uint64_t),                  \
        .max_entries = 1,                                \
    };

// One-entry array map holding the protobuf message being accumulated.
#define UPT_DEFINE_STATS_MAP(__name, __type)             \
    struct jbpf_load_map_def SEC("maps") __name = {      \
        .type = JBPF_MAP_TYPE_ARRAY,                     \
        .key_size = sizeof(int),                         \
        .value_size = sizeof(__type),                    \
        .max_entries = 1,                                \
    };

// 64-bit min/max accumulators (stats_utils.h only does 32-bit).
#define UPT_LAT_INIT(__d)          \
    do {                           \
        __d.count = 0;             \
        __d.latency_sum_ns = 0;    \
        __d.latency_min_ns = ~0ULL;\
        __d.latency_max_ns = 0;    \
        __d.retx_count = 0;        \
    } while (0)

#define UPT_LAT_UPDATE(__d, __v)            \
    do {                                    \
        __d.count++;                        \
        __d.latency_sum_ns += (__v);        \
        if ((__v) < __d.latency_min_ns) {   \
            __d.latency_min_ns = (__v);     \
        }                                   \
        if ((__v) > __d.latency_max_ns) {   \
            __d.latency_max_ns = (__v);     \
        }                                   \
    } while (0)

#endif // JRTC_UPT_HELPERS_H
