/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * everlive_ctl.c — Unified Control & Diagnostic CLI for EverLive Capture Cards
 */

#include "everlive_card.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <limits.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/un.h>

static void print_capabilities(uint32_t caps)
{
	printf("    Capabilities: ");
	bool first = true;
#define CHECK_CAP(cap, name) \
	if (caps & (cap)) { \
		printf("%s%s", first ? "" : ", ", (name)); \
		first = false; \
	}
	CHECK_CAP(EVERLIVE_CAP_4K60, "4K60");
	CHECK_CAP(EVERLIVE_CAP_4K120, "4K120");
	CHECK_CAP(EVERLIVE_CAP_1440P120, "1440p120");
	CHECK_CAP(EVERLIVE_CAP_1080P240, "1080p240");
	CHECK_CAP(EVERLIVE_CAP_1080P60, "1080p60");
	CHECK_CAP(EVERLIVE_CAP_HDR10, "HDR10");
	CHECK_CAP(EVERLIVE_CAP_DUAL_INPUT, "Dual-Input");
	CHECK_CAP(EVERLIVE_CAP_RGB_LED, "RGB-LED");
	CHECK_CAP(EVERLIVE_CAP_BITSTREAM_AUDIO, "Bitstream-Audio (AC3/DTS)");
	CHECK_CAP(EVERLIVE_CAP_FRAMEGEN, "Lossless Scaling (lsfg-vk)");
	CHECK_CAP(EVERLIVE_CAP_HARDWARE_SPLITTER, "Hardware-Splitter");
#undef CHECK_CAP
	if (first)
		printf("None");
	printf("\n");
}

static int cmd_detect(void)
{
	struct everlive_device cards[8];
	int count = everlive_detect_installed_cards(cards, 8, NULL);

	printf("=== EverLive Capture Card Discovery ===\n");
	if (count <= 0) {
		printf("No supported EverLive capture cards detected on PCI bus.\n");
		return 1;
	}

	printf("Found %d supported capture card%s:\n\n", count, count > 1 ? "s" : "");
	for (int i = 0; i < count; i++) {
		const struct everlive_device *dev = &cards[i];
		printf("[%d] %s\n", i + 1, dev->desc->name);
		printf("    Model Slug:   %s\n", dev->desc->model_slug);
		printf("    PCI BDF:      %s\n", dev->pci_bdf);
		printf("    IOMMU Group:  %d\n", dev->iommu_group);
		printf("    Driver Bound: %s\n", dev->is_vfio_bound ? "vfio-pci (Ready)" : "Kernel driver (Needs bind)");
		printf("    Input Ports:  %d\n", dev->desc->num_input_ports);
		print_capabilities(dev->desc->capabilities);
		printf("\n");
	}
	return 0;
}

static void print_usage(const char *prog)
{
	printf("Usage: %s <command> [arguments]\n\n"
	       "Hardware & Diagnostics:\n"
	       "  detect                  List all installed capture cards and capabilities\n"
	       "  list-models             Display all supported card models in EverLive registry\n"
	       "  status                  Display capture daemon status and active hardware\n\n"
	       "Service & Bringup:\n"
	       "  init                    Initialize capture card in background (passthrough/audio)\n"
	       "  service status          Show systemd user service status\n"
	       "  service start           Start the background capture service\n"
	       "  service stop            Stop the background capture service\n"
	       "  service enable          Enable background capture service at user login\n"
	       "  service disable         Disable background capture autostart\n"
	       "  help                    Show this help message\n\n", prog);
}

static int cmd_status(void)
{
	printf("=== EverLive Background Engine Status ===\n");

	/* Check running processes */
	bool gc570d_proc = (system("pgrep -x opengc570d-userd >/dev/null 2>&1") == 0);
	bool gc573_proc = (system("pgrep -x gc573_user_streamer >/dev/null 2>&1") == 0 ||
			   system("pgrep -x everlive_userd >/dev/null 2>&1") == 0);

	if (gc570d_proc) {
		printf("Active Process:   opengc570d-userd (running)\n");
	} else if (gc573_proc) {
		printf("Active Process:   gc573_user_streamer / everlive_userd (running)\n");
	} else {
		printf("Active Process:   None (Engine is idle/standby)\n");
	}

	/* Check systemd user service */
	printf("Service State:    ");
	fflush(stdout);
	int ret = system("systemctl --user is-active everlive.service 2>/dev/null");
	if (ret != 0) {
		system("systemctl --user is-active opengc570d-userd.service 2>/dev/null || echo inactive");
	}

	/* Also report detected card */
	printf("\n");
	cmd_detect();
	return 0;
}

static int cmd_init(void)
{
	printf("[*] Initializing EverLive capture hardware in background...\n");
	/* Check if already running */
	if (system("pgrep -x opengc570d-userd >/dev/null 2>&1") == 0 ||
	    system("pgrep -x gc573_user_streamer >/dev/null 2>&1") == 0) {
		printf("[+] Capture engine is already running and hardware is initialized.\n");
		return 0;
	}

	/* Try starting via systemd if service file exists */
	if (system("systemctl --user is-enabled everlive.service >/dev/null 2>&1") == 0 ||
	    system("systemctl --user is-enabled opengc570d-userd.service >/dev/null 2>&1") == 0) {
		printf("[*] Starting via systemd user service...\n");
		system("systemctl --user start everlive.service 2>/dev/null || systemctl --user start opengc570d-userd.service");
		return 0;
	}

	/* Direct script invocation */
	char script_path[PATH_MAX];
	snprintf(script_path, sizeof(script_path), "%s/everlive-service.sh", "/usr/local/bin");
	if (access(script_path, X_OK) != 0) {
		/* Search relative to executable or current directory */
		snprintf(script_path, sizeof(script_path), "./tools/libeverlive-core/everlive-service.sh");
	}

	char cmd[PATH_MAX + 32];
	snprintf(cmd, sizeof(cmd), "nohup %s >/dev/null 2>&1 &", script_path);
	printf("[*] Spawning %s...\n", script_path);
	int res = system(cmd);
	if (res == 0) {
		printf("[+] EverLive background service launched successfully.\n");
	}
	return res;
}

static int cmd_service(int argc, char **argv)
{
	if (argc < 3) {
		return system("systemctl --user status everlive.service opengc570d-userd.service 2>/dev/null || true");
	}

	const char *action = argv[2];
	char cmd[256];

	if (strcmp(action, "status") == 0) {
		snprintf(cmd, sizeof(cmd), "systemctl --user status everlive.service opengc570d-userd.service");
	} else if (strcmp(action, "start") == 0) {
		snprintf(cmd, sizeof(cmd), "systemctl --user start everlive.service 2>/dev/null || systemctl --user start opengc570d-userd.service");
	} else if (strcmp(action, "stop") == 0) {
		snprintf(cmd, sizeof(cmd), "systemctl --user stop everlive.service opengc570d-userd.service 2>/dev/null");
	} else if (strcmp(action, "enable") == 0) {
		snprintf(cmd, sizeof(cmd), "systemctl --user enable --now everlive.service 2>/dev/null || systemctl --user enable --now opengc570d-userd.service");
	} else if (strcmp(action, "disable") == 0) {
		snprintf(cmd, sizeof(cmd), "systemctl --user disable --now everlive.service opengc570d-userd.service 2>/dev/null");
	} else {
		fprintf(stderr, "Unknown service action: %s\n", action);
		return 1;
	}

	return system(cmd);
}

static int cmd_list_models(void)
{
	size_t count = 0;
	const struct everlive_device_desc *reg = everlive_get_card_registry(&count);

	printf("=== EverLive Supported Card Registry ===\n");
	for (size_t i = 0; i < count; i++) {
		printf("- %s (%s)\n", reg[i].name, reg[i].model_slug);
		printf("  PCI ID: 0x%04x:0x%04x (Subsystem: 0x%04x)\n",
		       reg[i].vendor_id, reg[i].device_id, reg[i].subsystem_id);
		print_capabilities(reg[i].capabilities);
	}
	return 0;
}

int main(int argc, char **argv)
{
	if (argc < 2) {
		return cmd_detect();
	}

	const char *cmd = argv[1];
	if (strcmp(cmd, "detect") == 0 || strcmp(cmd, "-d") == 0) {
		return cmd_detect();
	} else if (strcmp(cmd, "status") == 0 || strcmp(cmd, "-s") == 0) {
		return cmd_status();
	} else if (strcmp(cmd, "init") == 0 || strcmp(cmd, "-i") == 0) {
		return cmd_init();
	} else if (strcmp(cmd, "service") == 0) {
		return cmd_service(argc, argv);
	} else if (strcmp(cmd, "list-models") == 0) {
		return cmd_list_models();
	} else if (strcmp(cmd, "help") == 0 || strcmp(cmd, "-h") == 0 || strcmp(cmd, "--help") == 0) {
		print_usage(argv[0]);
		return 0;
	} else {
		fprintf(stderr, "Unknown command: %s\n\n", cmd);
		print_usage(argv[0]);
		return 1;
	}
}
