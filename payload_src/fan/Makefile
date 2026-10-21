# Fan threshold payload. Needs ps4-payload-sdk, see ../rest-mode/README.md.
#   make PS4_PAYLOAD_SDK=/path/to/ps4-payload-sdk          → ps4-fan.elf
#   make PS4_PAYLOAD_SDK=/path/to/ps4-payload-sdk install  → copies it into the integration's fan_payload/

ifdef PS4_PAYLOAD_SDK
    include $(PS4_PAYLOAD_SDK)/toolchain/orbis.mk
else
    $(error PS4_PAYLOAD_SDK is undefined)
endif

ELF := ps4-fan.elf
INSTALL_DIR := ../../custom_components/ps4_goldhen/fan_payload

CFLAGS := -Wall -Werror -O2

all: $(ELF)

$(ELF): main.c
	$(CC) $(CFLAGS) -o $@ $^

install: $(ELF)
	mkdir -p $(INSTALL_DIR)
	cp $(ELF) $(INSTALL_DIR)/

clean:
	rm -f $(ELF)

.PHONY: all install clean
