// Copyright (c) Microsoft Corporation. Licensed under the MIT license.
//
// EdgeRIC-RT wire contract (per-TTI synchronized DL scheduling, EdgeRIC NSDI'24 RT-E2 over jbpf).
//
// The rt_* contexts are byte-for-byte copies of the gNB structs in
// ocudu-jbpf/include/srsran/jbpf/jbpf_srsran_contexts.h (jbpf_mac_sched_slot_ctrl_info,
// jbpf_mac_sched_slot_report_info): codelets build against the SDK image headers, which do not have
// them. rt_action_msg is the action the bridge xApp writes into the rt_act_in control channel; the
// muApp packs the same layout (edgeric-rt/edgeric_rt/wire.py). Keep all three in step.

#ifndef EDGERIC_RT_WIRE_H
#define EDGERIC_RT_WIRE_H

#include <stdint.h>

#define RT_MAX_UES (16)

// Slot status (= JBPF_RT_STATUS_*).
#define RT_STATUS_NONE (0)
#define RT_STATUS_HIT  (1)
#define RT_STATUS_HOLD (2)

#define RT_ACTION_MAGIC (0xE5C1)
#define RT_ACTION_VER   (1)

// Ring of pending actions, indexed by target TTI. An action more than RT_RING - 1 TTIs ahead is dropped.
#define RT_RING (16)

// Per-UE entry of an action (= jbpf_rt_ue_action). 8 bytes.
struct rt_ue_action {
    uint32_t weight;
    uint16_t du_ue_index;
    uint8_t  flags;
    uint8_t  reserved;
};

// mac_sched_slot_ctrl context (= jbpf_mac_sched_slot_ctrl_info). 208 bytes.
struct rt_ctrl_ctx {
    uint64_t t_slot_ns;
    uint32_t ran_tti;
    uint16_t cell_id;
    uint8_t  is_dl;
    uint8_t  pad0;
    uint32_t status;
    uint32_t act_seq;
    uint32_t act_tti;
    uint32_t act_src_tti;
    uint32_t act_flags;
    uint32_t n;
    uint64_t act_t_src_report_ns;
    uint64_t act_t_sent_ns;
    uint32_t n_rx;
    uint32_t n_late;
    uint32_t n_early;
    uint32_t n_overwritten;
    uint32_t n_bad;
    uint32_t n_hit;
    struct rt_ue_action ue[RT_MAX_UES];
};

// Per-UE entry of the slot report (= jbpf_rt_ue_report = nanopb rt_ue). 60 bytes.
struct rt_report_ue {
    uint32_t du_ue_index;
    uint32_t rnti;
    uint32_t cqi;
    uint32_t eff_cqi_x100;
    uint32_t olla_mcs;
    uint32_t dl_pending_bytes;
    uint32_t free_harqs;
    uint32_t dl_prbs;
    uint32_t newtx_tbs;
    uint32_t retx_tbs;
    uint32_t dl_mcs;
    uint32_t drb_bytes;
    uint32_t rb_quota;
    uint32_t weight;
    uint32_t flags;
};

// mac_sched_slot_report context (= jbpf_mac_sched_slot_report_info = nanopb rt_report). 1080 bytes.
struct rt_report_ctx {
    uint64_t t_slot_ns;
    uint64_t t_report_ns;
    uint64_t act_t_src_report_ns;
    uint64_t act_t_sent_ns;
    uint32_t ran_tti;
    uint32_t sfn;
    uint32_t slot;
    uint32_t is_dl;
    uint32_t dl_mask;
    uint32_t dl_rbs_avail;
    uint32_t dl_rbs_used;
    uint32_t n_weighted;
    uint32_t n_sel;
    uint32_t act_status;
    uint32_t act_seq;
    uint32_t act_tti;
    uint32_t act_src_tti;
    uint32_t act_flags;
    uint32_t n_rx;
    uint32_t n_late;
    uint32_t n_early;
    uint32_t n_overwritten;
    uint32_t n_bad;
    uint32_t n_hit;
    uint32_t cell_id;
    uint16_t ue_count;
    uint16_t pad0;
    struct rt_report_ue ue[RT_MAX_UES];
};

// Action for one target TTI (muApp -> bridge xApp -> rt_act_in). 168 bytes, little endian.
struct rt_action_msg {
    uint16_t magic;           // RT_ACTION_MAGIC
    uint8_t  ver;             // RT_ACTION_VER
    uint8_t  n;               // valid entries in ue[], <= RT_MAX_UES
    uint32_t seq;             // muApp action sequence number (echoed in the report of the TTI it is applied in)
    uint32_t tti;             // target RANtime: applied only in exactly this TTI
    uint32_t src_tti;         // RANtime of the report the action was computed from
    uint32_t flags;           // JBPF_RT_FLAG_* (bit0 work-conserving, bit1 greedy PDCCH)
    uint32_t hold;            // TTIs the action may be re-applied (HOLD) after its HIT; 0 = exact TTI only
    uint64_t t_src_report_ns; // echo of the source report's t_report_ns
    uint64_t t_sent_ns;       // CLOCK_MONOTONIC when the bridge wrote it into the channel
    struct rt_ue_action ue[RT_MAX_UES];
};

#ifdef __cplusplus
static_assert(sizeof(struct rt_ue_action) == 8, "rt_ue_action layout");
static_assert(sizeof(struct rt_ctrl_ctx) == 208, "rt_ctrl_ctx layout");
static_assert(__builtin_offsetof(struct rt_ctrl_ctx, ue) == 80, "rt_ctrl_ctx layout");
static_assert(sizeof(struct rt_report_ue) == 60, "rt_report_ue layout");
static_assert(sizeof(struct rt_report_ctx) == 1080, "rt_report_ctx layout");
static_assert(__builtin_offsetof(struct rt_report_ctx, ue_count) == 116, "rt_report_ctx layout");
static_assert(__builtin_offsetof(struct rt_report_ctx, ue) == 120, "rt_report_ctx layout");
static_assert(sizeof(struct rt_action_msg) == 168, "rt_action_msg layout");
static_assert(__builtin_offsetof(struct rt_action_msg, t_src_report_ns) == 24, "rt_action_msg layout");
static_assert(__builtin_offsetof(struct rt_action_msg, ue) == 40, "rt_action_msg layout");
#endif

#endif // EDGERIC_RT_WIRE_H
