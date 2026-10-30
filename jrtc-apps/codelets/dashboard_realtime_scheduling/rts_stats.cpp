// Copyright (c) Microsoft Corporation. Licensed under the MIT license.
//
// rts_stats - per-UE scheduling state for the dashboard-realtime-scheduling xApp.
//
// HOOK: mac_sched_slot_report (every slot, once the slot is scheduled; the EdgeRIC-RT gNB hook). ctx.data is the
// gNB's slot report (rt_report_ctx in ../edgeric_rt/rt_wire.h): the CQI and DL backlog of every UE and the slot's
// grants. Like the upt codelets, this one aggregates inside the codelet over a 1 << 27 ns bucket (134.217728 ms)
// and sends one rts_stats message per bucket, so the xApp sees ~7.5 messages/s instead of 2,000 slot reports.
// That keeps it light next to the rt_bridge xApp, which shares jrtc's Python GIL. It does not need the edgeric_rt
// codelets: any codelet on mac_sched_slot_report makes the gNB build the report, so it works with the stock
// scheduler too.

#include <linux/bpf.h>

#include "jbpf_srsran_contexts.h"
#include "rts_stats.pb.h"

#include "../edgeric_rt/rt_wire.h"
#include "../utils/misc_utils.h"
#include "../upt/upt_helpers.h"

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

static_assert(sizeof(((rts_stats*)0)->ue) / sizeof(rts_ue) == RT_MAX_UES, "rts_stats.ue max_count must be 16");

// One message per bucket.
jbpf_ringbuf_map(out_rts_stats, rts_stats, 16);

UPT_DEFINE_STATS_MAP(rts_stats_map, rts_stats)
UPT_DEFINE_BUCKET_MAP(rts_bucket_map)

extern "C" SEC("jbpf_srsran_generic")
uint64_t jbpf_main(void* state)
{
    int zero = 0;
    struct jbpf_ran_generic_ctx* ctx = (jbpf_ran_generic_ctx*)state;

    const struct rt_report_ctx* rep = (const struct rt_report_ctx*)ctx->data;
    if (reinterpret_cast<const uint8_t*>(rep) + sizeof(struct rt_report_ctx) >
        reinterpret_cast<const uint8_t*>(ctx->data_end)) {
        return JBPF_CODELET_FAILURE;
    }

    rts_stats* out = (rts_stats*)jbpf_map_lookup_elem(&rts_stats_map, &zero);
    if (!out) {
        return JBPF_CODELET_FAILURE;
    }
    uint64_t* last_bucket = (uint64_t*)jbpf_map_lookup_elem(&rts_bucket_map, &zero);
    if (!last_bucket) {
        return JBPF_CODELET_FAILURE;
    }

    uint64_t now    = jbpf_time_get_ns();
    uint64_t bucket = now >> UPT_BUCKET_SHIFT;

    // ---- bucket rollover: close out the previous bucket and start a new one ----
    if (*last_bucket != bucket) {
        if (*last_bucket != 0 && out->slots > 0) {
            out->timestamp = now;
            out->bucket_id = *last_bucket;
            out->ue_count  = RT_MAX_UES; // the xApp skips the entries with slots == 0
            jbpf_ringbuf_output(&out_rts_stats, (void*)out, sizeof(rts_stats));
        }
        jbpf_map_clear(&rts_stats_map);
        *last_bucket   = bucket;
        out->tti_first = rep->ran_tti;
    }

    out->cell_id  = rep->cell_id;
    out->tti_last = rep->ran_tti;
    out->slots++;
    if (rep->is_dl) {
        out->dl_slots++;
        out->rbs_avail += rep->dl_rbs_avail;
        out->rbs_used += rep->dl_rbs_used;
        if (rep->act_status == RT_STATUS_HIT) {
            out->hit++;
        } else if (rep->act_status == RT_STATUS_HOLD) {
            out->hold++;
        }
    }

    uint32_t n = rep->ue_count;
    for (int i = 0; i < RT_MAX_UES; i++) {
        if ((uint32_t)i >= n) {
            break;
        }
        const struct rt_report_ue* r = &rep->ue[i];
        rts_ue* u                    = &out->ue[r->du_ue_index & (RT_MAX_UES - 1)];
        if (u->slots == 0) {
            u->du_ue_index = r->du_ue_index;
            u->cqi_min     = r->cqi;
        } else if (u->du_ue_index != r->du_ue_index) {
            continue; // two UEs 16 DU indexes apart in one bucket: keep the first
        }
        u->rnti = r->rnti;
        u->slots++;
        u->cqi_sum += r->cqi;
        if (r->cqi < u->cqi_min) {
            u->cqi_min = r->cqi;
        }
        if (r->cqi > u->cqi_max) {
            u->cqi_max = r->cqi;
        }
        u->backlog_sum += r->dl_pending_bytes;
        if (r->dl_pending_bytes > u->backlog_max) {
            u->backlog_max = r->dl_pending_bytes;
        }
        u->backlog_last = r->dl_pending_bytes;
        u->drb_bytes += r->drb_bytes;
        u->newtx_bytes += r->newtx_tbs;
        u->retx_bytes += r->retx_tbs;
        if (r->dl_prbs > 0) {
            u->prbs += r->dl_prbs;
            u->grant_slots++;
            u->mcs_sum += r->dl_mcs;
        }
    }

    return JBPF_CODELET_SUCCESS;
}
