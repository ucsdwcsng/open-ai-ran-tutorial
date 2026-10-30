// Copyright (c) Microsoft Corporation. Licensed under the MIT license.
//
// l4span_mark - L4S in-place ECN CE marking of DL DRB IP packets, PER (UE, DRB).
//
// HOOK: pdcp_dl_sdu_segment (CTRL hook; ctx.data = writable first segment of the
// DL IP packet; srs_meta_data1 = (cu_up_ue_index << 32) | rb_id). For ECT IPv4
// packets we set ECN CE with the per-(UE,DRB) marking probability maintained by
// the signal codelet (l4span_rate or codel_rate) in the shared `l4s_map`, fixing
// the IPv4 checksum incrementally (RFC 1624).
//
// Keying: slot = ((ue << 4) | (rb & 0xF)) % L4S_MAX. On this gNB the CU-UP PDCP
// ue_index equals the DU RLC du_ue_index (both assigned in attach order; verified
// via F1/E1 logs), so the mark side (cu_up_ue_index) and the signal side
// (du_ue_index) address the same slot for a given UE's DRB.

#include <linux/bpf.h>
#include <string.h>

#include "jbpf_srsran_contexts.h"

#include "../utils/misc_utils.h"

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

#define L4S_MAX (64)

// per-(UE,DRB) state, shared with the signal codelet. markprob = per-mille;
// s1/s2 are signal-codelet scratch (EWMA delay for l4span_rate; CoDel timers).
struct l4s_state {
    uint32_t markprob;
    uint32_t s1;
    uint32_t s2;
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

#define IP4_HDR_MIN (20)

extern "C" SEC("jbpf_srsran_generic")
uint64_t jbpf_main(void* state)
{
    struct jbpf_ran_generic_ctx* ctx = (jbpf_ran_generic_ctx*)state;

    uint8_t* p   = (uint8_t*)ctx->data;
    uint8_t* end = (uint8_t*)ctx->data_end;
    if (p + IP4_HDR_MIN > end) {
        return JBPF_CODELET_SUCCESS;
    }
    if ((p[0] >> 4) != 4) {
        return JBPF_CODELET_SUCCESS;   // IPv4 only
    }
    uint8_t ecn = (uint8_t)(p[1] & 0x3);
    if (ecn == 0x0 || ecn == 0x3) {
        return JBPF_CODELET_SUCCESS;   // Not-ECT (can't mark) or already CE
    }

    // per-(UE,DRB) marking probability
    uint32_t ue   = (uint32_t)(ctx->srs_meta_data1 >> 32);
    uint32_t rb   = (uint32_t)(ctx->srs_meta_data1 & 0xFFFFFFFF);
    uint32_t slot = l4s_slot(ue, rb);
    struct l4s_state* st = (struct l4s_state*)jbpf_map_lookup_elem(&l4s_map, &slot);
    if (!st) {
        return JBPF_CODELET_SUCCESS;
    }
    uint32_t prob = st->markprob;
    if (prob == 0) {
        return JBPF_CODELET_SUCCESS;
    }

    uint32_t r = (uint32_t)(jbpf_time_get_ns() & 0x3FF);   // 0..1023
    if (r >= prob) {
        return JBPF_CODELET_SUCCESS;
    }

    // Set CE (ECN = 0b11) and repair the IPv4 checksum incrementally (RFC 1624).
    uint8_t old_tos = p[1];
    uint8_t new_tos = (uint8_t)(old_tos | 0x3);
    p[1] = new_tos;

    uint32_t old_word = ((uint32_t)p[0] << 8) | old_tos;
    uint32_t new_word = ((uint32_t)p[0] << 8) | new_tos;
    uint32_t hc       = (~(((uint32_t)p[10] << 8) | p[11])) & 0xFFFF;
    uint32_t s        = hc + ((~old_word) & 0xFFFF) + (new_word & 0xFFFF);
    s = (s & 0xFFFF) + (s >> 16);
    s = (s & 0xFFFF) + (s >> 16);
    uint16_t newhc = (uint16_t)(~s & 0xFFFF);
    p[10] = (uint8_t)(newhc >> 8);
    p[11] = (uint8_t)(newhc & 0xFF);

    return JBPF_CODELET_SUCCESS;
}
