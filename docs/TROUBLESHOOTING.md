# Troubleshooting

## Pairing fails

| HA says | Do this |
|---|---|
| No answer from the PS4 | The PS4 must be on (not in rest) and reachable from HA. Check the IP and the VLAN rules in [Requirements](../README.md#requirements). |
| The PS4 is in rest mode | Turn it on fully, open Add Device, try again. |
| The PS4 refused the number | The number changes every time Add Device opens. Use the one on screen now. |
| The PS4 doesn't know this account ID | The ID must belong to a user on this PS4: signed in to PSN, or activated in Apollo. |
| Remote Play is busy | Close other Remote Play sessions (PS Remote Play app, chiaki-ng) and try again. |
| Pairing failed | The HA log has the PS4's reason code. Please include it in an issue. |

## Turn on does nothing

If HA says the PS4 isn't on the network, check the rest mode settings from [Requirements](../README.md#requirements):
with *Stay Connected to the Internet* off, a resting PS4 drops off the network and looks fully off.

**Fully off, not rest?** Network wake only works from rest mode. *Wake entity* (**Configure → Connection and wake
settings**) can point at a button or script that wakes the PS4 another way, for example an ESP32 Bluetooth wake. After
a full power-off you still have to load GoldHEN again before its features come back.

[Back to the main README](../README.md)
