# DVMBridge live stream for Trunk Recorder Pro

`dvmtrstream-pro` streams live call audio from Trunk Recorder Pro to DVMBridge. It uses the same raw PCM UDP format as the original dvmtrstream plugin and runs as a TR Pro subprocess. It uses TR Pro's 8 kHz audio for both analog and digital calls. [TR Pro plugin protocol](https://github.com/TrunkRecorder/trunk-recorder-pro/blob/main/docs/plugins/writing-plugins.md) · [Original dvmtrstream](https://github.com/DVMProject/dvmtrstream)

## Install on TR Pro

Use Trunk Recorder Pro 0.3.8 and Python 3.10 or newer on Linux or macOS. The plugin uses only the Python standard library. The account running TR Pro needs `python3` on its PATH. For Docker, copy the executable into the container or mount it and make Python available inside the container.

If the GitHub release is publicly accessible, install it through **Plugins → Install from GitHub…** or with `trunk-pro plugin install https://github.com/n71fs/dvmtrstream-pro --tag v0.1.0`. Then open **Setup → Plugins**, add your streams, and turn the plugin on.

To install by hand, extract the release archive or use this checkout. Copy the executable somewhere permanent and confirm that TR Pro can read its manifest.

```sh
mkdir -p "$HOME/.local/bin"
install -m 755 ./dvmtrstream-pro "$HOME/.local/bin/dvmtrstream-pro"
trunk-pro plugin describe "$HOME/.local/bin/dvmtrstream-pro"
```

Merge the plugin entry from the example into the top-level `plugins` object of your existing TR Pro config. Replace the example paths, system names, talkgroups, and ports. Once TR Pro sees the plugin, you can edit its streams and timing in **Setup → Plugins**. Start recording and check **Plugins → Health** for UDP traffic and errors. [Example TR Pro config](example-config.json)

You can also install the executable as `plugins/dvmtrstream-pro/dvmtrstream-pro` next to `config.json` and omit the `path` setting. For a live feed, leave TR Pro's `pluginDelays` unset for this plugin. Decrypted calls use TR Pro's `shareDecrypted` setting, which defaults to false. [TR Pro plugin settings](https://github.com/TrunkRecorder/trunk-recorder-pro/blob/main/docs/configuration/plugins.md)

## Configure DVMBridge

Set these keys in the bridge's existing YAML. On a separate host, set `udpReceiveAddress` to the bridge's LAN address and use that address in the TR Pro stream. Use loopback, a trusted LAN, or a VPN for this unauthenticated UDP feed.

```yaml
network:
  destinationId: 3101
  overrideSourceIdFromUDP: true
system:
  localAudio: false
  udpAudio: true
  udpMetadata: true
  udpReceiveAddress: "127.0.0.1"
  udpReceivePort: 32001
  udpRTPFrames: false
  udpUseULaw: false
  udpUsrp: false
  udpFrameTiming: false
```

DVMBridge uses its own `network.destinationId` for outgoing radio traffic. For different outgoing talkgroups, configure one bridge instance and receive port per outgoing talkgroup and send each route to the corresponding port. The plugin's optional `destinationId` controls the talkgroup field in the UDP packet for consumers that read it. DVMBridge can use the packet's source field with both `udpMetadata` and `overrideSourceIdFromUDP` enabled. When the packet carries an unknown source ID (zero), the bridge can use its configured `network.sourceId`. [Bridge example configuration](https://github.com/DVMProject/dvmhost/blob/master/configs/bridge-config.example.yml) · [Bridge receiver source](https://github.com/DVMProject/dvmhost/blob/435ff45b367887ffa94c89cb1a2779628e0f7bfb/src/bridge/HostBridge.cpp#L1479)

## Routes and timing

| Route key | Meaning |
| --- | --- |
| `shortName` | TR Pro system short name. Omit or leave empty to match any system. |
| `TGID` | Match the recorded talkgroup or a talkgroup it is patched with. Set to `0` to match any talkgroup. |
| `address`, `port` | DVMBridge host or IP and its UDP receive port. |
| `sourceId` | Optional fixed 24-bit radio ID. Otherwise use the latest source reported by TR Pro, or zero if unknown. |
| `destinationId` | Optional fixed 24-bit ID in the UDP packet. Otherwise use the call's recorded talkgroup. |

Multiple destinations can receive the same call. At a given IP and port, the first matching route supplies the IDs and only one copy is sent. Put specific routes before wildcard routes. Shared endpoints serialize calls by TR Pro call ID. When TR Pro uses a different live audio ID for another site of the same system and talkgroup, the plugin uses shared call metadata when there is exactly one match.

| Setting | Default | Meaning |
| --- | ---: | --- |
| `silenceLeader` | 0 ms | Silence before each active call, rounded up to 20 ms. |
| `interCallDelay` | 400 ms | Minimum pause between calls on the same endpoint. Keep it above the bridge's call timeout when calls must create separate transmissions. |
| `callGapHoldMs` | 250 ms | Release a call after this much inactivity once its queued audio has drained. A call-end event skips the inactivity wait. |
| `maxQueueMs` | 1,500 ms | Caps queued PCM per endpoint and discards frames older than this limit. Must exceed the rounded silence leader. |

Paste legacy `streams` entries and timing settings under `plugins.dvmtrstream-pro.settings`. Serialization is always enabled, frames are paced at 20 ms, and `callGapHoldMs` is the inactivity fallback. The legacy `strictCallSerialization`, `sendTickMs`, and `callActiveStaleMs` settings can be removed.

Each datagram carries 4 bytes of length (320), 320 bytes of signed 16-bit little-endian mono PCM, 4 bytes of destination ID, and 4 bytes of source ID. The length and both IDs are big-endian. IDs are masked to their lower 24 bits. The plugin joins partial PCM across events and zero-pads the final frame or the remainder before an ID change. Overlapping or delayed calls can lose frames if a destination's queue is full or the frames expire. Stopping the plugin discards pending live audio. A healthy UDP status confirms local sends only. The Health tab reports buffered and discarded frames, send errors, and invalid PCM chunks.

## Verify locally

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v -s tests
```

The tests exercise packet bytes over localhost, shared-destination routing, source updates, frame assembly, queue expiry, and call handoff. For a recorder smoke test without an SDR, point a real-time TR Pro file source at an IQ file made with `trunk-pro tool dmrsynth`. [TR Pro synthetic DMR command](https://github.com/TrunkRecorder/trunk-recorder-pro/blob/main/docs/command-line.md#tool-dmrsynth)

The Linux smoke test used TR Pro 0.3.8 and DVMBridge 05.04A on loopback. DVMBridge opened and closed the synthetic call with the radio ID from TR Pro and its own configured destination talkgroup.
