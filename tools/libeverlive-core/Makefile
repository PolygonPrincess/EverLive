# SPDX-License-Identifier: GPL-2.0-only
CC ?= gcc
AR ?= ar
CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-parameter -std=gnu11 -D_GNU_SOURCE

TARGET_LIB := libeverlive_core.a
TARGET_CLI := everlive-ctl

OBJS := everlive_card.o

all: $(TARGET_LIB) $(TARGET_CLI)

$(TARGET_LIB): $(OBJS)
	$(AR) rcs $@ $^

$(TARGET_CLI): everlive_ctl.o $(TARGET_LIB)
	$(CC) $(CFLAGS) -o $@ everlive_ctl.o -L. -leverlive_core

%.o: %.c everlive_card.h
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f $(OBJS) everlive_ctl.o $(TARGET_LIB) $(TARGET_CLI)

.PHONY: all clean
