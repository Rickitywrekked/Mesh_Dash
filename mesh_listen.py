# mesh_listen_v13_settings.py
import time, logging, queue, csv, os, json, threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as HTTPServer
from urllib.parse import urlparse, parse_qs
from collections import deque, defaultdict
from pubsub import pub
import meshtastic.tcp_interface  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# --- Config ---
HOST = "192.168.0.91"          # Meshtastic node IP (your gateway)
REFRESH_EVERY = 5.0            # console table refresh seconds
SHOW_UNKNOWN = True
SHOW_PER_PACKET = True
LOG_TO_CSV = True
LOG_PREFIX = "meshtastic_log"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
API_HOST, API_PORT = "127.0.0.1", 8080

# History / chat
HISTORY_MAXLEN = 300           # chart: last N points per node
HISTORY_SAMPLE_SECS = 2.0      # chart: min spacing between points
MAX_MSGS_PER_CONV = 2000       # chat: per-conversation ring size

# --- Settings (persistent) ---
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "settings.json")
settings_lock = threading.Lock()
DEFAULT_SETTINGS = {
    "units": {"temp":"F", "pressure":"hPa", "alt":"ft", "time":"12h"},
    "ui": {"rememberScroll": True, "theme":"dark", "cardDensity":"comfy", "autoScrollChat": True},
    "data": {"historyMax": HISTORY_MAXLEN, "sampleSecs": HISTORY_SAMPLE_SECS, "csvRetentionDays": 14, "autoScaleCharts": True},
    "msg": {"defaultTarget":"last_peer", "requestAck": True, "retry": 1, "hideSelfAsRecipient": True, "readOnly": False},
    "nodes": {"hideStaleMins": 0, "sortBy":"updated", "aliases": {}},
    "net": {"meshHost": HOST, "meshPort": 4403}
}
def _load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
            # shallow merge
            out = DEFAULT_SETTINGS.copy()
            for k,v in (s or {}).items():
                if isinstance(v, dict) and isinstance(out.get(k), dict):
                    nv = out[k].copy(); nv.update(v); out[k] = nv
                else:
                    out[k] = v
            return out
    except Exception:
        return DEFAULT_SETTINGS.copy()

def _save_settings(s):
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)

settings = _load_settings()

# --- Console handoff ---
outq: "queue.Queue[tuple[str, object]]" = queue.Queue()
def say(msg: str): outq.put(("msg", msg))
def emit_packet(packet: dict): outq.put(("packet", packet))

# --- State ---
nodes_lock = threading.Lock()
nodes: dict[str, dict] = {}
_connected = False

# Friendly names
node_names: dict[str, str] = {}
g_iface = None
my_id: str | None = None  # our local node ID like "!a0cb0f88" (when detected)

# Rolling history: nodeId -> deque of dict points
# point keys: t (epoch), batt, temp (°F stored), press (hPa), rh, rssi, snr
hist_lock = threading.Lock()
history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAXLEN))
last_hist_time: dict[str, float] = {}

# Chat storage: conversationId -> deque of msgs
# conv "^all" for broadcast; for DMs: conv is "pair:!A|!B" (A/B sorted)
msg_lock = threading.Lock()
messages: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_MSGS_PER_CONV))
last_msg_ts: dict[str, float] = {}  # conv -> last epoch

# De-dupe + send suppressor
seen_pkt_ids_lock = threading.Lock()
seen_pkt_ids: deque[str] = deque(maxlen=10000)
seen_pkt_set: set[str] = set()

recent_sends_lock = threading.Lock()
recent_sends: deque[tuple[str,str,float]] = deque(maxlen=512)  # (toId, text, ts)
RECENT_SEND_SUPPRESS_SECS = 5.0

# ---- helpers ----
def _refresh_names_from(iface):
    """Fetch my_id and node_names as robustly as possible."""
    global my_id
    try:
        mi = getattr(iface, "myInfo", None)
        if isinstance(mi, dict):
            u = mi.get("user") or {}
            if u.get("id"):
                my_id = u["id"]
            nm = (u.get("longName") or u.get("shortName"))
            if my_id and nm:
                node_names[my_id] = nm

        nd = getattr(iface, "nodes", None) or {}
        for entry in nd.values():
            u = (entry.get("user") or {})
            fid = u.get("id")
            if not fid:
                continue
            name = u.get("longName") or u.get("shortName")
            if name:
                node_names[fid] = name
            if my_id is None and u.get("isLocal"):
                my_id = fid
    except Exception:
        pass

def _fmt(x, fmt="{:.2f}"):
    try:
        if x is None: return "-"
        return fmt.format(float(x))
    except Exception:
        return str(x)
def _fmt_latlon(x): return _fmt(x, "{:.5f}")
def _fmt_alt(x):    return _fmt(x, "{:.0f}")
def _fmt_time(ts: float | None):
    if not ts: return "-"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
def _pressure_to_hpa(p):
    try: p = float(p)
    except Exception: return None
    return p / 100.0 if p > 1100 else p

def _csv_path_for_now():
    return os.path.join(SCRIPT_DIR, f"{LOG_PREFIX}_{time.strftime('%Y-%m-%d')}.csv")
def _csv_ensure_header(path: str, fieldnames: list[str]):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writeheader()
def _csv_write(row: dict):
    if not LOG_TO_CSV: return
    try:
        path = _csv_path_for_now()
        fns = ["ts_local","epoch","event","fromId","toId","portnum","rssi","snr",
               "battery","voltage","temp_c","temp_f","humidity","pressure_hpa",
               "lat","lon","alt","text"]
        _csv_ensure_header(path, fns)
        with open(path, "a", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=fns).writerow(row)
    except Exception as e:
        say(f"[CSV] write failed: {e}")

# Conversation key (order-independent)
def pair_conv_id(a: str, b: str) -> str:
    if not a or not b:
        return (a or b or "^all")
    a2, b2 = (a, b) if a <= b else (b, a)
    return f"pair:{a2}|{b2}"

def parse_pair_conv(cid: str) -> tuple[str,str] | None:
    if not cid.startswith("pair:"): return None
    try:
        body = cid.split(":",1)[1]
        a, b = body.split("|",1)
        return a, b
    except Exception:
        return None

def disp_name(node_id: str | None) -> str:
    if not node_id: return "unknown"
    return settings["nodes"]["aliases"].get(node_id) or node_names.get(node_id) or node_id

def _record_recent_send(to_id: str, text: str, ts: float):
    with recent_sends_lock:
        recent_sends.append((to_id, text, ts))

def _is_recent_send(from_id: str | None, to_id: str | None, text: str | None, now: float) -> bool:
    """True if this looks like the same message we just sent (suppress duplicate append)."""
    if not (from_id and to_id and text and my_id and from_id == my_id):
        return False
    with recent_sends_lock:
        for (tgt, txt, ts) in reversed(recent_sends):
            if tgt == to_id and txt == text and (now - ts) <= RECENT_SEND_SUPPRESS_SECS:
                return True
    return False

def _pkt_seen_once(pkt_id: str | None) -> bool:
    if not pkt_id:  # no id -> can't de-dupe by packet id
        return False
    with seen_pkt_ids_lock:
        if pkt_id in seen_pkt_set:
            return True
        seen_pkt_set.add(pkt_id)
        seen_pkt_ids.append(pkt_id)
        if len(seen_pkt_ids) == seen_pkt_ids.maxlen:
            seen_pkt_set.clear()
            seen_pkt_set.update(seen_pkt_ids)
    return False

# --- pubsub (bg thread) ---
def on_receive(packet, interface=None): emit_packet(packet)

def on_connection(interface=None):
    global _connected, g_iface
    _connected = True
    g_iface = interface
    _refresh_names_from(interface)
    say("[Connection] Established.")
    t0 = time.time()
    while (my_id is None) and (time.time()-t0 < 5.0):
        _refresh_names_from(interface)
        time.sleep(0.2)
    _touch_broadcast()

def on_connection_lost(interface=None):
    global _connected
    _connected = False
    say("[Connection] Lost.")
    _touch_broadcast()

def on_node_updated(node=None, interface=None, **_):
    if interface:
        _refresh_names_from(interface)

# --- history helpers (store °F) ---
def _record_history(node_id: str, rec: dict, now: float):
    lt = last_hist_time.get(node_id, 0.0)
    if now - lt < HISTORY_SAMPLE_SECS:
        return
    last_hist_time[node_id] = now

    tf = rec.get("temp_f")
    if tf is None and rec.get("temp_c") is not None:
        try:
            tf = float(rec["temp_c"]) * 9/5 + 32
        except Exception:
            tf = None

    pt = {
        "t": now,
        "batt": rec.get("batt"),
        "temp": tf,                       # Fahrenheit in history
        "press": rec.get("press_hpa"),    # hPa
        "rh": rec.get("rh"),
        "rssi": rec.get("rssi"),
        "snr": rec.get("snr"),
    }
    with hist_lock:
        history[node_id].append(pt)

# --- chat helpers ---
def _append_msg(conv: str, msg: dict):
    with msg_lock:
        messages[conv].append(msg)
        last_msg_ts[conv] = msg.get("epoch", time.time())

# --- packet processing (main thread) ---
def handle_packet(pkt: dict):
    d = (pkt.get("decoded") or {})
    port = d.get("portnum")
    frm, to = pkt.get("fromId"), pkt.get("toId")
    rssi, snr = pkt.get("rxRssi"), pkt.get("rxSnr")
    now = time.time()
    
    global my_id
    if my_id is None and port == "TEXT_MESSAGE_APP" and to and to != "^all":
        my_id = to

    # de-dupe by pkt id (if present)
    pkt_id = pkt.get("id") or d.get("id")
    if _pkt_seen_once(str(pkt_id) if pkt_id is not None else None):
        return

    friendly = node_names.get(frm)

    with nodes_lock:
        if frm not in nodes:
            nodes[frm] = {
                "to":None,"rssi":None,"snr":None,"batt":None,"voltage":None,
                "temp_c":None,"temp_f":None,"rh":None,"press_hpa":None,
                "lat":None,"lon":None,"alt":None,"text":None,
                "name": friendly,
                "updated":None
            }
        rec = nodes[frm]
        rec["to"] = to
        if rssi is not None: rec["rssi"] = rssi
        if snr  is not None: rec["snr"]  = snr
        if friendly and not rec.get("name"):
            rec["name"] = friendly

    if SHOW_PER_PACKET:
        if port == "TEXT_MESSAGE_APP":
            say(f"[TEXT] {frm} → {to} | rssi={rssi} snr={snr} | {d.get('text')}")
        elif port == "POSITION_APP":
            p = d.get("position") or {}
            say(f"[GPS]  {frm} → {to} | rssi={rssi} snr={snr} | lat={p.get('latitude')} lon={p.get('longitude')} alt={p.get('altitude')}")
        elif port not in ("TELEMETRY_APP",) and SHOW_UNKNOWN:
            say(f"[UNK]  {frm} → {to} | rssi={rssi} snr={snr} | port={port}")

    base = {"ts_local": datetime.now().isoformat(timespec="seconds"),
            "epoch": f"{now:.0f}","event": port or "UNKNOWN",
            "fromId": frm,"toId": to,"portnum": port,
            "rssi": rssi,"snr": snr,
            "battery": None,"voltage": None,"temp_c": None,"temp_f": None,
            "humidity": None,"pressure_hpa": None,"lat": None,"lon": None,"alt": None,"text": None}

    changed = False
    if port == "TEXT_MESSAGE_APP":
        txt = d.get("text") or ""
        scope = "broadcast" if (to == "^all") else "dm"

        # suppress duplicate if this exactly matches a very recent send from us
        if not _is_recent_send(frm, to, txt, now):
            if scope == "broadcast":
                conv = "^all"
            else:
                conv = pair_conv_id(frm or "", to or "")
            msg = {
                "epoch": now,
                "iso": datetime.now().isoformat(timespec="seconds"),
                "fromId": frm, "toId": to,
                "text": txt, "rssi": rssi, "snr": snr,
                "scope": scope
            }
            _append_msg(conv, msg)

        with nodes_lock:
            nodes[frm]["text"] = txt[:120]; nodes[frm]["updated"] = now
            rec = nodes[frm]
        _csv_write(base | {"text": txt}); changed = True

    elif port == "POSITION_APP":
        p = d.get("position") or {}
        with nodes_lock:
            nodes[frm]["lat"] = p.get("latitude")
            nodes[frm]["lon"] = p.get("longitude")
            nodes[frm]["alt"] = p.get("altitude")
            nodes[frm]["updated"] = now
            rec = nodes[frm]
        _csv_write(base | {"lat": rec["lat"],"lon": rec["lon"],"alt": rec["alt"]}); changed = True

    elif port == "TELEMETRY_APP":
        t  = d.get("telemetry") or {}
        dm = t.get("deviceMetrics") or {}
        em = t.get("environmentMetrics") or {}
        with nodes_lock:
            if "batteryLevel" in dm: nodes[frm]["batt"] = dm.get("batteryLevel")
            if "voltage"      in dm: nodes[frm]["voltage"] = dm.get("voltage")
            if em:
                c  = em.get("temperature"); rh = em.get("relativeHumidity"); pa = _pressure_to_hpa(em.get("barometricPressure"))
                if c is not None:
                    try:
                        c = float(c)
                        nodes[frm]["temp_c"] = c
                        nodes[frm]["temp_f"] = c*9/5+32  # compute °F
                    except Exception:
                        pass
                if rh is not None: nodes[frm]["rh"] = rh
                if pa is not None: nodes[frm]["press_hpa"] = pa
            nodes[frm]["updated"] = now
            rec = nodes[frm]
        _csv_write(base | {"battery": rec.get("batt"),"voltage": rec.get("voltage"),
                           "temp_c": rec.get("temp_c"),"temp_f": rec.get("temp_f"),
                           "humidity": rec.get("rh"),"pressure_hpa": rec.get("press_hpa")})
        changed = True

    else:
        if SHOW_UNKNOWN: _csv_write(base)

    if changed:
        _record_history(frm, rec, now)
        _touch_broadcast()

# --- console table (°F in console, same as before) ---
def render_table():
    with nodes_lock:
        snap = list(sorted(nodes.items(), key=lambda kv: kv[1].get("updated") or 0, reverse=True))
    rows = []
    header = ("Node".ljust(16),"Batt%","V","T(°F)","P(hPa)","RH%","RSSI","SNR","Lat","Lon","Alt","Last Text".ljust(24),"Updated")
    hdr = " | ".join(header)
    rows.append(hdr); rows.append("-"*len(hdr))
    for nid, rec in snap:
        display = (rec.get("name") or nid)
        rows.append(" | ".join([
            str(display).ljust(16),
            _fmt(rec.get("batt"), "{:.0f}"), _fmt(rec.get("voltage")),
            _fmt(rec.get("temp_f")), _fmt(rec.get("press_hpa")),
            _fmt(rec.get("rh"), "{:.1f}"), _fmt(rec.get("rssi"), "{:.0f}"), _fmt(rec.get("snr")),
            _fmt_latlon(rec.get("lat")), _fmt_latlon(rec.get("lon")), _fmt_alt(rec.get("alt")),
            (rec.get("text") or "-")[:24].ljust(24), _fmt_time(rec.get("updated")),
        ]))
    return "\n".join(rows)

# --- JSON snapshot ---
def _nodes_json_snapshot():
    with nodes_lock, settings_lock:
        now_iso = datetime.now().isoformat(timespec="seconds")
        out = {"connected": _connected, "server_time": now_iso, "nodes": {}, "my_id": my_id,
               "my_name": (disp_name(my_id) if my_id else None), "names": node_names,
               "settings": settings}
        for k,v in nodes.items():
            out["nodes"][k] = {
                **v,
                "name": v.get("name"),
                "updated_iso": datetime.fromtimestamp(v["updated"]).isoformat(timespec="seconds") if v.get("updated") else None,
                "updated_epoch": v.get("updated"),
            }
        return out

def _history_json_snapshot(limit_per_node: int | None):
    with hist_lock:
        result = {}
        for nid, dq in history.items():
            pts = list(dq)[-limit_per_node:] if limit_per_node else list(dq)
            result[nid] = [{"t": round(p["t"],2),
                            "batt": p["batt"], "temp": p["temp"], "press": p["press"],
                            "rh": p["rh"], "rssi": p["rssi"], "snr": p["snr"]} for p in pts]
        return result

# Conversations / Messages snapshots
def _conversations_snapshot():
    # Existing conversations
    with msg_lock:
        conv_keys = set(messages.keys())

    # Seed with ANY known nodes (from traffic OR radio map), paired with me
    known_ids = set(node_names.keys())
    with nodes_lock:
        known_ids.update(nodes.keys())

    with settings_lock:
        hide_self = settings["msg"].get("hideSelfAsRecipient", True)

    if my_id:
        peers = [nid for nid in known_ids if (nid != my_id or not hide_self)]
        for nid in peers:
            if nid == my_id and hide_self:
                continue
            conv_keys.add(pair_conv_id(my_id, nid))

    out = []
    for cid in conv_keys or []:
        if cid == "^all":
            nm = "Broadcast (^all)"
            last_t = last_msg_ts.get(cid, 0.0)
        else:
            pair = parse_pair_conv(cid)
            if not pair:
                nm = cid
                last_t = last_msg_ts.get(cid, 0.0)
            else:
                a, b = pair
                if my_id and (my_id == a or my_id == b):
                    peer = b if my_id == a else a
                    if hide_self and peer == my_id:
                        continue
                    nm = disp_name(peer)
                    last_t = last_msg_ts.get(cid, 0.0)
                    if last_t == 0.0:
                        with nodes_lock:
                            last_t = (nodes.get(peer) or {}).get("updated") or 0.0
                else:
                    nm = f"{disp_name(a)} \u2194 {disp_name(b)}"
                    last_t = last_msg_ts.get(cid, 0.0)

        out.append({"id": cid, "name": nm, "last_epoch": last_t})

    # Sort: Broadcast pinned on top, then newest activity
    out.sort(key=lambda x: (x["id"]!="^all", -x["last_epoch"], x["name"].lower()))
    return out

def _messages_snapshot(conv_id: str, limit: int | None = None, since: float | None = None, include_broadcast: bool = False):
    with msg_lock:
        base = list(messages.get(conv_id, deque()))
    pair = parse_pair_conv(conv_id)
    if include_broadcast and pair:
        a, b = pair
        with msg_lock:
            bcast = list(messages.get("^all", deque()))
        for m in bcast:
            if m.get("fromId") in (a, b):
                base.append(m | {"scope": "broadcast"})
    base.sort(key=lambda m: m.get("epoch", 0))
    if since is not None:
        base = [m for m in base if m.get("epoch", 0) > since]
    if limit is not None:
        base = base[-limit:]
    return base

# --- minimal "broadcast" mark (polling clients just re-fetch) ---
def _touch_broadcast():
    pass

# --- HTML (simple, pretty with dashboard/chat/settings) ---

DASHBOARD_SIMPLE = """<!doctype html>
<html><meta charset="utf-8"/><title>Meshtastic Simple</title>
<style>
body{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:16px;}
pre{white-space:pre-wrap;font-family:ui-monospace,Consolas,Menlo,monospace}
</style>
<h1>Meshtastic Simple Table</h1>
<div id="meta" style="color:#666;margin:6px 0 10px;"></div>
<pre id="tbl">loading…</pre>
<script>
async function fetchNodes(){
  const r=await fetch('/api/nodes?t='+Date.now(),{cache:'no-store'});
  return await r.json();
}
function unitize(v, kind, cfg){
  if(v==null) return '-';
  const n=Number(v); if(!isFinite(n)) return '-';
  if(kind==='temp'){
    return (cfg.units.temp==='C') ? ((n-32)*5/9).toFixed(2) : n.toFixed(2);
  }
  return n.toFixed(2);
}
function unitLabels(cfg){
  return {
    temp: cfg.units.temp==='C'?'T(°C)':'T(°F)',
    press: 'P(hPa)',
  };
}
function asTable(snap){
  const cfg = snap.settings || {units:{temp:'F'}};
  const lbl = unitLabels(cfg);
  const header = ["Node".padEnd(16),"Batt%","V",lbl.temp,"P(hPa)","RH%","RSSI","SNR","Lat","Lon","Alt","Last Text".padEnd(24),"Updated"].join(" | ");
  const bar = "-".repeat(header.length);
  const rows=[header,bar];
  const items=Object.entries(snap.nodes).sort((a,b)=>(b[1].updated_epoch||0)-(a[1].updated_epoch||0));
  for(const [id,v] of items){
    const name=(v.name&&v.name.trim().length)?v.name:id;
    const r = [
      String(name).padEnd(16),
      v.batt==null?"-":Number(v.batt).toFixed(0),
      v.voltage==null?"-":Number(v.voltage).toFixed(2),
      v.temp_f==null?"-":unitize(v.temp_f,'temp',cfg),
      v.press_hpa==null?"-":Number(v.press_hpa).toFixed(2),
      v.rh==null?"-":Number(v.rh).toFixed(1),
      v.rssi==null?"-":Number(v.rssi).toFixed(0),
      v.snr==null?"-":Number(v.snr).toFixed(2),
      v.lat==null?"-":Number(v.lat).toFixed(5),
      v.lon==null?"-":Number(v.lon).toFixed(5),
      v.alt==null?"-":Number(v.alt).toFixed(0),
      (v.text||"-").slice(0,24).padEnd(24),
      v.updated_iso? new Date(v.updated_iso).toLocaleTimeString():"-"
    ].join(" | ");
    rows.push(r);
  }
  return rows.join("\\n");
}
async function tick(){
  try{
    const snap=await fetchNodes();
    document.getElementById('meta').textContent = `connected=${snap.connected} | server=${snap.server_time} | nodes=${Object.keys(snap.nodes).length}`;
    document.getElementById('tbl').textContent = asTable(snap);
  }catch(e){ document.getElementById('tbl').textContent='error'; }
}
setInterval(tick,1000); tick();
</script></html>
"""

DASHBOARD_PRETTY = """<!doctype html>
<html lang="en"><meta charset="utf-8"/>
<title>Meshtastic Dashboard · Chat</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3"></script>
<style>
:root{--bg:#0b1020;--card:#121a2e;--muted:#8391a7;--fg:#e8efff;--ok:#21c07a;--warn:#f3b32a;--bad:#e35d6a;--line:#1f2b47;--accent:#6fb6ff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif}
.wrap{padding:16px 20px;max-width:1250px;margin:0 auto}
h1{margin:0 0 10px;font-weight:650;letter-spacing:.2px}
.meta{color:var(--muted);margin-bottom:12px}
.nav{display:flex;gap:10px;margin-bottom:12px}
.nav button{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:999px;padding:8px 14px;cursor:pointer}
.nav button.active{outline:2px solid var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;box-shadow:0 6px 18px rgba(0,0,0,.15)}
.card h3{margin:0 0 8px;font-size:16px;font-weight:650}
.kv{display:flex;flex-wrap:wrap;gap:8px 14px;margin:8px 0 6px}
.kv div{color:var(--muted);font-size:12px}
.kv b{color:var(--fg);font-weight:650;margin-left:6px}
.kv a{margin-left:8px;color:var(--accent);text-decoration:none}
.kv a:hover{text-decoration:underline}
.bar{height:8px;background:#1c2944;border-radius:999px;overflow:hidden}
.bar>span{display:block;height:100%;background:linear-gradient(90deg,#3577ff,#53d2ff)}
.small{font-size:12px;color:var(--muted);margin-top:6px}
canvas{width:100%;height:120px}
.warn{color:var(--warn)} .bad{color:var(--bad)} .ok{color:var(--ok)}
.table{margin-top:18px;border:1px solid var(--line);border-radius:10px;overflow:auto}
table{width:100%;border-collapse:collapse;background:var(--card)}
th,td{padding:10px;border-bottom:1px solid var(--line);text-align:left;font-size:13px}
th{color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--card)}
tr:last-child td{border-bottom:none}

/* Chat */
.chat{display:grid;grid-template-columns:280px 1fr;gap:14px}
.convlist{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:auto;max-height:70vh}
.convlist h3{margin:10px 12px;font-size:14px;color:var(--muted)}
.conv{display:block}
.conv button{display:block;width:100%;text-align:left;border:none;border-bottom:1px solid var(--line);background:transparent;padding:12px;color:var(--fg);cursor:pointer}
.conv button:hover{background:#0f1730}
.conv button.active{background:#12224a;outline:2px solid var(--accent)}
.conv .name{font-weight:600;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* Thread */
.thread{display:flex;flex-direction:column;min-height:70vh;background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:0 6px 18px rgba(0,0,0,.15)}
.thread .head{padding:10px 12px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center;justify-content:space-between}
.thread .head .who{display:flex;gap:10px;align-items:center}
.thread .who .to{color:var(--accent)}
.thread .msgs{padding:12px;overflow:auto;flex:1}
.bubble{max-width:70%;margin:6px 0;padding:10px 12px;border-radius:12px;line-height:1.35;position:relative}
.me{background:#27427c;margin-left:auto} .them{background:#1a2542;margin-right:auto}
.bubble .meta{font-size:11px;color:#c7d2e1;margin-top:6px}
.bubble .tag{position:absolute;top:-8px;right:8px;font-size:10px;color:#ffd56e}
.compose{display:flex;gap:8px;border-top:1px solid var(--line);padding:10px;background:#0e1529;border-radius:0 0 12px 12px}
.compose input[type=text]{flex:1;background:#0b1020;border:1px solid var(--line);color:var(--fg);padding:10px;border-radius:10px}
.compose button{background:var(--accent);border:none;color:#08111f;font-weight:700;padding:10px 14px;border-radius:10px;cursor:pointer}
.compose button:disabled{opacity:.6;cursor:not-allowed}
.hide{display:none}
.warnTxt{font-size:11px;color:#f3b32a;margin-left:8px}

/* Settings */
#settings.hide{display:none}
.form{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
.form h3{margin:6px 0 10px}
.row{display:flex;flex-wrap:wrap;gap:14px;margin:8px 0}
.row label{font-size:13px;color:var(--muted)}
.row select,.row input[type=checkbox]{margin-left:6px}
.saveok{color:#7ce2a9;font-size:12px;margin-left:8px}
</style>

<div class="wrap">
  <h1>Meshtastic · Dashboard & Chat</h1>
  <div class="nav">
    <button id="tabDash" class="active">Dashboard</button>
    <button id="tabChat">Messages</button>
    <button id="tabSettings">Settings</button>
  </div>
  <div class="meta" id="meta">loading…</div>

  <!-- DASHBOARD -->
  <div id="dash">
    <div class="grid" id="cards"></div>
    <div class="table">
      <table id="tbl"><thead><tr id="tblHead">
        <th>Node</th><th>Batt%</th><th>V</th><th>T(°F)</th><th>P(hPa)</th><th>RH%</th><th>RSSI</th><th>SNR</th><th>Lat</th><th>Lon</th><th>Alt</th><th>Updated</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>

  <!-- CHAT -->
  <div id="chat" class="hide">
    <div class="chat">
      <div class="convlist">
        <h3>Conversations</h3>
        <div id="conv" class="conv"></div>
      </div>
      <div class="thread">
        <div class="head">
          <div class="who">
            <div id="convTitle">Select a conversation</div>
            <div id="toWrap" class="meta"></div>
          </div>
          <div id="meInfo" class="meta"></div>
        </div>
        <div id="msgs" class="msgs"></div>
        <div class="compose">
          <input id="msgBox" type="text" placeholder="Type a message…" />
          <button id="sendBtn">Send</button>
          <span id="sendWarn" class="warnTxt hide">Delivery unconfirmed…</span>
        </div>
      </div>
    </div>
  </div>

  <!-- SETTINGS -->
  <div id="settings" class="hide">
    <div class="form">
      <h3>Units</h3>
      <div class="row">
        <label>Temperature
          <select id="setTemp"><option value="F">°F</option><option value="C">°C</option></select>
        </label>
        <label>Pressure
          <select id="setPress"><option value="hPa">hPa</option><option value="inHg">inHg</option></select>
        </label>
        <label>Altitude
          <select id="setAlt"><option value="ft">ft</option><option value="m">m</option></select>
        </label>
        <label>Time
          <select id="setTime"><option value="12h">12h</option><option value="24h">24h</option></select>
        </label>
      </div>

      <h3>Chat</h3>
      <div class="row">
        <label><input type="checkbox" id="setHideSelf"> Hide my node in recipient list</label>
        <label><input type="checkbox" id="setAck"> Request ACK</label>
        <label><input type="checkbox" id="setReadOnly"> Read-only (disable send)</label>
      </div>

      <h3>UI</h3>
      <div class="row">
        <label><input type="checkbox" id="setScroll"> Remember scroll on refresh</label>
      </div>

      <div class="row">
        <button id="saveBtn">Save settings</button><span id="saveOk" class="saveok hide">Saved ✓</span>
      </div>
    </div>
  </div>
</div>

<script>
let charts = {}; // dashboard charts
let activeConv = null;
let lastMsgSeen = 0;
let myId = null;
let myName = null;
let names = {};
let sendTarget = null; // current recipient id (or '^all')
let convTimer = null;  // refresh conversation list
let warnTimer = null;  // send warning timer
let cfg = null;        // settings from server

function battClass(b){ if(b==null) return ''; if(b<=15) return 'bad'; if(b<=30) return 'warn'; return 'ok'; }
function battPct(b){ return (b==null)?0:Math.max(0,Math.min(100,Number(b)||0)); }
function nice(v,d=2){ if(v==null||v===undefined) return '-'; const n=Number(v); return isFinite(n)?n.toFixed(d):v; }
function timeStrISO(iso){
  if(!iso) return '-';
  const dt=new Date(iso);
  const opt={hour:'numeric',minute:'2-digit',second:'2-digit', hour12: (cfg && cfg.units && cfg.units.time==='12h')};
  return dt.toLocaleTimeString(undefined,opt);
}
function dispName(id){ if(!id) return 'unknown'; if(id===myId) return myName || (names[id]||id); return (cfg?.nodes?.aliases?.[id]) || names[id] || id; }

// Units conversions
function toTempDisplay(tempF){
  if(tempF==null) return null;
  const n=Number(tempF); if(!isFinite(n)) return null;
  if(cfg?.units?.temp==='C'){ return ( (n-32)*5/9 ); }
  return n;
}
function tempLabel(){ return (cfg?.units?.temp==='C') ? '°C' : '°F'; }

function toPressDisplay(hpa){
  if(hpa==null) return null;
  const n=Number(hpa); if(!isFinite(n)) return null;
  if(cfg?.units?.pressure==='inHg'){ return n * 0.0295299830714; }
  return n;
}
function pressLabel(){ return (cfg?.units?.pressure==='inHg') ? 'inHg' : 'hPa'; }

function toAltDisplay(m){
  if(m==null) return null;
  const n=Number(m); if(!isFinite(n)) return null;
  if(cfg?.units?.alt==='ft'){ return n * 3.28084; }
  return n;
}
function altLabel(){ return (cfg?.units?.alt==='ft') ? 'ft' : 'm'; }

async function fetchJSON(url, opts){ const r=await fetch(url,opts||{cache:'no-store'}); return await r.json(); }

//// DASHBOARD ////
async function loadDashboard(){
  const remember = !!(cfg && cfg.ui && cfg.ui.rememberScroll);
  const sx = remember ? (window.scrollX || 0) : 0;
  const sy = remember ? (window.scrollY || document.documentElement.scrollTop || 0) : 0;

  const [snap, hist] = await Promise.all([
    fetchJSON('/api/nodes?t='+Date.now()),
    fetchJSON('/api/history?n=150&t='+Date.now())
  ]);
  myId = snap.my_id || null;
  myName = snap.my_name || null;
  names = snap.names || {};
  cfg = snap.settings || cfg;

  document.getElementById('meta').textContent =
    `connected=${snap.connected} | server=${snap.server_time} | nodes=${Object.keys(snap.nodes).length}`;

  // update table headers for units
  const th = document.getElementById('tblHead');
  th.innerHTML = `
    <th>Node</th><th>Batt%</th><th>V</th>
    <th>T(${tempLabel()})</th><th>P(${pressLabel()})</th><th>RH%</th>
    <th>RSSI</th><th>SNR</th><th>Lat</th><th>Lon</th><th>Alt(${altLabel()})</th><th>Updated</th>
  `;

  const cards = document.getElementById('cards');
  for (const k in charts) { try { charts[k].destroy(); } catch (e) {} delete charts[k]; }
  cards.innerHTML = '';

  const entries = Object.entries(snap.nodes).sort((a,b)=>(b[1].updated_epoch||0)-(a[1].updated_epoch||0));
  for(const [id, v] of entries){
    const name = (v.name&&v.name.trim().length)?v.name:id;
    const h = hist[id] || [];
    const cid = 'c_'+id.replace(/[^a-zA-Z0-9_]/g,'_');
    const lat = (v.lat==null)? null : Number(v.lat).toFixed(5);
    const lon = (v.lon==null)? null : Number(v.lon).toFixed(5);
    const altDisp = toAltDisplay(v.alt); const altTxt = (altDisp==null? '-' : altDisp.toFixed(0)+' '+altLabel());
    const pressDisp = toPressDisplay(v.press_hpa);
    const tempDisp = toTempDisplay(v.temp_f);
    const mapLink = (lat && lon) ? `<a href="https://maps.google.com/?q=${lat},${lon}" target="_blank" rel="noopener">map</a>` : '';

    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <h3>${name}</h3>
      <div class="kv">
        <div>Battery<b class="${battClass(v.batt)}">${nice(v.batt,0)}%</b></div>
        <div>Voltage<b>${nice(v.voltage,2)} V</b></div>
        <div>Temp<b>${tempDisp==null?'-':tempDisp.toFixed(1)} ${tempLabel()}</b></div>
        <div>Pressure<b>${pressDisp==null?'-':pressDisp.toFixed(2)} ${pressLabel()}</b></div>
        <div>RH<b>${nice(v.rh,1)} %</b></div>
        <div>RSSI<b>${nice(v.rssi,0)} dBm</b></div>
        <div>SNR<b>${nice(v.snr,2)} dB</b></div>
        <div>GPS<b>${lat && lon ? (lat+', '+lon) : '-'}</b>${mapLink}</div>
        <div>Alt<b>${altTxt}</b></div>
        <div>Updated<b>${timeStrISO(v.updated_iso)}</b></div>
      </div>
      <div class="bar"><span style="width:${battPct(v.batt)}%"></span></div>
      <div class="small">${(v.text||'-')}</div>
      <canvas id="${cid}"></canvas>
    `;
    cards.appendChild(card);

    const ctx = card.querySelector('canvas').getContext('2d');
    // history temp is stored in °F; convert if needed
    const labels = h.map(p => new Date(p.t*1000).toLocaleTimeString(undefined,{hour12:(cfg?.units?.time==='12h')}));
    const dsBatt = h.map(p => p.batt);
    const dsTemp = h.map(p => {
      if(p.temp==null) return null;
      return (cfg?.units?.temp==='C') ? ((p.temp-32)*5/9) : p.temp;
    });
    const tLabel = 'Temp ' + tempLabel();
    charts[cid]=new Chart(ctx, {
      type:'line',
      data:{labels, datasets:[
        {label:'Battery %', data: dsBatt, yAxisID:'y1', tension:.3},
        {label:tLabel, data: dsTemp, yAxisID:'y2', tension:.3}
      ]},
      options:{
        animation:false,
        plugins:{legend:{labels:{color:'#e8efff'}}},
        scales:{
          x:{ticks:{color:'#8391a7'} , grid:{color:'#1f2b47'}},
          y1:{position:'left', suggestedMin:0, suggestedMax:100, ticks:{color:'#8391a7'}, grid:{color:'#1f2b47'}},
          y2:{position:'right', ticks:{color:'#8391a7'}, grid:{display:false}}
        }
      }
    });
  }

  const tbody = document.querySelector('#tbl tbody'); tbody.innerHTML='';
  for(const [id,v] of entries){
    const name=(v.name&&v.name.trim().length)?v.name:id;
    const tr=document.createElement('tr');
    const tempD = toTempDisplay(v.temp_f);
    const pressD = toPressDisplay(v.press_hpa);
    const altD = toAltDisplay(v.alt);
    const cells=[
      name, nice(v.batt,0), nice(v.voltage,2),
      (tempD==null?'-':tempD.toFixed(1)),
      (pressD==null?'-':pressD.toFixed(2)),
      nice(v.rh,1), nice(v.rssi,0), nice(v.snr,2),
      v.lat==null?'-':Number(v.lat).toFixed(5),
      v.lon==null?'-':Number(v.lon).toFixed(5),
      (altD==null?'-':altD.toFixed(0)),
      timeStrISO(v.updated_iso)
    ];
    cells.forEach((c,idx)=>{
      const td=document.createElement('td'); td.textContent=c;
      if(idx===1){ if(v.batt!=null){ const b=Number(v.batt); if(b<=15) td.className='bad'; else if(b<=30) td.className='warn'; else td.className='ok'; } }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }

  if(remember){ requestAnimationFrame(()=>{ window.scrollTo(sx, sy); }); }
}

//// CHAT UI ////
async function loadConversations(){
  const convs = await fetchJSON('/api/conversations?t='+Date.now());
  const wrap = document.getElementById('conv'); wrap.innerHTML='';
  for(const c of convs){
    const btn=document.createElement('button');
    btn.dataset.id = c.id;
    btn.onclick = ()=> selectConv(c.id, c.name);
    btn.innerHTML = `<span class="name">${c.name}</span>`;
    if (activeConv && c.id===activeConv) btn.classList.add('active');
    wrap.appendChild(btn);
  }
}
function setActiveConvButton(){
  const wrap = document.getElementById('conv');
  Array.from(wrap.querySelectorAll('button')).forEach(b=>{
    if(b.dataset.id===activeConv) b.classList.add('active'); else b.classList.remove('active');
  });
}
function renderMsg(m){
  const me = (myId && m.fromId===myId);
  const div=document.createElement('div');
  div.className='bubble ' + (me?'me':'them');
  const who = me ? 'You' : (dispName(m.fromId || ''));
  const tag = (m.scope==='broadcast') ? `<span class="tag">broadcast</span>` : '';
  const opt={hour:'numeric',minute:'2-digit',second:'2-digit', hour12:(cfg?.units?.time==='12h')};
  div.innerHTML = `${tag}${m.text}<div class="meta">${who} · ${new Date(m.epoch*1000).toLocaleTimeString(undefined,opt)}${m.rssi!=null? ' · rssi '+m.rssi:''}</div>`;
  return div;
}
async function fetchMessages(conv, since, includeBroadcast){
  const url = `/api/messages?conv=${encodeURIComponent(conv)}`
    + (since?('&since='+since):'')
    + (includeBroadcast? '&include_broadcast=1' : '')
    + '&t='+Date.now();
  return await fetchJSON(url);
}
function headerFor(cid){
  if(cid==='^all') return 'Broadcast (^all)';
  const pair = cid.startsWith('pair:') ? cid.slice(5).split('|') : null;
  if(!pair) return cid;
  const [a,b] = pair;
  if(myId && (myId===a || myId===b)){
    const peer = (myId===a)? b : a;
    return dispName(peer);
  }
  return `${dispName(a)} \u2194 ${dispName(b)}`;
}
function peerForConv(cid, msgList){
  if(cid==='^all') return '^all';
  const pair = cid.startsWith('pair:') ? cid.slice(5).split('|') : null;
  if(!pair) return null;
  const [a,b] = pair;
  if(myId && (myId===a || myId===b)) return (myId===a)? b : a;
  if(msgList && msgList.length){
    const last = msgList[msgList.length-1];
    if(last && last.fromId && last.fromId!=='^all') return last.fromId;
  }
  return b;
}
let chatVisible=false;

async function selectConv(id, name){
  activeConv = id; lastMsgSeen = 0;

  const snap = await fetchJSON('/api/nodes?t='+Date.now());
  myId = snap.my_id || null; myName = snap.my_name || null; names = snap.names || {}; cfg = snap.settings || cfg;

  const includeB = (id !== '^all');
  const list = await fetchMessages(id, null, includeB);

  sendTarget = peerForConv(id, list);

  const toLabel = (id==='^all') ? 'Broadcast' : (sendTarget ? dispName(sendTarget) : '(selecting peer…)');
  document.getElementById('convTitle').textContent = headerFor(id);
  document.getElementById('toWrap').textContent = (id==='^all') ? 'To: Broadcast' : ('To: ' + toLabel);
  document.getElementById('meInfo').textContent = myId ? `me=${dispName(myId)}` : '';

  const ro = !!(cfg && cfg.msg && cfg.msg.readOnly);
  document.getElementById('sendBtn').disabled = ro;
  document.getElementById('msgBox').disabled = ro;

  const box = document.getElementById('msgs'); box.innerHTML='';
  list.forEach(m=> box.appendChild(renderMsg(m)));
  box.scrollTop = box.scrollHeight;
  if(list.length) lastMsgSeen = list[list.length-1].epoch;
  setActiveConvButton();
}

async function pollActive(){
  if(!activeConv || !chatVisible) return;
  const includeBroadcast = (activeConv !== '^all');
  const box = document.getElementById('msgs');
  const nearBottom = (box.scrollHeight - box.scrollTop - box.clientHeight) < 120;
  const inc = await fetchMessages(activeConv, lastMsgSeen || 0, includeBroadcast);
  if(inc.length){
    inc.forEach(m=> box.appendChild(renderMsg(m)));
    lastMsgSeen = inc[inc.length-1].epoch;
    if(nearBottom) box.scrollTop = box.scrollHeight;
  }
}
function showSendWarn(show){ const el=document.getElementById('sendWarn'); if(show) el.classList.remove('hide'); else el.classList.add('hide'); }
async function sendCurrent(){
  if(cfg && cfg.msg && cfg.msg.readOnly) return;
  const input = document.getElementById('msgBox');
  const btn = document.getElementById('sendBtn');
  const text = input.value.trim();
  if(!text){ return; }
  if(!activeConv){ alert('Pick a conversation'); return; }

  btn.disabled = true; showSendWarn(false);
  try{
    const payload = (activeConv==='^all')
      ? { to: '^all', text, channelIndex: 0, wantAck: !!(cfg?.msg?.requestAck) }
      : { conv: activeConv, to: (sendTarget||''), text, channelIndex: 0, wantAck: !!(cfg?.msg?.requestAck) };

    const r = await fetch('/api/send', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    const res = await r.json();
    if(!r.ok){
      alert('Send failed: '+(res.error||r.status));
    }else{
      const now = Date.now()/1000;
      const box = document.getElementById('msgs');
      const m = { epoch: now, fromId: myId||'me', toId: (payload.to===''?undefined:payload.to), text: text, scope: (activeConv==='^all'?'broadcast':'dm') };
      const dom = renderMsg(m);
      const meta = dom.querySelector('.meta');
      if(meta){ meta.textContent += ' · ✓ sent'; }
      box.appendChild(dom); box.scrollTop = box.scrollHeight;
      lastMsgSeen = now;
      input.value = '';

      clearTimeout(warnTimer);
      warnTimer = setTimeout(()=>{ showSendWarn(true); }, 15000);
    }
  }catch(e){ alert('Send error'); }
  finally{ btn.disabled = false; input.focus(); }
}

//// SETTINGS UI ////
async function loadSettings(){
  const s = await fetchJSON('/api/settings?t='+Date.now());
  cfg = s || cfg;
  document.getElementById('setTemp').value = cfg.units.temp || 'F';
  document.getElementById('setPress').value = cfg.units.pressure || 'hPa';
  document.getElementById('setAlt').value = cfg.units.alt || 'ft';
  document.getElementById('setTime').value = cfg.units.time || '12h';
  document.getElementById('setHideSelf').checked = !!cfg.msg.hideSelfAsRecipient;
  document.getElementById('setAck').checked = !!cfg.msg.requestAck;
  document.getElementById('setReadOnly').checked = !!cfg.msg.readOnly;
  document.getElementById('setScroll').checked = !!cfg.ui.rememberScroll;
}
async function saveSettings(){
  const body = {
    units: {
      temp: document.getElementById('setTemp').value,
      pressure: document.getElementById('setPress').value,
      alt: document.getElementById('setAlt').value,
      time: document.getElementById('setTime').value
    },
    msg: {
      hideSelfAsRecipient: document.getElementById('setHideSelf').checked,
      requestAck: document.getElementById('setAck').checked,
      readOnly: document.getElementById('setReadOnly').checked
    },
    ui: {
      rememberScroll: document.getElementById('setScroll').checked
    }
  };
  const r = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if(r.ok){
    cfg = await r.json(); // server returns merged settings
    const ok=document.getElementById('saveOk'); ok.classList.remove('hide'); setTimeout(()=>ok.classList.add('hide'), 1200);
    // Refresh views to reflect new units
    loadDashboard();
    if(activeConv) selectConv(activeConv);
  }else{
    alert('Failed to save settings');
  }
}

//// Tabs ////
const tabDash = document.getElementById('tabDash');
const tabChat = document.getElementById('tabChat');
const tabSettings = document.getElementById('tabSettings');
const viewDash = document.getElementById('dash');
const viewChat = document.getElementById('chat');
const viewSettings = document.getElementById('settings');
tabDash.onclick = ()=>{ chatVisible=false; clearInterval(convTimer);
  tabDash.classList.add('active'); tabChat.classList.remove('active'); tabSettings.classList.remove('active');
  viewDash.classList.remove('hide'); viewChat.classList.add('hide'); viewSettings.classList.add('hide');
};
tabChat.onclick = async ()=>{ chatVisible=true;
  tabChat.classList.add('active'); tabDash.classList.remove('active'); tabSettings.classList.remove('active');
  viewChat.classList.remove('hide'); viewDash.classList.add('hide'); viewSettings.classList.add('hide');
  await loadConversations(); clearInterval(convTimer); convTimer=setInterval(loadConversations, 3000);
};
tabSettings.onclick = async ()=>{ chatVisible=false; clearInterval(convTimer);
  tabSettings.classList.add('active'); tabDash.classList.remove('active'); tabChat.classList.remove('active');
  viewSettings.classList.remove('hide'); viewDash.classList.add('hide'); viewChat.classList.add('hide');
  await loadSettings();
};

//// Hooks ////
document.getElementById('sendBtn').onclick = sendCurrent;
document.getElementById('msgBox').addEventListener('keydown', (e)=>{ if(e.key==='Enter'){ sendCurrent(); } });
document.getElementById('saveBtn').onclick = saveSettings;

//// Schedules ////
setInterval(loadDashboard, 2000);
setInterval(pollActive, 1000);
(async ()=>{ const snap=await fetch('/api/nodes?t='+Date.now()); cfg=(await snap.json()).settings; })().then(loadDashboard);
</script>
"""

# --- HTTP Handler ---
class ApiHandler(BaseHTTPRequestHandler):
    server_version = "MeshDash/13-settings"

    def _hdr_json(self, code=200):
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Cache-Control","no-store, no-cache, must-revalidate")
        self.send_header("Pragma","no-cache")
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()

    def _read_json(self):
        length = int(self.headers.get("Content-Length","0") or "0")
        data = self.rfile.read(length) if length>0 else b""
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            return {}

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path == "/api/send":
                body = self._read_json()
                to = (body.get("to") or "").strip()
                conv = body.get("conv")  # optional pair conv id
                text = (body.get("text") or "").strip()
                ch = int(body.get("channelIndex", 0))
                # wantAck default from settings if not provided
                with settings_lock:
                    wantAck = bool(body.get("wantAck", settings["msg"].get("requestAck", True)))

                if not text:
                    self._hdr_json(400); self.wfile.write(json.dumps({"error":"empty text"}).encode("utf-8")); return
                if not _connected or g_iface is None:
                    self._hdr_json(503); self.wfile.write(json.dumps({"error":"not connected"}).encode("utf-8")); return

                # If a pair conversation is provided and my_id is known, force 'to' to be the other peer.
                peer_forced = None
                if conv and conv.startswith("pair:") and my_id:
                    pr = parse_pair_conv(conv)
                    if pr and (my_id in pr):
                        a,b = pr
                        peer_forced = b if my_id==a else a
                if peer_forced:
                    to = peer_forced

                # If still no 'to' and not broadcast, fail cleanly
                if (not to) and (conv != "^all"):
                    self._hdr_json(400); self.wfile.write(json.dumps({"error":"no destination"}).encode("utf-8")); return

                try:
                    ts = time.time()
                    if conv == "^all" or to == "^all":
                        g_iface.sendText(text, destinationId="^all", channelIndex=ch, wantAck=wantAck)
                        _record_recent_send("^all", text, ts)
                        msg = {"epoch": ts, "iso": datetime.now().isoformat(timespec="seconds"),
                               "fromId": my_id, "toId": "^all", "text": text, "scope": "broadcast"}
                        _append_msg("^all", msg)
                        self._hdr_json(200); self.wfile.write(json.dumps({"ok": True, "conv": "^all"}).encode("utf-8")); return
                    else:
                        g_iface.sendText(text, destinationId=to, channelIndex=ch, wantAck=wantAck)
                        _record_recent_send(to, text, ts)
                        conv_id = conv if conv else (pair_conv_id(my_id, to) if my_id else f"pair:{to}|{to}")
                        msg = {"epoch": ts, "iso": datetime.now().isoformat(timespec="seconds"),
                               "fromId": my_id or "me", "toId": to, "text": text, "scope": "dm"}
                        _append_msg(conv_id, msg)
                        self._hdr_json(200); self.wfile.write(json.dumps({"ok": True, "conv": conv_id}).encode("utf-8")); return
                except Exception as e:
                    self._hdr_json(500); self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8")); return

            if path == "/api/settings":
                body = self._read_json()
                with settings_lock:
                    # shallow merge for units/msg/ui sections we accept
                    for key in ("units","msg","ui","nodes","data","net"):
                        if key in body and isinstance(body[key], dict):
                            merged = settings.get(key, {}).copy()
                            merged.update(body[key])
                            settings[key] = merged
                    _save_settings(settings)
                    self._hdr_json(200); self.wfile.write(json.dumps(settings).encode("utf-8")); return

            self._hdr_json(404); self.wfile.write(json.dumps({"error":"not found"}).encode("utf-8"))
        except Exception as e:
            try: self._hdr_json(500); self.wfile.write(json.dumps({"error":str(e)}).encode("utf-8"))
            except Exception: pass

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Cache-Control","no-store")
                self.end_headers()
                self.wfile.write(DASHBOARD_PRETTY.encode("utf-8"))
                return
            if path == "/simple":
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Cache-Control","no-store")
                self.end_headers()
                self.wfile.write(DASHBOARD_SIMPLE.encode("utf-8"))
                return
            if path == "/api/health":
                self._hdr_json()
                body = {"status":"ok","connected":_connected,"node_count":len(nodes),"time":datetime.now().isoformat(timespec="seconds")}
                self.wfile.write(json.dumps(body).encode("utf-8")); return
            if path == "/api/nodes":
                self._hdr_json(); self.wfile.write(json.dumps(_nodes_json_snapshot()).encode("utf-8")); return
            if path.startswith("/api/nodes/"):
                node_id = path.split("/",3)[-1]
                snap = _nodes_json_snapshot()
                node = snap["nodes"].get(node_id)
                if node is None:
                    self._hdr_json(404); self.wfile.write(json.dumps({"error":"not found"}).encode("utf-8")); return
                self._hdr_json(); self.wfile.write(json.dumps(node).encode("utf-8")); return
            if path == "/api/history":
                qs = parse_qs(urlparse(self.path).query)
                n = None
                try:
                    if "n" in qs: n = max(1, min(1000, int(qs["n"][0])))
                except Exception: n = None
                self._hdr_json(); self.wfile.write(json.dumps(_history_json_snapshot(n)).encode("utf-8")); return
            if path == "/api/conversations":
                self._hdr_json(); self.wfile.write(json.dumps(_conversations_snapshot()).encode("utf-8")); return
            if path == "/api/messages":
                qs = parse_qs(urlparse(self.path).query)
                conv = qs.get("conv", ["^all"])[0]
                since = None
                if "since" in qs:
                    try: since = float(qs["since"][0])
                    except Exception: since = None
                n = None
                if "n" in qs:
                    try: n = max(1, min(5000, int(qs["n"][0])))
                    except Exception: n = None
                include_b = qs.get("include_broadcast", ["0"])[0] in ("1","true","True")
                self._hdr_json(); self.wfile.write(json.dumps(_messages_snapshot(conv, n, since, include_b)).encode("utf-8")); return
            if path == "/api/settings":
                with settings_lock:
                    self._hdr_json(); self.wfile.write(json.dumps(settings).encode("utf-8")); return

            self._hdr_json(404); self.wfile.write(json.dumps({"error":"not found"}).encode("utf-8"))
        except Exception as e:
            try: self._hdr_json(500); self.wfile.write(json.dumps({"error":str(e)}).encode("utf-8"))
            except Exception: pass

    def log_message(self, fmt, *args): pass

def start_api_server():
    httpd = HTTPServer((API_HOST, API_PORT), ApiHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    say(f"[API] Live on http://{API_HOST}:{API_PORT} (pretty=/ , simple=/simple , settings=/api/settings)")

# --- main loop w/ auto-reconnect ---
def main():
    pub.subscribe(on_receive,"meshtastic.receive")
    pub.subscribe(on_connection,"meshtastic.connection.established")
    pub.subscribe(on_connection_lost,"meshtastic.connection.lost")
    pub.subscribe(on_node_updated,"meshtastic.node.updated")
    start_api_server()
    iface=None; backoff=1.0; last_table=0.0
    say(f"Opening TCP {HOST}:4403 …")
    try:
        while True:
            if not _connected or iface is None:
                if iface is not None:
                    try: iface.close()
                    except Exception: pass
                    iface=None
                try:
                    iface=meshtastic.tcp_interface.TCPInterface(hostname=HOST); backoff=1.0
                except Exception as e:
                    say(f"[Connect] failed: {e}. Retrying in {int(backoff)}s…")
                    time.sleep(backoff); backoff=min(30.0, backoff*2.0)
            try:
                typ,payload=outq.get(timeout=0.5)
                if typ=="msg": print(payload, flush=True)
                elif typ=="packet": handle_packet(payload)
            except queue.Empty:
                pass
            now=time.time()
            if now-last_table>=REFRESH_EVERY:
                print("\n"+render_table(), flush=True); last_table=now
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if iface is not None: iface.close()
        except Exception: pass

if __name__=="__main__":
    main()



