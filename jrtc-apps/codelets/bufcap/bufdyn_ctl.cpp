// Copyright (c) Microsoft Corporation. Licensed under the MIT license.
//
// bufdyn_ctl - controller half of the dynamic RLC DL buffer cap (experiment B).
//
// HOOK: rlc_dl_ctrl (control hook, per DL SDU on each RLC AM DRB, before the enqueue /
// tail-drop decision). Reads the UE's current DL MCS from mcs_map (written by bufdyn_obs,
// linked map) and sets the byte limit by Little's law plus a transient pad:
//
//   mu(MCS)   link rate, bytes/s, from mu_table.h (fit: mu = 5.658*SE + 1.325 Mbit/s,
//             SE from 38.214 Table 5.1.3.1-1, calibrated on this testbed)
//   B_ss    = mu * W_TARGET                            steady state: queue delay = W_TARGET
//   on a drop mu1 -> mu2:
//   B_pad   = mu1 * W_TARGET + (mu1 - mu2) * RTT0      hold what is queued + the excess TCP
//                                                      sends in the ~RTT0 before it notices
//   cap     = B_pad for HOLD_MS, then a linear ramp over RAMP_MS down to mu2 * W_TARGET
//   on a rise: cap = mu_new * W_TARGET at once (nothing to absorb)
//
// All integer math. Build parameters (ms): W_TARGET_MS, RTT0_MS, HOLD_MS, RAMP_MS.

#include <linux/bpf.h>
#include <string.h>

#include "jbpf_srsran_contexts.h"

#include "../utils/misc_utils.h"

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

#include "mu_table.h"   // static inline uint32_t mu_Bps(uint32_t mcs)

#ifndef W_TARGET_MS
#define W_TARGET_MS (50)
#endif
#ifndef RTT0_MS
#define RTT0_MS (30)
#endif
#ifndef HOLD_MS
#define HOLD_MS (60)
#endif
#ifndef RAMP_MS
#define RAMP_MS (200)
#endif
#define CAP_FLOOR (12000)   // ~8 full packets; the gNB also clamps to >= one max PDU
#define BUFDYN_MAX_UE (16)

struct jbpf_rlc_ctrl_info {   // MUST MATCH the gNB (see bufctl/rlc_ctrl.cpp)
    uint16_t du_ue_index;
    uint8_t  is_srb;
    uint8_t  rb_id;
    uint32_t cur_byte_limit;
    uint32_t new_byte_limit;
    uint32_t cur_sdu_limit;
    uint32_t new_sdu_limit;
    uint32_t cfg_byte_limit;
    uint32_t cfg_sdu_limit;
};

struct bufdyn_mcs {           // written by bufdyn_obs (linked map)
    uint32_t mcs;
    uint32_t valid;
};
struct jbpf_load_map_def SEC("maps") mcs_map = {
    .type = JBPF_MAP_TYPE_ARRAY,
    .key_size = sizeof(uint32_t),
    .value_size = sizeof(struct bufdyn_mcs),
    .max_entries = BUFDYN_MAX_UE,
};

struct bufdyn_state {
    uint32_t mu_ref;          // bytes/s of the level we are sized for
    uint32_t pad_bytes;       // B_pad while a pad is active, else 0
    uint64_t pad_t_ns;        // when the pad started
};
struct jbpf_load_map_def SEC("maps") st_map = {
    .type = JBPF_MAP_TYPE_ARRAY,
    .key_size = sizeof(uint32_t),
    .value_size = sizeof(struct bufdyn_state),
    .max_entries = BUFDYN_MAX_UE,
};

extern "C" SEC("jbpf_srsran_generic")
uint64_t jbpf_main(void* state)
{
    struct jbpf_ran_generic_ctx* ctx = (jbpf_ran_generic_ctx*)state;
    struct jbpf_rlc_ctrl_info* ci = (struct jbpf_rlc_ctrl_info*)ctx->data;
    if (reinterpret_cast<uint8_t*>(ci) + sizeof(struct jbpf_rlc_ctrl_info) >
        reinterpret_cast<uint8_t*>(ctx->data_end)) {
        return JBPF_CODELET_FAILURE;
    }
    if (ci->is_srb) {
        return JBPF_CODELET_SUCCESS;
    }
    uint32_t key = (uint32_t)ci->du_ue_index % BUFDYN_MAX_UE;
    struct bufdyn_mcs* m = (struct bufdyn_mcs*)jbpf_map_lookup_elem(&mcs_map, &key);
    struct bufdyn_state* s = (struct bufdyn_state*)jbpf_map_lookup_elem(&st_map, &key);
    if (!m || !s) {
        return JBPF_CODELET_SUCCESS;
    }
    uint32_t mu = mu_Bps(m->valid ? m->mcs : 28);
    uint64_t now = jbpf_time_get_ns();

    if (s->mu_ref == 0) {
        s->mu_ref = mu;                                        // first packet: size for now
    } else if (mu < s->mu_ref) {                               // channel got worse: pad
        s->pad_bytes = (uint32_t)(((uint64_t)s->mu_ref * W_TARGET_MS +
                                   (uint64_t)(s->mu_ref - mu) * RTT0_MS) / 1000);
        s->pad_t_ns = now;
        s->mu_ref = mu;
    } else if (mu > s->mu_ref) {                               // channel got better
        s->mu_ref = mu;
        s->pad_bytes = 0;
    }

    uint32_t base = (uint32_t)(((uint64_t)mu * W_TARGET_MS) / 1000);
    uint32_t cap = base;
    if (s->pad_bytes > base) {
        uint64_t dt_ms = (now - s->pad_t_ns) / 1000000ULL;
        if (dt_ms < HOLD_MS) {
            cap = s->pad_bytes;
        } else if (dt_ms < HOLD_MS + RAMP_MS) {
            cap = s->pad_bytes - (uint32_t)(((uint64_t)(s->pad_bytes - base) * (dt_ms - HOLD_MS)) / RAMP_MS);
        } else {
            s->pad_bytes = 0;
        }
    }
    if (cap < CAP_FLOOR) {
        cap = CAP_FLOOR;
    }
    ci->new_byte_limit = cap;
    return JBPF_CODELET_SUCCESS;
}
