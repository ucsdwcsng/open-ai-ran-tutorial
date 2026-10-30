// Copyright (c) Microsoft Corporation. Licensed under the MIT license.
//
// bufcap - fixed RLC DL buffer cap, for the control-hook tutorial (experiment A).
//
// HOOK: rlc_dl_ctrl (control hook in rlc_tx_am_entity::handle_sdu). Fires once per DL SDU
// on each RLC AM DRB, before the enqueue/tail-drop decision.
//
// One source, built once per cap: -DCAP_BYTES=<bytes>. While loaded it asserts that byte
// limit on every DRB of every UE, so the queue tail-drops once it holds CAP_BYTES.
//
// CAP_BYTES=0 builds "bufcap_off": it writes the bearer's CONFIGURED limits back
// (cfg_* from the hook context). The gNB keeps the last limit after a codelet is
// unloaded, so bufcap.sh loads bufcap_off for a moment to undo a cap.
//
// The gNB clamps the byte limit to [one max PDCP PDU (~9 kB), configured queue-bytes].

#include <linux/bpf.h>
#include <string.h>

#include "jbpf_srsran_contexts.h"

#include "../utils/misc_utils.h"

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

#ifndef CAP_BYTES
#define CAP_BYTES (65536)
#endif

// Layout MUST MATCH struct jbpf_rlc_ctrl_info in
// ocudu-jbpf/include/srsran/jbpf/jbpf_srsran_contexts.h (see bufctl/rlc_ctrl.cpp).
struct jbpf_rlc_ctrl_info {
    uint16_t du_ue_index;
    uint8_t  is_srb;           // always 0: the hook only fires for DRBs
    uint8_t  rb_id;            // DRB id
    uint32_t cur_byte_limit;   // gNB -> codelet
    uint32_t new_byte_limit;   // codelet -> gNB (0 = leave unchanged)
    uint32_t cur_sdu_limit;    // gNB -> codelet
    uint32_t new_sdu_limit;    // codelet -> gNB (0 = leave unchanged)
    uint32_t cfg_byte_limit;   // gNB -> codelet: configured byte limit
    uint32_t cfg_sdu_limit;    // gNB -> codelet: configured SDU limit
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
#if CAP_BYTES == 0
    ci->new_byte_limit = ci->cfg_byte_limit;
    ci->new_sdu_limit  = ci->cfg_sdu_limit;
#else
    ci->new_byte_limit = CAP_BYTES;
#endif
    return JBPF_CODELET_SUCCESS;
}
