// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.
//
// Shared state + sampling logic for the rlc_buffer codelet pair.
//
// Occupancy is *sampled directly* from jbpf_rlc_ctx_info.u.*_tx.sdu_queue_info,
// which srsRAN fills in with the live SDU-queue depth (num_pkts / num_bytes) on
// every RLC event.  That is why this does not reconstruct occupancy by
// integrating (enqueued - dequeued) over buckets: an integrated counter drifts
// permanently if a single event is missed or a bearer is re-established, while a
// direct sample is self-correcting.  enq/deq counts are still reported per
// bucket so the arrival/drain behaviour of the queue stays visible.
//
// Two hooks feed this: rlc_dl_new_sdu (queue grows) and
// rlc_dl_sdu_send_completed (queue drains).  They share stats/bucket/hash maps
// via linked_maps in the codeletset.  The enqueue codelet owns the output
// channel and flushes the closed bucket.

#ifndef JRTC_UPT_RLC_BUFFER_COMMON_H
#define JRTC_UPT_RLC_BUFFER_COMMON_H

#include "upt_helpers.h"

// Pull the live SDU-queue depth out of the mode-specific union.
#define UPT_RLCB_GET_QUEUE(__rlc_ctx, __qi)                                            \
    do {                                                                               \
        __qi = NULL;                                                                   \
        if ((__rlc_ctx.rlc_mode == JBPF_RLC_MODE_AM) &&                                \
            (__rlc_ctx.u.am_tx.sdu_queue_info.used)) {                                 \
            __qi = &__rlc_ctx.u.am_tx.sdu_queue_info;                                  \
        } else if ((__rlc_ctx.rlc_mode == JBPF_RLC_MODE_UM) &&                         \
                   (__rlc_ctx.u.um_tx.sdu_queue_info.used)) {                          \
            __qi = &__rlc_ctx.u.um_tx.sdu_queue_info;                                  \
        } else if ((__rlc_ctx.rlc_mode == JBPF_RLC_MODE_TM) &&                         \
                   (__rlc_ctx.u.tm_tx.sdu_queue_info.used)) {                          \
            __qi = &__rlc_ctx.u.tm_tx.sdu_queue_info;                                  \
        }                                                                              \
    } while (0)

#define UPT_RLCB_ITEM_INIT(__d, __rlc_ctx)      \
    do {                                        \
        __d.du_ue_index      = __rlc_ctx.du_ue_index; \
        __d.is_srb           = __rlc_ctx.is_srb;      \
        __d.rb_id            = __rlc_ctx.rb_id;       \
        __d.samples          = 0;               \
        __d.queue_bytes_last = 0;               \
        __d.queue_bytes_max  = 0;               \
        __d.queue_bytes_sum  = 0;               \
        __d.queue_pkts_last  = 0;               \
        __d.queue_pkts_max   = 0;               \
        __d.enq_pkts         = 0;               \
        __d.deq_pkts         = 0;               \
    } while (0)

#define UPT_RLCB_SAMPLE(__d, __qi)                          \
    do {                                                    \
        __d.samples++;                                      \
        __d.queue_bytes_last = __qi->num_bytes;             \
        __d.queue_pkts_last  = __qi->num_pkts;              \
        __d.queue_bytes_sum += __qi->num_bytes;             \
        if (__qi->num_bytes > __d.queue_bytes_max) {        \
            __d.queue_bytes_max = __qi->num_bytes;          \
        }                                                   \
        if (__qi->num_pkts > __d.queue_pkts_max) {          \
            __d.queue_pkts_max = __qi->num_pkts;            \
        }                                                   \
    } while (0)

#endif // JRTC_UPT_RLC_BUFFER_COMMON_H
