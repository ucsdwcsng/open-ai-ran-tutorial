// dl_sdu_count: count the downlink SDUs and bytes that enter RLC, per UE, and report them
// once per period (2^30 ns, about 1.07 s).
// Hook: rlc_dl_new_sdu (monitor). ctx->data is a jbpf_rlc_ctx_info, and the hook packs
// sdu_length << 32 | pdcp_sn into srs_meta_data1.

#include <linux/bpf.h>

#include "jbpf_srsran_contexts.h"
#include "dl_sdu_count.pb.h"

#include "../utils/misc_utils.h"   // JBPF_CODELET_SUCCESS, JBPF_CODELET_FAILURE

#define SEC(NAME) __attribute__((section(NAME), used))

#include "jbpf_defs.h"
#include "jbpf_helper.h"

#define MAX_UE       16
#define PERIOD_SHIFT 30

struct ue_count {
    uint64_t period;   // the period these counts belong to
    uint64_t bytes;
    uint32_t sdus;
};

// Per-UE counters, indexed by du_ue_index.
struct jbpf_load_map_def SEC("maps") counts = {
    .type        = JBPF_MAP_TYPE_ARRAY,
    .key_size    = sizeof(uint32_t),
    .value_size  = sizeof(struct ue_count),
    .max_entries = MAX_UE,
};

// Output channel to the xApp, one dl_sdu_count message per entry.
jbpf_ringbuf_map(out_counts, dl_sdu_count, 64);

extern "C" SEC("jbpf_srsran_generic")
uint64_t jbpf_main(void* state)
{
    struct jbpf_ran_generic_ctx* ctx = (struct jbpf_ran_generic_ctx*)state;
    const struct jbpf_rlc_ctx_info* rlc = (const struct jbpf_rlc_ctx_info*)ctx->data;

    // The verifier rejects any read of ctx->data without this check.
    if ((const uint8_t*)rlc + sizeof(*rlc) > (const uint8_t*)ctx->data_end) {
        return JBPF_CODELET_FAILURE;
    }
    // Count data bearers only.
    if (rlc->is_srb) {
        return JBPF_CODELET_SUCCESS;
    }

    uint32_t ue = rlc->du_ue_index % MAX_UE;
    struct ue_count* c = (struct ue_count*)jbpf_map_lookup_elem(&counts, &ue);
    if (!c) {
        return JBPF_CODELET_FAILURE;
    }

    // This UE's first SDU in a new period: send the last period's counts, then start over.
    uint64_t period = jbpf_time_get_ns() >> PERIOD_SHIFT;
    if (c->period != period) {
        if (c->sdus > 0) {
            dl_sdu_count msg = {};
            msg.timestamp   = period << PERIOD_SHIFT;
            msg.du_ue_index = ue;
            msg.sdus        = c->sdus;
            msg.bytes       = c->bytes;
            jbpf_ringbuf_output(&out_counts, &msg, sizeof(msg));
        }
        c->period = period;
        c->sdus   = 0;
        c->bytes  = 0;
    }

    c->sdus += 1;
    c->bytes += ctx->srs_meta_data1 >> 32;
    return JBPF_CODELET_SUCCESS;
}
