// Copyright (c) Microsoft Corporation. Licensed under the MIT license.
//
// rt_report - EdgeRIC-RT per-TTI report (RT-E2 Report).
//
// HOOK: mac_sched_slot_report (once per cell per slot, after the slot is scheduled). ctx.data points at
// the gNB's rt_report_ctx: RANtime, the state of every UE, this slot's grants and the echo of the action
// that was applied. Its layout is the nanopb rt_report struct, so it goes out to the bridge xApp as is,
// every TTI.

#include <linux/bpf.h>
#include <string.h>

#include "jbpf_srsran_contexts.h"
#include "rt_report.pb.h"
#include "rt_wire.h"

#include "../utils/misc_utils.h"

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

static_assert(sizeof(rt_report) == sizeof(struct rt_report_ctx), "rt_report_ctx must match nanopb rt_report");
static_assert(sizeof(rt_ue) == sizeof(struct rt_report_ue), "rt_report_ue must match nanopb rt_ue");
static_assert(__builtin_offsetof(rt_report, ran_tti) == __builtin_offsetof(struct rt_report_ctx, ran_tti), "layout");
static_assert(__builtin_offsetof(rt_report, cell_id) == __builtin_offsetof(struct rt_report_ctx, cell_id), "layout");
static_assert(__builtin_offsetof(rt_report, ue_count) == __builtin_offsetof(struct rt_report_ctx, ue_count), "layout");
static_assert(__builtin_offsetof(rt_report, ue) == __builtin_offsetof(struct rt_report_ctx, ue), "layout");
static_assert(__builtin_offsetof(rt_ue, flags) == __builtin_offsetof(struct rt_report_ue, flags), "layout");

// Enough for a few ms of reports if the bridge stalls; the bridge keeps only the newest anyway.
jbpf_ringbuf_map(out_rt_report, rt_report, 256);

// Output back-off: with no consumer every output fails and jbpf logs an error from the scheduler thread,
// so after RT_FAIL_STREAK failures in a row the codelet skips RT_SKIP_SLOTS slots before trying again.
#define RT_FAIL_STREAK (8)
#define RT_SKIP_SLOTS  (1000)

struct rt_report_state {
    uint32_t fail_streak;
    uint32_t skip;
    uint32_t sent;
    uint32_t failed;
};

struct jbpf_load_map_def SEC("maps") rt_report_state_map = {
    .type        = JBPF_MAP_TYPE_ARRAY,
    .key_size    = sizeof(uint32_t),
    .value_size  = sizeof(struct rt_report_state),
    .max_entries = 1,
};

extern "C" SEC("jbpf_srsran_generic")
uint64_t jbpf_main(void* state)
{
    struct jbpf_ran_generic_ctx* ctx = (jbpf_ran_generic_ctx*)state;

    void* rep = (void*)ctx->data;
    if (reinterpret_cast<uint8_t*>(rep) + sizeof(rt_report) > reinterpret_cast<uint8_t*>(ctx->data_end)) {
        return JBPF_CODELET_FAILURE;
    }

    uint32_t zero = 0;
    struct rt_report_state* st = (struct rt_report_state*)jbpf_map_lookup_elem(&rt_report_state_map, &zero);
    if (!st) {
        return JBPF_CODELET_FAILURE;
    }
    if (st->skip > 0) {
        st->skip--;
        return JBPF_CODELET_SUCCESS;
    }

    if (jbpf_ringbuf_output(&out_rt_report, rep, sizeof(rt_report)) < 0) {
        st->failed++;
        st->fail_streak++;
        if (st->fail_streak >= RT_FAIL_STREAK) {
            st->fail_streak = 0;
            st->skip        = RT_SKIP_SLOTS;
        }
    } else {
        st->sent++;
        st->fail_streak = 0;
    }
    return JBPF_CODELET_SUCCESS;
}
