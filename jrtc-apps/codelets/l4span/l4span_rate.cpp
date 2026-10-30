// Copyright (c) Microsoft Corporation. Licensed under the MIT license.
//
// l4span_rate - L4S congestion signal, PER (UE, DRB).
//
// HOOK: rlc_dl_sdu_send_completed (srs_meta_data2 = latency_ns = RLC queuing
// delay for this SDU; ctx.data = jbpf_rlc_ctx_info with du_ue_index + rb_id).
// Per (UE,DRB): EWMA the queuing delay (state.s1) and map it PROPORTIONALLY to a
// marking probability (state.markprob), published in the shared `l4s_map` and
// applied by l4span_mark. Proportional+capped control settles at the minimum
// marking that holds the queue near target, preserving throughput.
//
// Keyed by slot((du_ue_index, rb_id)); l4span_mark uses the CU-UP ue_index which
// equals du_ue_index on this gNB (attach-order assignment; verified via F1/E1).

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
    uint32_t s1;         // EWMA queuing delay (ns)
    uint32_t s2;         // unused here
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

#define TARGET_NS   (10000000U)   // 10 ms
#define DELAY_CAP   (500000000U)  // clamp per-sample to 500 ms
#define P_SHIFT     (18)
#define P_CAP       (250)

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

    uint32_t cur = (uint32_t)(ctx->srs_meta_data2 > DELAY_CAP ? DELAY_CAP : ctx->srs_meta_data2);

    uint32_t slot = l4s_slot((uint32_t)rlc->du_ue_index, (uint32_t)rlc->rb_id);
    struct l4s_state* stp = (struct l4s_state*)jbpf_map_lookup_elem(&l4s_map, &slot);
    if (!stp) {
        return JBPF_CODELET_SUCCESS;
    }
    struct l4s_state st = *stp;

    // EWMA (1/8) of the queuing delay
    st.s1 = st.s1 - (st.s1 >> 3) + (cur >> 3);

    // proportional marking probability
    uint32_t prob = 0;
    if (st.s1 > TARGET_NS) {
        prob = (st.s1 - TARGET_NS) >> P_SHIFT;
        if (prob > P_CAP) {
            prob = P_CAP;
        }
    }
    st.markprob = prob;
    jbpf_map_update_elem(&l4s_map, &slot, &st, 0);

    return JBPF_CODELET_SUCCESS;
}
