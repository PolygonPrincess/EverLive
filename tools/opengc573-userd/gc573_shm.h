/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef GC573_SHM_H
#define GC573_SHM_H

#include <stdint.h>
#include <stdbool.h>

#define GC573_SHM_NAME "/gc573_v4l2_shm"
#define GC573_SHM_MAGIC 0x47433537U /* "GC57" */

/* Support up to 4K (3840x2160) YUY2 (2 bytes per pixel) */
#define GC573_SHM_MAX_WIDTH  3840U
#define GC573_SHM_MAX_HEIGHT 2160U
#define GC573_SHM_FRAME_BYTES (GC573_SHM_MAX_WIDTH * GC573_SHM_MAX_HEIGHT * 2U)

/**
 * struct gc573_shm_port - Single capture port frame buffer with seqlock
 *
 * Seqlock Protocol:
 * - ODD `seq`: Publisher is actively mutating the frame or metadata.
 * - EVEN `seq`: Buffer contains a consistent, fully written frame.
 *
 * Publisher must store (seq + 1) with __ATOMIC_RELEASE before writing,
 * followed by a memory barrier, copy the payload, and finally store (seq + 2)
 * with __ATOMIC_RELEASE.
 *
 * Readers must load `seq1` with __ATOMIC_ACQUIRE, verify it is even, read the
 * frame with acquire fences, and verify `seq2 == seq1` with __ATOMIC_ACQUIRE.
 */
struct gc573_shm_port {
	uint64_t seq;
	uint64_t timestamp_ns;
	uint32_t width;
	uint32_t height;
	uint32_t no_signal;
	uint32_t streaming;
	uint32_t fps_num;
	uint32_t fps_den;
	uint8_t  yuy2[GC573_SHM_FRAME_BYTES];
};

struct gc573_shm_region {
	uint32_t magic;
	uint32_t version;
	char pci_bdf[32];
	struct gc573_shm_port port;

	/*
	 * Liveness beacon for consumers.  Appended after the frame payload so
	 * existing width/height/seq offsets stay stable.  heartbeat_ns is
	 * CLOCK_MONOTONIC; a consumer that sees it older than ~1 s knows the
	 * publisher died and should emit a black frame instead of a stale one.
	 */
	uint32_t publisher_pid;
	uint32_t reserved;
	uint64_t heartbeat_ns;
};

#endif /* GC573_SHM_H */
