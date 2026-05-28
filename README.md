# Oppo UDP-20X Home Assistant Integration

A custom Home Assistant integration for controlling Oppo UDP-203 and UDP-205 4K UHD Blu-Ray players via their TCP/IP control protocol.

## Features

- **Power control**: Turn on/off
- **Playback control**: Play, pause, stop, next/previous track
- **Volume control**: Volume up/down, set level, mute
- **Input source selection**: Switch between BD Player, HDMI In, ARC, Optical, etc.
- **Media info**: Track name, album, artist, playback position/duration
- **Real-time updates**: Uses verbose mode 3 for detailed streaming status including playback progress
- **Disc type detection**: Reports what type of disc is loaded (UHD BD, BD, DVD, CD, etc.)
- **Audio type reporting**: Shows the current audio codec in use
- **Automatic reconnection**: Reconnects automatically if the connection is lost

## Installation

### HACS (Recommended)

1. Add this repository as a custom repository in HACS
2. Install the "Oppo UDP-20X" integration
3. Restart Home Assistant

### Manual

1. Copy the `custom_components/oppo_udp` folder to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for "Oppo UDP-20X"
3. Enter the IP address of your player (port is optional, default is 23)
4. Select your model (UDP-203 or UDP-205)

## Requirements

- Oppo UDP-203 or UDP-205 player
- Player must be connected to your network
- The player communicates on TCP port 23
- If you want to power the player on via the integration, enable network in standby in the player's settings

## Protocol

This integration communicates with the player using the Oppo RS-232 and IP Control Protocol over TCP port 23. Commands are sent in the format `#CMD\r` and responses are received as `@OK value\r` or `@ER error\r`.

The integration enables verbose mode 3 (detailed unsolicited status updates) so the player pushes state changes — including playback progress — in real-time without polling.

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

## Known Issues

- **Player becomes unresponsive to IP commands**: The Oppo UDP-20X players have a known issue where they can become completely unresponsive to IP control commands. IR and front panel controls still work in this state. To restore IP control, you need to physically disconnect the power cable, then power the player back on (the network stack only starts when the player is powered on).
- **Single connection limit**: The player only supports one TCP connection at a time. If you need multiple systems to control the player simultaneously, use [oppo-multiplexer](https://github.com/henrikwidlund/oppo-multiplexer) to broker the connections.
- **Input source switching after power on**: The player may accept a power on command and report success while still transitioning between states. If you want to switch input source immediately after powering on, add a delay of a few seconds to allow the player to fully start.
- **Track metadata accuracy**: The artist, album, and track name information may not always be available or accurate. This is a limitation of what the player provides.

## Troubleshooting

- **Cannot connect**: Ensure the player is powered on and on the same network. Check that no other application is connected to port 23 (only one TCP connection is allowed at a time).
- **Slow responses**: The integration rate-limits commands to 100ms intervals to avoid overwhelming the player.
- **State not updating**: If the player loses connection, it will automatically attempt to reconnect every 30 seconds.
