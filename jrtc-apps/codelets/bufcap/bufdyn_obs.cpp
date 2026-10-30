// Copyright (c) Microsoft Corporation. Licensed under the MIT license.
//
// bufdyn_obs - observer half of the dynamic buffer controller (experiment B).
//
// HOOK: mac_sched_harq_dl (telemetry hook, fires per DL HARQ event). On every new DL
// transmission it records the MCS the scheduler actually used for that UE into mcs_map,
// which the controller (bufdyn_ctl on rlc_dl_ctrl) reads through a linked map.
// It observes the real MCS, so the controller follows ANY channel change (badchan fade,
// trace or step, or real link adaptation) without sharing a schedule with it.

#include <linux/bpf.h>

#include "jbpf_srsran_contexts.h"

#include "../utils/misc_utils.h"

#ifndef SEC
#define SEC(NAME) __attribute__((section(NAME), used))
#endif

#include "jbpf_defs.h"
#include "jbpf_helper.h"

#define BUFDYN_MAX_UE (16)

struct bufdyn_mcs {
    uint32_t mcs;     // last new-TX DL MCS
    uint32_t valid;   // 1 once seen
};

struct jbpf_load_map_def SEC("maps") mcs_map = {
    .type = JBPF_MAP_TYPE_ARRAY,
    .key_size = sizeof(uint32_t),
    .value_size = sizeof(struct bufdyn_mcs),
    .max_entries = BUFDYN_MAX_UE,
};

extern "C" SEC("jbpf_ran_mac_sched")
uint64_t jbpf_main(void* state)
{
    struct jbpf_mac_sched_ctx* ctx = (jbpf_mac_sched_ctx*)state;
    const jbpf_mac_sched_harq_ctx_info_dl* h = reinterpret_cast<const jbpf_mac_sched_harq_ctx_info_dl*>(ctx->data);
    if (reinterpret_cast<const uint8_t*>(h) + sizeof(jbpf_mac_sched_harq_ctx_info_dl) >
        reinterpret_cast<const uint8_t*>(ctx->data_end)) {
        return JBPF_CODELET_FAILURE;
    }
    if (h->h.harq_type != JBPF_HARQ_EVENT_TX) {
        return JBPF_CODELET_SUCCESS;   // retx / failure events repeat an old grant's MCS
    }
    uint32_t key = (uint32_t)ctx->du_ue_index % BUFDYN_MAX_UE;
    struct bufdyn_mcs* e = (struct bufdyn_mcs*)jbpf_map_lookup_elem(&mcs_map, &key);
    if (e) {
        e->mcs = h->h.mcs;
        e->valid = 1;
    }
    return JBPF_CODELET_SUCCESS;
}
