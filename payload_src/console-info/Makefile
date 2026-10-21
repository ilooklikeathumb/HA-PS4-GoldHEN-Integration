# Console-info payload. Needs ps4-payload-sdk, see ../rest-mode/README.md.
#   make PS4_PAYLOAD_SDK=/path/to/ps4-payload-sdk          → console-info.elf
#   make PS4_PAYLOAD_SDK=/path/to/ps4-payload-sdk install  → copies it into the integration's info_payload/

ifdef PS4_PAYLOAD_SDK
    include $(PS4_PAYLOAD_SDK)/toolchain/orbis.mk
else
    $(error PS4_PAYLOAD_SDK is undefined)
endif

ELF := console-info.elf
INSTALL_DIR := ../../custom_components/ps4_goldhen/info_payload

CFLAGS := -Wall -Werror -O2

all: $(ELF)

$(ELF): main.c
	$(CC) $(CFLAGS) -o $@ $^ -lkernel_sys

install: $(ELF)
	cp $(ELF) $(INSTALL_DIR)/$(ELF)

clean:
	rm -f $(ELF)

.PHONY: all install clean
