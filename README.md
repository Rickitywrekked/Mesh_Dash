# Mesh_Dash

A lightweight Python web dashboard and messenger for Meshtastic networks. Connects to a Wi-Fi-enabled Meshtastic node over TCP and displays real-time node data through an intuitive web interface.

## Overview

This zero-dependency (standard library only) web application connects to a Meshtastic node via TCP (port 4403), ingests packets in real time, and provides:

- **Live dashboard** with friendly names, telemetry, GPS data, and mini-charts (battery/temperature)
- **Messenger interface** that groups conversations by node pairs (A⇄B) or broadcasts
- **Optional CSV logging** with daily file rotation
- **JSON HTTP API** for scripting and automation

No external backend required. The web UI is served directly by the Python script, with chart rendering via CDN-hosted Chart.js.

## Features

### Core Functionality
- Connects via `meshtastic.tcp_interface.TCPInterface` to node at HOST:4403
- Learns and displays friendly names from node list (longName/shortName)
- Merges packets into per-node summaries with last updated timestamps
- Tracks rolling history per node for chart visualization

### Messaging
- Auto-detects "my node" (the dashboard-connected device) on first direct message
- **Conversation pairing**: each thread shows exactly two nodes in one place (no duplicates)
- **Smart reply targeting**: always replies to the other node; defaults to last sender if "me" is unknown
- Supports broadcast messaging (`^all`)

### Data Management
- CSV logging per day (`meshtastic_log_YYYY-MM-DD.csv`)
- Simple JSON API endpoints: `/api/health`, `/api/nodes`, `/api/history`, `/api/send`

## Requirements

- **Python 3.9+** (Windows/macOS/Linux)
- **Meshtastic Python libraries**:
  ```bash
  pip install meshtastic pypubsub
  ```
- **Meshtastic ESP32 node** on Wi-Fi with TCP server enabled (default port 4403)
- Node's IP address

> **Note**: `from pubsub import pub` comes from the PyPubSub package (`pypubsub` on pip).

## Getting Started

### 1. Clone and Setup

```bash
git clone https://github.com/Rickitywrekked/Mesh_Dash.git
cd Mesh_Dash
```

### 2. Virtual Environment (Recommended)

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

pip install --upgrade pip
pip install meshtastic pypubsub
```

### 3. Configuration

Open your main file (e.g., `mesh_listen.py`) and set the constants near the top:

```python
HOST = "192.168.0.91"   # Your node's IP address
API_HOST, API_PORT = "127.0.0.1", 8080
LOG_TO_CSV = True
```

> **Tip**: Keep `API_HOST = "127.0.0.1"` for local use. To access the dashboard from other devices on your LAN, change to `API_HOST = "0.0.0.0"` and configure your firewall accordingly.

### 4. Run the Application

```bash
python mesh_listen.py
```

### 5. Access the Dashboard

Open your browser and navigate to:
```
http://127.0.0.1:8080/
```

You should see the live cards and table. Click into **Messenger** in the UI to view and reply to conversation threads.


