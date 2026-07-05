# Oppo UDP-20X Home Assistant Integration

A custom Home Assistant integration for controlling Oppo UDP-203 and UDP-205 4K UHD Blu-Ray players via their TCP/IP control protocol. Magnetar players are also supported (see [Magnetar players](#magnetar-players)).

## Features

- **Power control**: Turn on/off
- **Playback control**: Play, pause, stop, next/previous track
- **Volume control**: Volume up/down, set level, mute
- **Input source selection**: Switch between model-specific inputs (see [Supported Input Sources](#supported-input-sources))
- **Repeat and shuffle**: Set repeat mode (off/one/all) and toggle shuffle
- **Media info**: Track name, album, artist, playback position/duration
- **Extended state attributes**: Disc type, audio type, subtitle type, aspect ratio, 3D status, HDR status, video resolution
- **Real-time updates**: Uses verbose mode 3 for detailed streaming status including playback progress
- **Custom services**: Dimmer, Pure Audio toggle, on-screen info toggle, audio language cycle, subtitle cycle, zoom cycle (see [Services](#services))
- **Automatic reconnection**: Reconnects automatically if the connection is lost

## Installation

### HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=henrikwidlund&repository=hass-oppo&category=Integration)

1. Install [HACS](https://hacs.xyz/) if you don't already have it.
2. Home Assistant → HACS → Integrations.
3. Search “Oppo UDP-20X” → Install.
4. Restart Home Assistant.

### Manual Installation
1. Clone or download this repository.
2. Copy `custom_components/oppo_udp` into your HA `custom_components/` directory.
3. Restart HA.

Manual installs won’t auto-notify updates-watch the repo if you go this route.

## Configuration

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for "Oppo UDP-20X"
3. Enter the IP address of your player
4. Enter the TCP port (default is 23)
5. Enter a name for the entity (default is "Oppo UDP-203")
6. Select your model (UDP-203, UDP-205 or Magnetar)
7. For Magnetar players, enter the player's MAC address (required, used to wake it on power on). The port is fixed at 8102.

### Magnetar players

Magnetar players speak a fire-and-forget network-control protocol on TCP port 8102: every command is acknowledged with `ack` and the player reports **no** power, playback or volume state. As a result:

- State shown in Home Assistant is **optimistic** — derived from the commands the integration sends, not read back from the player.
- Supported features: power on/off, play, pause, stop, next/previous, volume up/down, mute, plus the custom services below.
- Not available (no protocol support): input source selection, set-volume-to-level, repeat/shuffle, media info, extended state attributes, and real-time streaming updates.
- A MAC address is required. Power on sends a Wake-on-LAN magic packet before the power command as the players go into sleep mode after being powered off for some time.

## Requirements

- Oppo UDP-203 / UDP-205, or Magnetar player
- Player must be connected to your network
- The player communicates on TCP port 23 (Oppo) or 8102 (Magnetar)
- If you want to power the player on via the integration, enable network in standby in the player's settings
- Home Assistant `2026.6.3` or newer
- Python `3.14.2` or newer (matches Home Assistant's bundled Python)

## Protocol

This integration communicates with the player using the Oppo RS-232 and IP Control Protocol over TCP port 23. Commands are sent in the format `#CMD\r` and responses are received as `@OK value\r` or `@ER error\r`.

The integration enables verbose mode 3 (detailed unsolicited status updates) so the player pushes state changes - including playback progress - in real-time without polling.

## Supported Input Sources

### UDP-203
- Blu-Ray Player
- HDMI In
- ARC HDMI Out

### UDP-205
- Blu-Ray Player
- HDMI In
- Optical
- Coaxial
- USB Audio

## Services

The integration exposes the following services. All take an entity target.

| Service                          | Description                                               |
|----------------------------------|-----------------------------------------------------------|
| `oppo_udp.dimmer`                | Cycle the front-panel display brightness (On / Dim / Off) |
| `oppo_udp.pure_audio_toggle`     | Toggle Pure Audio mode (disables video output)            |
| `oppo_udp.info_toggle`           | Show or hide the on-screen display                        |
| `oppo_udp.audio_language_toggle` | Cycle to the next audio language or channel               |
| `oppo_udp.subtitle_toggle`       | Cycle to the next subtitle language                       |
| `oppo_udp.zoom`                  | Cycle zoom / aspect-ratio mode                            |

Example:

```yaml
service: oppo_udp.pure_audio_toggle
target:
  entity_id: media_player.oppo_udp_203
```

## Known Issues

- **Player becomes unresponsive to IP commands**: The Oppo UDP-20X players have a known issue where they can become completely unresponsive to IP control commands. IR and front panel controls still work in this state. To restore IP control, you need to physically disconnect the power cable, then power the player back on (the network stack only starts when the player is powered on).
- **Single connection limit**: The player only supports one TCP connection at a time. If you need multiple systems to control the player simultaneously, use [oppo-multiplexer](https://github.com/henrikwidlund/oppo-multiplexer) to broker the connections.
- **Input source switching after power on**: The player may accept a power on command and report success while still transitioning between states. If you want to switch input source immediately after powering on, add a delay of a few seconds to allow the player to fully start.
- **Track metadata accuracy**: The artist, album, and track name information may not always be available or accurate. This is a limitation of what the player provides.

## Troubleshooting

- **Cannot connect**: Ensure the player is powered on and on the same network. Check that no other application is connected to port 23 (only one TCP connection is allowed at a time).
- **Slow responses**: The integration rate-limits commands to 100ms intervals to avoid overwhelming the player.
- **State not updating**: If the player loses connection, it will automatically attempt to reconnect every 30 seconds.
