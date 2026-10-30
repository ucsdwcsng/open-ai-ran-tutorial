// Copyright (c) Microsoft Corporation. Licensed under the MIT license.
//
// rt_ctrl - EdgeRIC-RT per-TTI action intake (RT-E2 Policy, the RAN side of the TTI sync).
//
// HOOK: mac_sched_slot_ctrl (CONTROL; once per cell per slot, before DL scheduling). ctx.data points at
// the gNB's rt_ctrl_ctx carrying this slot's RANtime.
//
// Each slot:
//  1. Drain the rt_act_in channel (non-blocking) into a ring of pending actions indexed by target TTI.
//     Actions whose TTI has already passed are late (Lazy RIC) and dropped; for the same TTI the newer
//     seq wins, so an action refreshed while UL slots go by replaces the older one.
//  2. Gate: the slot gets an action only if one is pending for exactly this RANtime (HIT). Otherwise the
//     last HIT is re-applied if the muApp asked for a hold budget (HOLD), else the stock scheduler runs
//     (NONE), the RAN's default policy for Lazy RIC.

#include <linux/bpf.h>
#include <string.h>

#include "jbpf_srsran_contexts.h"
#include "rt_wire.h"

#include "../utils/misc_utils.h"

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

// Actions drained per slot. The bridge sends at most one per report, so this is ample.
#define RT_DRAIN (8)

jbpf_control_input_map(rt_act_in, rt_action_msg, 64);

struct rt_ring_entry {
    struct rt_action_msg msg;
    uint32_t             valid;
    uint32_t             pad;
};

struct jbpf_load_map_def SEC("maps") rt_ring = {
    .type        = JBPF_MAP_TYPE_ARRAY,
    .key_size    = sizeof(uint32_t),
    .value_size  = sizeof(struct rt_ring_entry),
    .max_entries = RT_RING,
};

// Receive buffer (keeps the 168-byte message off the 512-byte eBPF stack).
struct jbpf_load_map_def SEC("maps") rt_scratch = {
    .type        = JBPF_MAP_TYPE_ARRAY,
    .key_size    = sizeof(uint32_t),
    .value_size  = sizeof(struct rt_action_msg),
    .max_entries = 1,
};

struct rt_ctrl_state {
    struct rt_action_msg last_hit;
    uint32_t             last_hit_tti;
    uint32_t             have_last;
    uint32_t             n_rx;
    uint32_t             n_late;
    uint32_t             n_early;
    uint32_t             n_overwritten;
    uint32_t             n_bad;
    uint32_t             n_hit;
};

struct jbpf_load_map_def SEC("maps") rt_state = {
    .type        = JBPF_MAP_TYPE_ARRAY,
    .key_size    = sizeof(uint32_t),
    .value_size  = sizeof(struct rt_ctrl_state),
    .max_entries = 1,
};

static inline void rt_apply(struct rt_ctrl_ctx* ci, const struct rt_action_msg* m, uint32_t status)
{
    ci->status              = status;
    ci->act_seq             = m->seq;
    ci->act_tti             = m->tti;
    ci->act_src_tti         = m->src_tti;
    ci->act_flags           = m->flags;
    ci->act_t_src_report_ns = m->t_src_report_ns;
    ci->act_t_sent_ns       = m->t_sent_ns;
    ci->n                   = m->n < RT_MAX_UES ? m->n : RT_MAX_UES;
#pragma unroll
    for (int i = 0; i < RT_MAX_UES; i++) {
        ci->ue[i] = m->ue[i];
    }
}

extern "C" SEC("jbpf_srsran_generic")
uint64_t jbpf_main(void* state)
{
    struct jbpf_ran_generic_ctx* ctx = (jbpf_ran_generic_ctx*)state;

    struct rt_ctrl_ctx* ci = (struct rt_ctrl_ctx*)ctx->data;
    if (reinterpret_cast<uint8_t*>(ci) + sizeof(struct rt_ctrl_ctx) > reinterpret_cast<uint8_t*>(ctx->data_end)) {
        return JBPF_CODELET_FAILURE;
    }

    uint32_t              zero = 0;
    struct rt_ctrl_state* st   = (struct rt_ctrl_state*)jbpf_map_lookup_elem(&rt_state, &zero);
    struct rt_action_msg* m    = (struct rt_action_msg*)jbpf_map_lookup_elem(&rt_scratch, &zero);
    if (!st || !m) {
        return JBPF_CODELET_FAILURE;
    }
    const uint32_t now = ci->ran_tti;

    // 1. Drain new actions into the ring.
#pragma unroll
    for (int i = 0; i < RT_DRAIN; i++) {
        if (jbpf_control_input_receive(&rt_act_in, m, sizeof(struct rt_action_msg)) <= 0) {
            break;
        }
        st->n_rx++;
        if (m->magic != RT_ACTION_MAGIC || m->ver != RT_ACTION_VER || m->n > RT_MAX_UES) {
            st->n_bad++;
            continue;
        }
        const int32_t ahead = (int32_t)(m->tti - now);
        if (ahead < 0) {
            st->n_late++; // its TTI has passed: Lazy RIC
            continue;
        }
        if (ahead >= RT_RING) {
            st->n_early++;
            continue;
        }
        uint32_t              key = m->tti & (RT_RING - 1);
        struct rt_ring_entry* e   = (struct rt_ring_entry*)jbpf_map_lookup_elem(&rt_ring, &key);
        if (!e) {
            continue;
        }
        if (e->valid && e->msg.tti == m->tti) {
            st->n_overwritten++;
            if ((int32_t)(m->seq - e->msg.seq) <= 0) {
                continue; // an older action for the same TTI arrived late: keep the newer one
            }
        }
        e->msg   = *m;
        e->valid = 1;
    }

    // 2. Gate on the exact TTI.
    uint32_t              key = now & (RT_RING - 1);
    struct rt_ring_entry* e   = (struct rt_ring_entry*)jbpf_map_lookup_elem(&rt_ring, &key);
    if (e && e->valid && e->msg.tti == now) {
        rt_apply(ci, &e->msg, RT_STATUS_HIT);
        st->last_hit     = e->msg;
        st->last_hit_tti = now;
        st->have_last    = 1;
        e->valid         = 0;
        st->n_hit++;
    } else if (st->have_last && st->last_hit.hold > 0 && (now - st->last_hit_tti) <= st->last_hit.hold) {
        rt_apply(ci, &st->last_hit, RT_STATUS_HOLD);
    } else {
        ci->status = RT_STATUS_NONE;
    }

    ci->n_rx          = st->n_rx;
    ci->n_late        = st->n_late;
    ci->n_early       = st->n_early;
    ci->n_overwritten = st->n_overwritten;
    ci->n_bad         = st->n_bad;
    ci->n_hit         = st->n_hit;
    return JBPF_CODELET_SUCCESS;
}
