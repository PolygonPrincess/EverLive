/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * test_edid_unifier.c — Unit tests for GC573 EDID Audio-Unifier Profile Builder
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

#include "../tools/opengc573-userd/gc573-user-compat.h"
#include "../tools/opengc573-userd/gc573_user_edid.h"

/* Stub I2C & HW dependencies for standalone compilation of gc573_user_edid */
int gc573_user_i2c_write(void *bar0, unsigned int bus, u16 addr,
			 u32 subaddr, unsigned int subaddr_len,
			 const u8 *data, unsigned int data_len)
{
	(void)bar0; (void)bus; (void)addr; (void)subaddr; (void)subaddr_len;
	(void)data; (void)data_len;
	return 0;
}

int gc573_user_i2c_read(void *bar0, unsigned int bus, u16 addr,
			u32 subaddr, unsigned int subaddr_len,
			u8 *data, unsigned int data_len)
{
	(void)bar0; (void)bus; (void)addr; (void)subaddr; (void)subaddr_len;
	(void)data; (void)data_len;
	return 0;
}

int gc573_user_set_gpio(void *bar0, unsigned int pin, bool high, unsigned int delay_ms)
{
	(void)bar0; (void)pin; (void)high; (void)delay_ms;
	return 0;
}

int gc573_user_get_gpio(void *bar0, unsigned int pin, bool *high)
{
	(void)bar0; (void)pin;
	if (high)
		*high = false;
	return 0;
}

int gc573_user_it6805_select_bank(void *bar0, u8 bank)
{
	(void)bar0; (void)bank;
	return 0;
}

int gc573_user_it6805_set_bank(void *bar0, u8 bank)
{
	(void)bar0; (void)bank;
	return 0;
}

#include "../tools/opengc573-userd/gc573_user_edid.c"

static int g_failures = 0;

#define TEST_ASSERT(cond, msg) do { \
	if (!(cond)) { \
		fprintf(stderr, "  [FAIL] %s (line %d)\n", msg, __LINE__); \
		g_failures++; \
	} else { \
		printf("  [PASS] %s\n", msg); \
	} \
} while (0)

static const u8 *find_cta_data_block(const u8 *edid, u8 target_tag, u8 *out_len)
{
	if (!edid || edid[128] != 0x02)
		return NULL;

	unsigned int dtd_off = edid[128 + 2];
	unsigned int data_end = 128 + dtd_off;
	if (dtd_off < 4 || data_end > 255)
		return NULL;

	for (unsigned int off = 132; off < data_end; ) {
		unsigned int tag = edid[off] >> 5;
		unsigned int len = edid[off] & 0x1f;
		if (off + 1 + len > data_end)
			return NULL;
		if (tag == target_tag) {
			if (out_len)
				*out_len = (u8)len;
			return edid + off + 1;
		}
		off += 1 + len;
	}
	return NULL;
}

static void test_default_edid_unification(void)
{
	u8 unified[GC573_EDID_SIZE];
	int ret;
	u8 audio_len = 0;
	const u8 *audio_sads;

	printf("Testing default GC573 EDID audio unification...\n");
	ret = edid_build_unified_profile(g_gc573_default_edid, unified);
	TEST_ASSERT(ret == 0, "edid_build_unified_profile on default EDID succeeds");
	TEST_ASSERT(edid_validate(unified) == 0, "unified default EDID checksums valid");

	/* Verify header is untouched */
	TEST_ASSERT(unified[0] == 0x00 && unified[7] == 0x00, "EDID magic header preserved");

	/* Verify CTA extension tag */
	TEST_ASSERT(unified[128] == 0x02, "Extension block tag is CTA-861 (0x02)");

	/* Verify audio data block was inserted */
	audio_sads = find_cta_data_block(unified, 1 /* Audio tag */, &audio_len);
	TEST_ASSERT(audio_sads != NULL, "Audio block present in unified default EDID");
	if (audio_sads) {
		TEST_ASSERT(audio_len == sizeof(g_gc573_edid_audio_sads),
			    "Audio block length matches unified SADs (9 bytes)");
		TEST_ASSERT(memcmp(audio_sads, g_gc573_edid_audio_sads,
				   sizeof(g_gc573_edid_audio_sads)) == 0,
			    "Unified SAD descriptors match LPCM/AC3/DTS");
	}
}

static void test_1440p120_edid_unification(void)
{
	u8 unified[GC573_EDID_SIZE];
	int ret;
	u8 audio_len = 0;

	printf("Testing 1440p120 GC573 EDID audio unification...\n");
	ret = edid_build_unified_profile(g_gc573_edid_1440p120, unified);
	TEST_ASSERT(ret == 0, "edid_build_unified_profile on 1440p120 EDID succeeds");
	TEST_ASSERT(edid_validate(unified) == 0, "unified 1440p120 EDID checksums valid");
	TEST_ASSERT(find_cta_data_block(unified, 1, &audio_len) != NULL,
		    "Audio block present in unified 1440p120 EDID");
}

static void test_1080p240_edid_unification(void)
{
	u8 unified[GC573_EDID_SIZE];
	int ret;
	u8 audio_len = 0;

	printf("Testing 1080p240 GC573 EDID audio unification...\n");
	ret = edid_build_unified_profile(g_gc573_edid_1080p240, unified);
	TEST_ASSERT(ret == 0, "edid_build_unified_profile on 1080p240 EDID succeeds");
	TEST_ASSERT(edid_validate(unified) == 0, "unified 1080p240 EDID checksums valid");
	TEST_ASSERT(find_cta_data_block(unified, 1, &audio_len) != NULL,
		    "Audio block present in unified 1080p240 EDID");
}

static void test_corrupted_edid_rejection(void)
{
	u8 bad_edid[GC573_EDID_SIZE];
	u8 unified[GC573_EDID_SIZE];

	printf("Testing invalid/corrupted EDID handling...\n");
	memcpy(bad_edid, g_gc573_default_edid, GC573_EDID_SIZE);

	/* NULL input validation */
	TEST_ASSERT(edid_build_unified_profile(NULL, unified) == -EINVAL,
		    "edid_build_unified_profile rejects NULL source buffer");
	TEST_ASSERT(edid_build_unified_profile(bad_edid, NULL) == -EINVAL,
		    "edid_build_unified_profile rejects NULL destination buffer");
	TEST_ASSERT(edid_validate(NULL) == -EINVAL,
		    "edid_validate rejects NULL pointer");

	/* Corrupt checksum */
	bad_edid[127] ^= 0xff;
	TEST_ASSERT(edid_validate(bad_edid) != 0, "edid_validate rejects corrupt block 0");

	/* Corrupt dtd offset */
	bad_edid[127] ^= 0xff; /* restore */
	bad_edid[128 + 2] = 2; /* invalid dtd offset (< 4) */
	TEST_ASSERT(edid_build_unified_profile(bad_edid, unified) != 0,
		    "edid_build_unified_profile rejects out-of-bounds dtd offset (< 4)");

	bad_edid[128 + 2] = 130; /* exceeds checksum offset (128 + 130 > 255) */
	TEST_ASSERT(edid_build_unified_profile(bad_edid, unified) != 0,
		    "edid_build_unified_profile rejects out-of-bounds dtd offset (> 127)");

	/* Non-CTA extension tag */
	bad_edid[128 + 2] = g_gc573_default_edid[128 + 2]; /* restore */
	bad_edid[128] = 0x00; /* Invalid tag (not 0x02) */
	TEST_ASSERT(edid_build_unified_profile(bad_edid, unified) != 0,
		    "edid_build_unified_profile rejects non-CTA extension block");
}

int main(void)
{
	printf("==================================================\n");
	printf(" GC573 EDID Audio-Unifier Test Suite\n");
	printf("==================================================\n");

	test_default_edid_unification();
	test_1440p120_edid_unification();
	test_1080p240_edid_unification();
	test_corrupted_edid_rejection();

	printf("==================================================\n");
	if (g_failures == 0) {
		printf(" ALL GC573 EDID UNIFIER TESTS PASSED!\n");
		return 0;
	} else {
		printf(" %d TESTS FAILED!\n", g_failures);
		return 1;
	}
}
