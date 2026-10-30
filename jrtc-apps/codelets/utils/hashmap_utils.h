// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

#ifndef HASHMAP_UTILS_H
#define HASHMAP_UTILS_H

#include <stdint.h>

#define JBPF_HIST_BIN(val, max_hist_size, hist_shift) ({ \
  val = val >> hist_shift; \
  val = (val > max_hist_size) ? max_hist_size : val; \
  val; \
})

#define JBPF_OUT_OF_RANGE_ERROR  (0x8000 + 1)
#define JBPF_MAP_UPDATE_ERROR    (0x8000 + 2)

#define _DEFINE_PROTOHASH(hashmap, ksize, max_entry) \
  struct jbpf_load_map_def SEC("maps") hashmap = { \
    .type = JBPF_MAP_TYPE_HASHMAP, \
    .key_size = ksize, \
    .value_size = sizeof(uint32_t), \
    .max_entries = max_entry, \
  }; 

#define DEFINE_PROTOHASH_64(hashmap, max_entry) \
  _DEFINE_PROTOHASH(hashmap, sizeof(uint64_t), max_entry) \
  typedef const uint64_t __##hashmap##_type;

#define DEFINE_PROTOHASH_32(hashmap, max_entry) \
  _DEFINE_PROTOHASH(hashmap, sizeof(uint32_t), max_entry) \
  typedef const uint32_t __##hashmap##_type;

#define IS_POWER_OF_2(v) ((v) && (((v) & ((v) - 1)) == 0))
#define LOG2_OF_POWER_OF_2(n) ((uint32_t)__builtin_ctz(n))

#define MAX(a,b) ((a) > (b) ? (a) : (b))

#define JBPF_PROTOHASH_LOOKUP_ELEM_32(out, hist, hashmap, key, is_new) ({\
  _Static_assert(IS_POWER_OF_2(sizeof(out->hist) / sizeof(out->hist[0])), "histogram length has to be power of 2"); \
  _Static_assert(sizeof(__##hashmap##_type) == 4, "This hashmap should have 32-bit keys"); \
  uint32_t compound_key = (uint32_t)key; \
  uint32_t *pind = (uint32_t *)jbpf_map_lookup_elem(&hashmap, &compound_key); \
  uint32_t ind; \
  if (!pind) { \
      ind = out->hist##_count; \
      int res = jbpf_map_update_elem(&hashmap, &compound_key, &ind, 0); \
      if (res != JBPF_MAP_SUCCESS) return JBPF_MAP_UPDATE_ERROR; \
      if (ind >= (uint32_t)(sizeof(out->hist) / sizeof(out->hist[0]))) return JBPF_OUT_OF_RANGE_ERROR; \
      *(uint32_t *)(&out->hist[ind & (sizeof(out->hist) / sizeof(out->hist[0]) - 1)]) = (uint32_t)key; \
      is_new = 1; \
      out->hist##_count++; \
  } else { \
      is_new = 0; \
      ind = *pind; \
  } \
  ind & (sizeof(out->hist) / sizeof(out->hist[0]) - 1) ; \
})

#define JBPF_PROTOHASH_COMPOUND_KEY_64(key1, key2) ((((uint64_t)(key2)) << 31) << 1 | (uint64_t)(key1))

#define JBPF_PROTOHASH_LOOKUP_ELEM_64(out, hist, hashmap, key1, key2, is_new) ({\
  _Static_assert(IS_POWER_OF_2(sizeof(out->hist) / sizeof(out->hist[0])), "histogram length has to be power of 2"); \
  _Static_assert(sizeof(__##hashmap##_type) == 8, "This hashmap should have 64-bit keys"); \
  uint64_t compound_key = JBPF_PROTOHASH_COMPOUND_KEY_64(key1, key2); \
  uint32_t *pind = (uint32_t *)jbpf_map_lookup_elem(&hashmap, &compound_key); \
  uint32_t ind; \
  if (!pind) { \
      ind = out->hist##_count; \
      int res = jbpf_map_update_elem(&hashmap, &compound_key, &ind, 0); \
      if (res != JBPF_MAP_SUCCESS) return JBPF_MAP_UPDATE_ERROR; \
      if (ind >= (uint32_t)(sizeof(out->hist) / sizeof(out->hist[0]))) return JBPF_OUT_OF_RANGE_ERROR; \
      *(uint64_t *)(&out->hist[ind & (uint32_t)(sizeof(out->hist) / sizeof(out->hist[0]) - 1) ]) = compound_key; \
      is_new = 1; \
      out->hist##_count++; \
  } else { \
      is_new = 0; \
      ind = *pind; \
  } \
  ind & (uint32_t)(sizeof(out->hist) / sizeof(out->hist[0]) - 1); \
})



#define JBPF_PROTOHASH_REMOVE_ELEM_64(out, hist, hashmap, key1, key2) ({\
  _Static_assert(IS_POWER_OF_2(sizeof(out->hist) / sizeof(out->hist[0])), "histogram length has to be power of 2"); \
  _Static_assert(sizeof(__##hashmap##_type) == 8, "This hashmap should have 64-bit keys"); \
  uint64_t compound_key = JBPF_PROTOHASH_COMPOUND_KEY_64(key1, key2); \
  int ret = jbpf_map_delete_elem(&hashmap, &compound_key); \
  if (ret == JBPF_MAP_ERROR) return JBPF_MAP_UPDATE_ERROR; \
  ret; \
})



#define JBPF_HASHMAP_LOOKUP_UPDATE_UINT32_ELEM(hist, key, default_val) ({\
  uint32_t *val = (uint32_t *)jbpf_map_lookup_elem(hist, key); \
  if (!val) { \
      uint32_t ctmp = default_val; \
      int res = jbpf_map_update_elem(hist, key, &ctmp, 0); \
      if (res == JBPF_MAP_SUCCESS) { \
          val = (uint32_t *)jbpf_map_lookup_elem(hist, key); \
          if (!val) return 1; \
      } else return JBPF_MAP_UPDATE_ERROR; \
  } \
  val; \
})


#define JBPF_HASHMAP_CLEAR(hist) ({\
  int res = jbpf_map_try_clear(hist); \
  if (res != JBPF_MAP_SUCCESS) return 1; \
  res; \
})


#endif // HASHMAP_UTILS_H
