// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

#ifndef NET_UTILS_H
#define NET_UTILS_H

#include <stdint.h>

#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
#define jbpf_ntohs(x) __builtin_bswap16(x)
#define jbpf_htons(x) __builtin_bswap16(x)
#define jbpf_ntohl(x) __builtin_bswap32(x)
#define jbpf_htonl(x) __builtin_bswap32(x)
#elif __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
#define jbpf_ntohs(x) (x)
#define jbpf_htons(x) (x)
#define jbpf_ntohl(x) (x)
#define jbpf_htonl(x) (x)
#endif

#endif // NET_UTILS_H

