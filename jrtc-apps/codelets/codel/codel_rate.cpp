// Copyright (c) Microsoft Corporation. Licensed under the MIT license.
//
// codel_rate - in-RAN CoDel-style AQM congestion signal (ECN variant), PER (UE,DRB).
//
// HOOK: rlc_dl_sdu_send_completed (srs_meta_data2 = latency_ns = SDU sojourn;
// ctx.data = jbpf_rlc_ctx_info with du_ue_index + rb_id). Per (UE,DRB), runs
// CoDel's control on the sojourn: below TARGET -> not congested; once sojourn stays
// >= TARGET for INTERVAL, enter dropping and RAMP the ECN marking probability.
// Published in the shared `l4s_map` (per-(UE,DRB) slot) and applied by l4span_mark.
//
// state.markprob = per-mille; state.s1 = first_above_ns; state.s2 = last_bump_ns.

#include <linux/bpf.h>
#include <string.h>

#include "jbpf_srsran_contexts.h"

#include "../utils/misc_utils.h"

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

#define L4S_MAX (64)

struct l4s_state {
    uint32_t markprob;   // per-mille
    uint32_t s1;         // first_above_ns
    uint32_t s2;         // last_bump_ns
};

struct jbpf_load_map_def SEC("maps") l4s_map = {
    .type = JBPF_MAP_TYPE_ARRAY,
    .key_size = sizeof(uint32_t),
    .value_size = sizeof(struct l4s_state),
    .max_entries = L4S_MAX,
};

static inline uint32_t l4s_slot(uint32_t ue, uint32_t rb)
{
    return ((ue << 4) | (rb & 0xF)) % L4S_MAX;
}

#define CODEL_TARGET_NS   (5000000U)     // 5 ms sojourn target
#define CODEL_INTERVAL_NS (100000000U)   // 100 ms hold-off
#define CODEL_BUMP_NS     (40000000U)    // ramp every 40 ms while dropping
#define CODEL_STEP        (30)
#define CODEL_CAP         (400)

extern "C" SEC("jbpf_srsran_generic")
uint64_t jbpf_main(void* state)
{
    struct jbpf_ran_generic_ctx* ctx = (jbpf_ran_generic_ctx*)state;

    const jbpf_rlc_ctx_info* rlc = (const jbpf_rlc_ctx_info*)ctx->data;
    if (reinterpret_cast<const uint8_t*>(rlc) + sizeof(jbpf_rlc_ctx_info) >
        reinterpret_cast<const uint8_t*>(ctx->data_end)) {
        return JBPF_CODELET_SUCCESS;
    }
    if (rlc->is_srb) {
        return JBPF_CODELET_SUCCESS;
    }

    uint64_t sojourn = ctx->srs_meta_data2;
    uint32_t now32   = (uint32_t)jbpf_time_get_ns();

    uint32_t slot = l4s_slot((uint32_t)rlc->du_ue_index, (uint32_t)rlc->rb_id);
    struct l4s_state* stp = (struct l4s_state*)jbpf_map_lookup_elem(&l4s_map, &slot);
    if (!stp) {
        return JBPF_CODELET_SUCCESS;
    }
    struct l4s_state st = *stp;

    if (sojourn < CODEL_TARGET_NS) {
        st.markprob = 0;
        st.s1 = 0;
        jbpf_map_update_elem(&l4s_map, &slot, &st, 0);
        return JBPF_CODELET_SUCCESS;
    }

    // sojourn >= target
    if (st.s1 == 0) {
        st.s1 = now32;                    // start hold-off timer
        jbpf_map_update_elem(&l4s_map, &slot, &st, 0);
        return JBPF_CODELET_SUCCESS;
    }
    if ((now32 - st.s1) < CODEL_INTERVAL_NS) {
        return JBPF_CODELET_SUCCESS;      // within hold-off; tolerate burst
    }

    // dropping state: ramp marking probability every CODEL_BUMP_NS
    if ((now32 - st.s2) >= CODEL_BUMP_NS) {
        uint32_t p = st.markprob + CODEL_STEP;
        if (p > CODEL_CAP) {
            p = CODEL_CAP;
        }
        st.markprob = p;
        st.s2 = now32;
        jbpf_map_update_elem(&l4s_map, &slot, &st, 0);
    }
    return JBPF_CODELET_SUCCESS;
}
