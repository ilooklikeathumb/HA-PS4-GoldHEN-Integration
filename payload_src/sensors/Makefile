# Live sensors payloads. Needs ps4-payload-sdk, see ../rest-mode/README.md.
#   make PS4_PAYLOAD_SDK=/path/to/ps4-payload-sdk          → ps4-sensors.elf + ps4-sensors-stop.elf
#   make PS4_PAYLOAD_SDK=/path/to/ps4-payload-sdk install  → copies both into the integration's sensors_payload/

ifdef PS4_PAYLOAD_SDK
    include $(PS4_PAYLOAD_SDK)/toolchain/orbis.mk
else
    $(error PS4_PAYLOAD_SDK is undefined)
endif

ELFS := ps4-sensors.elf ps4-sensors-stop.elf
INSTALL_DIR := ../../custom_components/ps4_goldhen/sensors_payload

CFLAGS := -Wall -Werror -O2

all: $(ELFS)

ps4-sensors.elf: main.c
	$(CC) $(CFLAGS) -o $@ $^ -lkernel_sys

ps4-sensors-stop.elf: stop.c
	$(CC) $(CFLAGS) -o $@ $^

install: $(ELFS)
	cp $(ELFS) $(INSTALL_DIR)/

clean:
	rm -f $(ELFS)

.PHONY: all install clean
