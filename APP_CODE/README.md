# ESP32 Logic Analyzer - Desktop Application

A cross-platform desktop application for the ESP32-based 8-channel logic analyzer. Built with Python, PyQt6, and PyQtGraph.

## Features

- **Real-time waveform display** - 8 stacked digital channels with zoom and pan
- **Live statistics panel** - Shows ESP32 health metrics (buffer counts, dropped packets, CRC errors)
- **Measurements** - Frequency, period, duty cycle, pulse width, edge counting
- **Cursor measurements** - Time delta and frequency between two cursors
- **Gap visualization** - Lost samples shown as gray regions
- **Board restart detection** - Handles ESP32 resets gracefully
- **Export** - VCD (PulseView/GTKWave compatible) and CSV
- **Cross-platform** - Works on Linux and Windows

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      main.py (GUI)                          │
├─────────────────────────────────────────────────────────────┤
│  WaveformView (PyQtGraph)    │  StatsPanel  │  Controls    │
├─────────────────────────────────────────────────────────────┤
│                    timeline.py (Timeline Model)             │
│  ChannelTimeline × 8  │  Gaps  │  Restarts  │  Measurements│
├─────────────────────────────────────────────────────────────┤
│                    parser.py (Frame Parser)                 │
│  TCP Client  │  Frame Sync  │  CRC16  │  RLE Decoder      │
├─────────────────────────────────────────────────────────────┤
│              ESP32 (WiFi TCP Port 5000)                     │
└─────────────────────────────────────────────────────────────┘
```

## Installation

### Linux (Fedora/Ubuntu/Debian/Arch)

```bash
# Install system dependencies
# Fedora:
sudo dnf install python3-qt6 python3-pyqtgraph

# Ubuntu/Debian:
sudo apt install python3-pyqt6 python3-pyqtgraph

# Arch:
sudo pacman -S python-pyqt6 python-pyqtgraph

# Or use pip (works on all):
pip install pyqt6 pyqtgraph
```

### Windows

```powershell
pip install pyqt6 pyqtgraph
```

## Usage

### Running the Application

```bash
cd APP_CODE
python3 main.py
```

### Loading a Captured Stream

1. Click **File → Open Stream...** or press `Ctrl+O`
2. Select a `.bin` file (raw stream capture)
3. The waveform will load and display

### Connecting to ESP32 (Live Capture)

1. Ensure ESP32 is running the logic analyzer firmware
2. Note the IP address from ESP32 serial output: `[wifi] on the laptop: tcp://192.168.x.y:5000`
3. Click **Connection → Connect...** or press `Ctrl+Shift+C`
4. Enter the IP address and port (default 5000)
4. Click OK - the app will connect and start streaming

### Controls

| Control | Action |
|---------|--------|
| Mouse wheel | Zoom in/out (time axis) |
| Mouse drag | Pan left/right |
| `F` key | Fit all data in view |
| Time/Div dropdown | Set time per division |
| Position slider | Navigate through capture |
| Channel checkboxes | Show/hide channels |

### Measurements

1. Click **View → Measurements...** or press `Ctrl+M`
2. Dialog shows frequency, period, duty cycle for all channels in visible range
3. For cursor measurements, add measurement cursors (planned feature)

### Export

1. Click **File → Export VCD...** for PulseView/GTKWave compatible format
2. Click **File → Export CSV (Edges)...** for edge list
3. Click **File → Export CSV (Gaps)...** for gap list

## File Formats

### VCD Export
Compatible with:
- **PulseView** (sigrok)
- **GTKWave**
- **Surfer** (saleae)

### CSV Export (Edges)
Columns: `time_ns`, `time_s`, `channel`, `level`, `sample_index`

### CSV Export (Gaps)
Columns: `start_ns`, `start_s`, `end_ns`, `end_s`, `duration_ns`, `duration_s`, `reason`

## Project Structure

```
APP_CODE/
├── main.py              # Main application entry point
├── parser.py            # Frame parser, TCP client, RLE decoder
├── timeline.py          # Timeline model (edges, gaps, restarts)
├── measurements.py      # Frequency, duty cycle, cursor measurements
├── export.py            # VCD and CSV export
├── waveform_viewer.py   # Standalone waveform viewer (legacy)
├── test_parser.py       # Parser unit tests
├── test_timeline.py     # Timeline unit tests
├── stream.bin           # Test stream (generated from firmware tests)
└── README.md            # This file
```

## Protocol Details

The ESP32 sends data over TCP (port 5000) with this frame format:

```
Frame: [A5 5A] [type] [flags] [seq] [len] [payload] [CRC16]
  - Sync: 0xA5, 0x5A
  - Type: 1=DATA, 2=STATS
  - Flags: bit 0 = GAP (samples lost before this packet)
  - Seq: u16, increments per frame
  - Len: u16, payload length
  - Payload: varies by type
  - CRC: CRC-16/CCITT-FALSE over bytes [2..end of payload]
```

**DATA Payload:**
```
[start_sample: u64] [lost_before: u32] [RLE events...]
```

**RLE Event:**
```
[state: u8] [run_length: varint]
- state: bit i = CHi level
- varint: 7 bits/byte, MSB=continuation
```

**STATS Payload (30 bytes):**
```
[version] [channels] [sample_hz] [produced] [consumed] 
[lost_buffers] [max_proc_us] [tx_drop_pkts] [tx_drop_samples]
```

## Testing

Run unit tests:

```bash
python3 test_parser.py
python3 test_timeline.py
```

Expected output for `stream.bin`:
- 140 frames
- 3,352,000 samples
- 152,000 lost samples
- 0 CRC errors
- 10 gaps

## Troubleshooting

### "Cannot connect to ESP32"
- Ensure laptop and ESP32 are on same WiFi network
- Check router doesn't have client isolation enabled
- Verify IP address from ESP32 serial output

### "No waveform displayed"
- Click **Fit All** (F key)
- Check channel visibility checkboxes
- Verify stream file loaded correctly (check status bar)

### High CPU usage
- Reduce update timer interval in `main.py` (default 50ms = 20 FPS)
- Hide channels not needed

## Development

### Adding Protocol Decoders

To add a protocol decoder (UART, I2C, SPI):

1. Create a new decoder module
2. Use `timeline.get_channel_edges()` to get edge lists
3. Implement protocol state machine
4. Add to Measurements dialog or create new view

### Custom Export Formats

Extend `export.py` with new export functions following the same pattern.

## License

MIT License - See LICENSE file for details.

## Credits

- ESP32 firmware by Aayu
- Reference decoder: `la_decode.py`
- Built with PyQt6 and PyQtGraph