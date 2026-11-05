/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * everlive_card.c — Core Hardware Registry and Autodiscovery for EverLive
 */

#include "everlive_card.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dirent.h>
#include <unistd.h>
#include <limits.h>

#define DEFAULT_PCI_SYSFS_PATH "/sys/bus/pci/devices"

/* -------------------------------------------------------------------------
 * Static Hardware Registry of Supported Capture Cards
 * ------------------------------------------------------------------------- */
static const struct everlive_device_desc kEverliveCardRegistry[] = {
	{
		.vendor_id = 0x1461,
		.device_id = 0x0054,
		.subsystem_id = 0x5700,
		.model_slug = "gc570d",
		.name = "AVerMedia Live Gamer DUO (GC570D)",
		.capabilities = EVERLIVE_CAP_VIDEO_CAPTURE |
				EVERLIVE_CAP_AUDIO_CAPTURE |
				EVERLIVE_CAP_PASSTHROUGH |
				EVERLIVE_CAP_DUAL_INPUT |
				EVERLIVE_CAP_RGB_LED |
				EVERLIVE_CAP_HARDWARE_SPLITTER |
				EVERLIVE_CAP_EDID_PASSTHROUGH,
		.num_input_ports = 2,
		.ops = NULL, /* Bound dynamically at initialization */
	},
	{
		.vendor_id = 0x1461,
		.device_id = 0x0054,
		.subsystem_id = 0x5730,
		.model_slug = "gc573",
		.name = "AVerMedia Live Gamer 4K (GC573)",
		.capabilities = EVERLIVE_CAP_VIDEO_CAPTURE |
				EVERLIVE_CAP_AUDIO_CAPTURE |
				EVERLIVE_CAP_PASSTHROUGH |
				EVERLIVE_CAP_4K60 |
				EVERLIVE_CAP_1440P120 |
				EVERLIVE_CAP_1080P240 |
				EVERLIVE_CAP_HDR10 |
				EVERLIVE_CAP_RGB_LED |
				EVERLIVE_CAP_BITSTREAM_AUDIO |
				EVERLIVE_CAP_FRAMEGEN |
				EVERLIVE_CAP_EDID_PASSTHROUGH,
		.num_input_ports = 1,
		.ops = NULL,
	},
	{
		.vendor_id = 0x1461,
		.device_id = 0x0055,
		.subsystem_id = 0, /* GC573 BOLT / Alternative PCIe ID */
		.model_slug = "gc573",
		.name = "AVerMedia Live Gamer 4K / BOLT (GC573)",
		.capabilities = EVERLIVE_CAP_VIDEO_CAPTURE |
				EVERLIVE_CAP_AUDIO_CAPTURE |
				EVERLIVE_CAP_PASSTHROUGH |
				EVERLIVE_CAP_4K60 |
				EVERLIVE_CAP_1440P120 |
				EVERLIVE_CAP_1080P240 |
				EVERLIVE_CAP_HDR10 |
				EVERLIVE_CAP_RGB_LED |
				EVERLIVE_CAP_BITSTREAM_AUDIO |
				EVERLIVE_CAP_FRAMEGEN |
				EVERLIVE_CAP_EDID_PASSTHROUGH,
		.num_input_ports = 1,
		.ops = NULL,
	},
	{
		.vendor_id = 0x1461,
		.device_id = 0x0057,
		.subsystem_id = 0, /* Future expansion: Live Gamer 4K 2.1 */
		.model_slug = "gc575",
		.name = "AVerMedia Live Gamer 4K 2.1 (GC575)",
		.capabilities = EVERLIVE_CAP_VIDEO_CAPTURE |
				EVERLIVE_CAP_AUDIO_CAPTURE |
				EVERLIVE_CAP_PASSTHROUGH |
				EVERLIVE_CAP_4K60 |
				EVERLIVE_CAP_1440P120 |
				EVERLIVE_CAP_HDR10 |
				EVERLIVE_CAP_RGB_LED |
				EVERLIVE_CAP_BITSTREAM_AUDIO |
				EVERLIVE_CAP_EDID_PASSTHROUGH,
		.num_input_ports = 1,
		.ops = NULL,
	},
};

#define REGISTRY_COUNT (sizeof(kEverliveCardRegistry) / sizeof(kEverliveCardRegistry[0]))

const struct everlive_device_desc *everlive_get_card_registry(size_t *out_count)
{
	if (out_count)
		*out_count = REGISTRY_COUNT;
	return kEverliveCardRegistry;
}

const struct everlive_device_desc *everlive_match_card(uint16_t vendor,
						       uint16_t device,
						       uint16_t subsystem)
{
	/* Pass 1: Look for exact match (vendor, device, subsystem) */
	for (size_t i = 0; i < REGISTRY_COUNT; i++) {
		const struct everlive_device_desc *d = &kEverliveCardRegistry[i];
		if (d->vendor_id == vendor && d->device_id == device) {
			if (d->subsystem_id != 0 && d->subsystem_id == subsystem)
				return d;
		}
	}

	/* Special case: GC570D is distinguished by subsystem 0x5700.
	 * If device is 0x0054 and subsystem is NOT 0x5700, it is a GC573.
	 */
	if (vendor == 0x1461 && device == 0x0054 && subsystem != 0x5700) {
		for (size_t i = 0; i < REGISTRY_COUNT; i++) {
			if (strcmp(kEverliveCardRegistry[i].model_slug, "gc573") == 0)
				return &kEverliveCardRegistry[i];
		}
	}

	/* Pass 2: Look for wildcard subsystem match */
	for (size_t i = 0; i < REGISTRY_COUNT; i++) {
		const struct everlive_device_desc *d = &kEverliveCardRegistry[i];
		if (d->vendor_id == vendor && d->device_id == device) {
			if (d->subsystem_id == 0)
				return d;
		}
	}

	return NULL;
}

static uint16_t read_sysfs_hex16(const char *path)
{
	FILE *f = fopen(path, "r");
	if (!f)
		return 0;

	char buf[32];
	uint16_t val = 0;
	if (fgets(buf, sizeof(buf), f)) {
		val = (uint16_t)strtoul(buf, NULL, 16);
	}
	fclose(f);
	return val;
}

static int read_iommu_group(const char *dev_dir)
{
	char path[PATH_MAX];
	char target[PATH_MAX];

	int n = snprintf(path, sizeof(path), "%s/iommu_group", dev_dir);
	if (n < 0 || (size_t)n >= sizeof(path))
		return -1;

	ssize_t len = readlink(path, target, sizeof(target) - 1);
	if (len <= 0)
		return -1;

	target[len] = '\0';
	char *slash = strrchr(target, '/');
	if (!slash)
		return -1;

	return (int)strtol(slash + 1, NULL, 10);
}

bool everlive_is_pci_vfio_bound(const char *pci_bdf, const char *sysfs_pci_root)
{
	if (!pci_bdf || !*pci_bdf)
		return false;

	const char *root = (sysfs_pci_root && *sysfs_pci_root) ?
			    sysfs_pci_root : DEFAULT_PCI_SYSFS_PATH;

	char driver_link[PATH_MAX];
	char target[PATH_MAX];

	snprintf(driver_link, sizeof(driver_link), "%s/%s/driver", root, pci_bdf);
	ssize_t len = readlink(driver_link, target, sizeof(target) - 1);
	if (len <= 0)
		return false;

	target[len] = '\0';
	char *slash = strrchr(target, '/');
	const char *driver_name = slash ? (slash + 1) : target;

	return (strcmp(driver_name, "vfio-pci") == 0);
}

int everlive_detect_installed_cards(struct everlive_device *out_devices,
				    size_t max_devices,
				    const char *sysfs_pci_root)
{
	if (!out_devices || max_devices == 0)
		return 0;

	const char *root = (sysfs_pci_root && *sysfs_pci_root) ?
			    sysfs_pci_root : DEFAULT_PCI_SYSFS_PATH;

	DIR *dir = opendir(root);
	if (!dir)
		return 0;

	size_t found_count = 0;
	struct dirent *ent;

	while ((ent = readdir(dir)) != NULL) {
		if (ent->d_name[0] == '.')
			continue;

		char dev_path[512];
		snprintf(dev_path, sizeof(dev_path), "%s/%s", root, ent->d_name);

		char vendor_path[PATH_MAX];
		char device_path[PATH_MAX];
		char subsystem_path[PATH_MAX];

		snprintf(vendor_path, sizeof(vendor_path), "%s/vendor", dev_path);
		snprintf(device_path, sizeof(device_path), "%s/device", dev_path);
		snprintf(subsystem_path, sizeof(subsystem_path), "%s/subsystem_device", dev_path);

		uint16_t vendor = read_sysfs_hex16(vendor_path);
		uint16_t device = read_sysfs_hex16(device_path);
		uint16_t subsystem = read_sysfs_hex16(subsystem_path);

		const struct everlive_device_desc *desc =
			everlive_match_card(vendor, device, subsystem);

		if (desc) {
			struct everlive_device *dev = &out_devices[found_count];
			memset(dev, 0, sizeof(*dev));
			dev->desc = desc;
			snprintf(dev->pci_bdf, sizeof(dev->pci_bdf), "%.31s", ent->d_name);
			dev->iommu_group = read_iommu_group(dev_path);
			dev->is_vfio_bound = everlive_is_pci_vfio_bound(ent->d_name, root);

			found_count++;
			if (found_count >= max_devices)
				break;
		}
	}

	closedir(dir);
	return (int)found_count;
}
