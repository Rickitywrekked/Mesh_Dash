# Mesh_Dash
A python script that connects a computer to a WiFi enabled Meshtastic Node and displays your local node data into easily digested infographics.

Meshtastic Live Dashboard + Messenger (Python)

A lightweight, zero-dependency (std-lib only) web dashboard & messenger for Meshtastic networks.
It connects to a Wi-Fi–enabled Meshtastic node over TCP (port 4403), ingests packets in real time, and provides:

A live dashboard (cards + table) with friendly names, telemetry, GPS, and mini-charts (battery/temp).

A messenger UI that groups conversations by pair (A⇄B) or broadcast and lets you reply directly from the browser.

Optional CSV logging (daily files).

A small JSON HTTP API you can script against.

No external backend required. The web UI is served by the Python script itself. Chart rendering is via CDN-hosted Chart.js.


Features

Connects via meshtastic.tcp_interface.TCPInterface to a node at HOST:4403.

Learns friendly names from the node list (longName/shortName) and displays them everywhere.

Merges packets into a per-node summary (last updated time).

Tracks rolling history per node for charts (tunable buffer length).

Messenger:

Auto-detects “my node” (the dashboard-connected device) on first direct message.

Conversation pairing: each thread is between exactly two nodes, shown in one place (no duplicates).

Smart default reply target: always the other node; if “me” isn’t known yet, replies go to the last sender in the thread.

Supports broadcast messaging (^all).

CSV logging per day (meshtastic_log_YYYY-MM-DD.csv).

Simple JSON API: /api/health, /api/nodes, /api/history, /api/send.


Requirements

Python 3.9+ (works on Windows/macOS/Linux).

Meshtastic Python libs:

pip install meshtastic pypubsub


from pubsub import pub comes from the PyPubSub package (pypubsub on pip).

A Meshtastic ESP32 node on Wi-Fi with TCP server enabled (default port 4403), and you know its IP address.


https://github.com/Rickitywrekked/Mesh_Dash.git

Getting Started

Clone the repo and open a terminal in the project folder:

git clone https://github.com/Rickitywrekked/Mesh_Dash.git
cd <repo>


(Recommended) create a virtual env and install deps:

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install --upgrade pip
pip install meshtastic pypubsub


Configure the script:

Open your main file (e.g., mesh_listen_v13.py or mesh_listen_v8_dash.py) and set the constants near the top:

HOST = "192.168.0.91"   # <-- your node's IP
API_HOST, API_PORT = "127.0.0.1", 8080
LOG_TO_CSV = True


Tip: Keep API_HOST = 127.0.0.1 for local use. If you want to view the dashboard from other devices on your LAN, change to API_HOST = "0.0.0.0" and allow the port in your firewall.

Run it:

python mesh_listen_v13.py


Open the dashboard:

http://127.0.0.1:8080/


You should see the live cards and table. Click into Messenger (in the UI) to view and reply to threads.


