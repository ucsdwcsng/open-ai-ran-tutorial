// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.
//
// rlc_ctrl - executes per-(UE, DRB) RLC DL buffer-size commands from an xApp.
//
// HOOK: rlc_dl_ctrl (CONTROL hook in rlc_tx_am_entity::handle_sdu). Fires once per
// DL SDU on each RLC AM DRB, before the enqueue/tail-drop decision. The gNB does
// not call it for SRBs.
//
// Roles:
//   xApp    decides WHICH bearer (du_ue_index, drb_id) and WHAT to do, and sends an
//           rlc_ctrl_msg on the ctrl_in control-input channel.
//   codelet (this file) stores the command and acts on that bearer's next DL SDU.
//           It makes no decisions of its own.
//   gNB     clamps and applies: byte limit to [one max PDCP PDU, configured queue-bytes],
//           SDU limit to [1, configured queue-size].
//
// Operations (rlc_ctrl_msg.op):
//   SET (0)   apply byte_limit and/or sdu_limit (a 0 field keeps the stored value)
//   RESET (1) write the bearer's CONFIGURED limits back (cfg_* from the hook context),
//             so an xApp never needs to know or store the defaults
//   GET (2)   emit one report for that bearer and change nothing
//
// Reports (out_rlc_ctrl) are emitted when: GET is requested, RESET is applied, or the
// limits in force changed since the last report -- which is how an xApp confirms a SET
// actually took effect. Nothing is emitted while a bearer is idle: the hook only runs
// when a packet arrives.
//
// IMPORTANT: unload this codelet when you are not commanding. While it is loaded the
// hook runs on every DL packet of every AM DRB. Unloading leaves the applied limits in
// place (send RESET first if you want the configured sizes back).
//
// TODO(scout): RLC UM DRBs have no rlc_dl_ctrl hook yet; only AM DRBs can be controlled.
// Full TODO list: RLC-CTRL-CHANGES.md; API reference: rlc-buffer-control-hook.md.

#include <linux/bpf.h>
#include <string.h>

#include "jbpf_srsran_contexts.h"
#include "rlc_ctrl_report.pb.h"

#include "../utils/misc_utils.h"

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

// Layout MUST MATCH struct jbpf_rlc_ctrl_info in
// ocudu-jbpf/include/srsran/jbpf/jbpf_srsran_contexts.h. Defined locally because
// codelets build against the SDK image's headers, not the modified gNB source
// tree. The gNB passes a pointer to its instance as ctx.data; the two
// definitions are the wire contract for that pointer.
struct jbpf_rlc_ctrl_info {
    uint16_t du_ue_index;
    uint8_t  is_srb;           // always 0: the hook only fires for DRBs
    uint8_t  rb_id;            // DRB id
    uint32_t cur_byte_limit;   // gNB -> codelet
    uint32_t new_byte_limit;   // codelet -> gNB (0 = leave unchanged)
    uint32_t cur_sdu_limit;    // gNB -> codelet
    uint32_t new_sdu_limit;    // codelet -> gNB (0 = leave unchanged)
    uint32_t cfg_byte_limit;   // gNB -> codelet: configured byte limit
    uint32_t cfg_sdu_limit;    // gNB -> codelet: configured SDU limit (= queue capacity)
};

// Control message from the xApp: 5 little-endian uint32 fields, 20 bytes.
struct rlc_ctrl_msg {
    uint32_t du_ue_index;  // target UE, 0 .. RLC_CTRL_MAX_UES-1
    uint32_t op;           // RLC_CTRL_OP_SET / _RESET / _GET
    uint32_t rb_id;        // target DRB id, 1 .. RLC_CTRL_MAX_DRBS
    uint32_t byte_limit;   // SET only; 0 = keep the stored value
    uint32_t sdu_limit;    // SET only; 0 = keep the stored value
};

#define RLC_CTRL_OP_SET   (0)
#define RLC_CTRL_OP_RESET (1)
#define RLC_CTRL_OP_GET   (2)

// Report reasons (rlc_ctrl_report.reason)
#define RLC_CTRL_REASON_CHANGED (0)
#define RLC_CTRL_REASON_GET     (1)
#define RLC_CTRL_REASON_RESET   (2)

// CONTROL_INPUT channel: xApp -> codelet. Depth 32 messages.
jbpf_control_input_map(ctrl_in, rlc_ctrl_msg, 32);

// Report channel: codelet -> xApp.
jbpf_ringbuf_map(out_rlc_ctrl, rlc_ctrl_report, 64);

#define RLC_CTRL_MAX_UES  (64)   // controllable du_ue_index values: 0..63
#define RLC_CTRL_MAX_DRBS (29)   // DRB ids 1..29 (OCUDU MAX_NOF_DRBS)

#define RLC_CTRL_F_GET   (1u << 0)   // a GET is pending for this bearer
#define RLC_CTRL_F_RESET (1u << 1)   // a RESET is pending for this bearer

struct rlc_ctrl_entry {
    uint32_t byte_limit;   // commanded byte limit; 0 = none
    uint32_t sdu_limit;    // commanded SDU limit; 0 = none
    uint32_t flags;        // RLC_CTRL_F_*
    uint32_t rep_byte;     // cur_byte_limit as last reported
    uint32_t rep_sdu;      // cur_sdu_limit as last reported
};

// Latest command and report state per (UE, DRB).
// Index = du_ue_index * RLC_CTRL_MAX_DRBS + (drb_id - 1).
struct jbpf_load_map_def SEC("maps") limit_map = {
    .type = JBPF_MAP_TYPE_ARRAY,
    .key_size = sizeof(uint32_t),
    .value_size = sizeof(struct rlc_ctrl_entry),
    .max_entries = RLC_CTRL_MAX_UES * RLC_CTRL_MAX_DRBS,
};

// Sets *key and returns 0 if (du_ue_index, drb_id) is in range; returns -1 otherwise.
static inline int ue_drb_key(uint32_t du_ue_index, uint32_t drb_id, uint32_t* key)
{
    if (du_ue_index >= RLC_CTRL_MAX_UES || drb_id < 1 || drb_id > RLC_CTRL_MAX_DRBS) {
        return -1;
    }
    *key = du_ue_index * RLC_CTRL_MAX_DRBS + (drb_id - 1);
    return 0;
}

extern "C" SEC("jbpf_srsran_generic")
uint64_t jbpf_main(void* state)
{
    struct jbpf_ran_generic_ctx* ctx = (jbpf_ran_generic_ctx*)state;

    struct jbpf_rlc_ctrl_info* ci = (struct jbpf_rlc_ctrl_info*)ctx->data;
    if (reinterpret_cast<uint8_t*>(ci) + sizeof(struct jbpf_rlc_ctrl_info) >
        reinterpret_cast<uint8_t*>(ctx->data_end)) {
        return JBPF_CODELET_FAILURE;
    }

    // 1. Store pending xApp commands. Bounded loop (verifier-friendly): at most 8 per invocation.
    struct rlc_ctrl_msg msg;
    uint32_t key;
    #pragma unroll
    for (int n = 0; n < 8; n++) {
        if (jbpf_control_input_receive(&ctrl_in, &msg, sizeof(msg)) <= 0) {
            break;
        }
        if (ue_drb_key(msg.du_ue_index, msg.rb_id, &key) != 0) {
            continue;   // out-of-range target: ignore
        }
        struct rlc_ctrl_entry* e = (struct rlc_ctrl_entry*)jbpf_map_lookup_elem(&limit_map, &key);
        if (!e) {
            continue;
        }
        if (msg.op == RLC_CTRL_OP_SET) {
            if (msg.byte_limit != 0) {
                e->byte_limit = msg.byte_limit;
            }
            if (msg.sdu_limit != 0) {
                e->sdu_limit = msg.sdu_limit;
            }
        } else if (msg.op == RLC_CTRL_OP_RESET) {
            e->flags |= RLC_CTRL_F_RESET;
        } else if (msg.op == RLC_CTRL_OP_GET) {
            e->flags |= RLC_CTRL_F_GET;
        }
        // any other op: ignored
    }

    // 2. Act on THIS bearer.
    if (ci->is_srb || ue_drb_key((uint32_t)ci->du_ue_index, (uint32_t)ci->rb_id, &key) != 0) {
        return JBPF_CODELET_SUCCESS;
    }
    struct rlc_ctrl_entry* e = (struct rlc_ctrl_entry*)jbpf_map_lookup_elem(&limit_map, &key);
    if (!e) {
        return JBPF_CODELET_SUCCESS;
    }

    uint32_t reason   = RLC_CTRL_REASON_CHANGED;
    uint32_t do_report = 0;

    if ((e->flags & RLC_CTRL_F_RESET) != 0) {
        // RESET: hand the configured limits straight back and forget any stored command.
        ci->new_byte_limit = ci->cfg_byte_limit;
        ci->new_sdu_limit  = ci->cfg_sdu_limit;
        e->byte_limit = 0;
        e->sdu_limit  = 0;
        e->flags &= ~RLC_CTRL_F_RESET;
        reason    = RLC_CTRL_REASON_RESET;
        do_report = 1;
    } else {
        // SET: apply whatever is stored (0 = leave unchanged).
        if (e->byte_limit != 0) {
            ci->new_byte_limit = e->byte_limit;
        }
        if (e->sdu_limit != 0) {
            ci->new_sdu_limit = e->sdu_limit;
        }
    }

    if ((e->flags & RLC_CTRL_F_GET) != 0) {
        e->flags &= ~RLC_CTRL_F_GET;
        reason    = RLC_CTRL_REASON_GET;
        do_report = 1;
    }

    // Report whenever the limits in force have moved since the last report. After a SET this
    // fires on the packet after the gNB applied it, so the xApp sees the value that took effect
    // (clamped if it was out of range).
    if (ci->cur_byte_limit != e->rep_byte || ci->cur_sdu_limit != e->rep_sdu) {
        do_report = 1;
    }

    if (do_report != 0) {
        rlc_ctrl_report rep;
        memset(&rep, 0, sizeof(rep));
        rep.timestamp      = jbpf_time_get_ns();
        rep.du_ue_index    = ci->du_ue_index;
        rep.rb_id          = ci->rb_id;
        rep.cur_byte_limit = ci->cur_byte_limit;
        rep.cur_sdu_limit  = ci->cur_sdu_limit;
        rep.cfg_byte_limit = ci->cfg_byte_limit;
        rep.cfg_sdu_limit  = ci->cfg_sdu_limit;
        rep.reason         = reason;
        jbpf_ringbuf_output(&out_rlc_ctrl, (void*)&rep, sizeof(rep));
        e->rep_byte = ci->cur_byte_limit;
        e->rep_sdu  = ci->cur_sdu_limit;
    }

    return JBPF_CODELET_SUCCESS;
}
