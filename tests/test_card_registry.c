/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * test_card_registry.c — Unit tests for libeverlive-core hardware registry and autodiscovery
 */

#include "everlive_card.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include <sys/stat.h>
#include <unistd.h>
#include <limits.h>

#define TEST_PASS(desc) printf("  [PASS] %s\n", (desc))

static void test_registry_enumeration(void)
{
	printf("Testing everlive_get_card_registry...\n");
	size_t count = 0;
	const struct everlive_device_desc *registry = everlive_get_card_registry(&count);

	assert(registry != NULL);
	assert(count >= 3);
	TEST_PASS("Registry contains multiple supported card definitions");

	bool found_gc570d = false;
	bool found_gc573 = false;

	for (size_t i = 0; i < count; i++) {
		if (strcmp(registry[i].model_slug, "gc570d") == 0)
			found_gc570d = true;
		if (strcmp(registry[i].model_slug, "gc573") == 0)
			found_gc573 = true;
	}

	assert(found_gc570d);
	assert(found_gc573);
	TEST_PASS("Registry includes both GC570D and GC573 descriptors");
}

static void test_card_matching(void)
{
	printf("Testing everlive_match_card...\n");

	/* Exact GC570D match (0x1461, 0x0054, 0x5700) */
	const struct everlive_device_desc *gc570d =
		everlive_match_card(0x1461, 0x0054, 0x5700);
	assert(gc570d != NULL);
	assert(strcmp(gc570d->model_slug, "gc570d") == 0);
	assert(gc570d->num_input_ports == 2);
	assert(gc570d->capabilities & EVERLIVE_CAP_DUAL_INPUT);
	assert(gc570d->capabilities & EVERLIVE_CAP_HARDWARE_SPLITTER);
	TEST_PASS("Identified GC570D by subsystem 0x5700 with dual inputs");

	/* Exact GC573 match (0x1461, 0x0054, 0x5730) */
	const struct everlive_device_desc *gc573_exact =
		everlive_match_card(0x1461, 0x0054, 0x5730);
	assert(gc573_exact != NULL);
	assert(strcmp(gc573_exact->model_slug, "gc573") == 0);
	assert(gc573_exact->capabilities & EVERLIVE_CAP_4K60);
	assert(gc573_exact->capabilities & EVERLIVE_CAP_1440P120);
	assert(gc573_exact->capabilities & EVERLIVE_CAP_FRAMEGEN);
	TEST_PASS("Identified GC573 by subsystem 0x5730 with 4K60/1440p120/FrameGen");

	/* Wildcard GC573 match (0x1461, 0x0054 with non-5700 subsystem) */
	const struct everlive_device_desc *gc573_wildcard =
		everlive_match_card(0x1461, 0x0054, 0x0000);
	assert(gc573_wildcard != NULL);
	assert(strcmp(gc573_wildcard->model_slug, "gc573") == 0);
	TEST_PASS("Defaulted 0x0054 to GC573 when subsystem != 0x5700");

	/* Secondary GC573 / BOLT PCIe ID (0x0055) */
	const struct everlive_device_desc *gc573_alt =
		everlive_match_card(0x1461, 0x0055, 0x0000);
	assert(gc573_alt != NULL);
	assert(strcmp(gc573_alt->model_slug, "gc573") == 0);
	TEST_PASS("Identified alternative GC573/BOLT PCIe device ID 0x0055");

	/* Future GC575 template (0x0057) */
	const struct everlive_device_desc *gc575 =
		everlive_match_card(0x1461, 0x0057, 0x0000);
	assert(gc575 != NULL);
	assert(strcmp(gc575->model_slug, "gc575") == 0);
	TEST_PASS("Identified GC575 device ID 0x0057");

	/* Unknown card rejected */
	const struct everlive_device_desc *unknown =
		everlive_match_card(0x10de, 0x1f02, 0x0000);
	assert(unknown == NULL);
	TEST_PASS("Unknown PCI IDs rejected gracefully");
}

static void create_file(const char *path, const char *content)
{
	FILE *f = fopen(path, "w");
	assert(f != NULL);
	fputs(content, f);
	fclose(f);
}

static void test_mock_sysfs_scan(void)
{
	printf("Testing everlive_detect_installed_cards with mock sysfs...\n");

	char tmp_root[] = "/tmp/everlive_sysfs_test_XXXXXX";
	char *res = mkdtemp(tmp_root);
	assert(res != NULL);

	/* Mock device 1: GC570D at 0000:03:00.0 */
	char d1[512], d2[512], d3[512];
	snprintf(d1, sizeof(d1), "%s/0000:03:00.0", tmp_root);
	snprintf(d2, sizeof(d2), "%s/0000:04:00.0", tmp_root);
	snprintf(d3, sizeof(d3), "%s/0000:01:00.0", tmp_root);

	assert(mkdir(d1, 0755) == 0);
	assert(mkdir(d2, 0755) == 0);
	assert(mkdir(d3, 0755) == 0);

	/* Populate GC570D */
	char p[PATH_MAX];
	snprintf(p, sizeof(p), "%s/vendor", d1);
	create_file(p, "0x1461\n");
	snprintf(p, sizeof(p), "%s/device", d1);
	create_file(p, "0x0054\n");
	snprintf(p, sizeof(p), "%s/subsystem_device", d1);
	create_file(p, "0x5700\n");

	/* Mock symlink for driver -> vfio-pci */
	snprintf(p, sizeof(p), "%s/driver", d1);
	assert(symlink("/sys/bus/pci/drivers/vfio-pci", p) == 0);

	/* Mock symlink for iommu_group -> .../16 */
	snprintf(p, sizeof(p), "%s/iommu_group", d1);
	assert(symlink("/sys/kernel/iommu_groups/16", p) == 0);

	/* Populate GC573 at 0000:04:00.0 */
	snprintf(p, sizeof(p), "%s/vendor", d2);
	create_file(p, "0x1461\n");
	snprintf(p, sizeof(p), "%s/device", d2);
	create_file(p, "0x0054\n");
	snprintf(p, sizeof(p), "%s/subsystem_device", d2);
	create_file(p, "0x5730\n");

	/* Mock symlink for iommu_group -> .../22 */
	snprintf(p, sizeof(p), "%s/iommu_group", d2);
	assert(symlink("/sys/kernel/iommu_groups/22", p) == 0);

	/* Populate other non-capture device at 0000:01:00.0 (e.g. Intel GPU) */
	snprintf(p, sizeof(p), "%s/vendor", d3);
	create_file(p, "0x8086\n");
	snprintf(p, sizeof(p), "%s/device", d3);
	create_file(p, "0x9bc5\n");
	snprintf(p, sizeof(p), "%s/subsystem_device", d3);
	create_file(p, "0x103c\n");

	/* Scan using mock root */
	struct everlive_device found[8];
	int count = everlive_detect_installed_cards(found, 8, tmp_root);

	assert(count == 2);
	TEST_PASS("Discovered exactly 2 supported capture cards from mock sysfs");

	bool saw_gc570d = false;
	bool saw_gc573 = false;

	for (int i = 0; i < count; i++) {
		if (strcmp(found[i].desc->model_slug, "gc570d") == 0) {
			saw_gc570d = true;
			assert(strcmp(found[i].pci_bdf, "0000:03:00.0") == 0);
			assert(found[i].iommu_group == 16);
			assert(found[i].is_vfio_bound == true);
		}
		if (strcmp(found[i].desc->model_slug, "gc573") == 0) {
			saw_gc573 = true;
			assert(strcmp(found[i].pci_bdf, "0000:04:00.0") == 0);
			assert(found[i].iommu_group == 22);
			assert(found[i].is_vfio_bound == false);
		}
	}

	assert(saw_gc570d);
	assert(saw_gc573);
	TEST_PASS("Correctly populated BDF, IOMMU group, and VFIO binding state");

	/* Cleanup mock directory */
	unlink(d1); // in case
	char cmd[PATH_MAX + 16];
	snprintf(cmd, sizeof(cmd), "rm -rf '%s'", tmp_root);
	int ret = system(cmd);
	(void)ret;
}

int main(void)
{
	printf("==================================================\n");
	printf(" libeverlive-core Card Registry & Discovery Tests \n");
	printf("==================================================\n");

	test_registry_enumeration();
	test_card_matching();
	test_mock_sysfs_scan();

	printf("==================================================\n");
	printf(" ALL CARD REGISTRY TESTS PASSED!\n");
	printf("==================================================\n");
	return 0;
}
