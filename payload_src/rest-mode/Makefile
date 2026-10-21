# Rest-mode payload. Needs ps4-payload-sdk, see README.md.
#   make PS4_PAYLOAD_SDK=/path/to/ps4-payload-sdk          → rest-mode.elf
#   make PS4_PAYLOAD_SDK=/path/to/ps4-payload-sdk install  → copies it into the integration's rest_payload/

ifdef PS4_PAYLOAD_SDK
    include $(PS4_PAYLOAD_SDK)/toolchain/orbis.mk
else
    $(error PS4_PAYLOAD_SDK is undefined)
endif

ELF := rest-mode.elf
INSTALL_DIR := ../../custom_components/ps4_goldhen/rest_payload

CFLAGS := -Wall -Werror -O2

all: $(ELF)

$(ELF): main.c
	$(CC) $(CFLAGS) -o $@ $^ -lSceSystemService

install: $(ELF)
	cp $(ELF) $(INSTALL_DIR)/$(ELF)

clean:
	rm -f $(ELF)

.PHONY: all install clean
