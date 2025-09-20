# mesh_listen_v15_settings.py
import time, logging, queue, csv, os, json, threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as HTTPServer
from urllib.parse import urlparse, parse_qs
from collections import deque, defaultdict
from pubsub import pub
import meshtastic.tcp_interface  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# --- Env helpers ---
def get_env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key, "").lower()
    if val in ("true", "1", "yes", "on"): return True
    if val in ("false", "0", "no", "off"): return False
    return default

def get_env_float(key: str, default: float) -> float:
    try: return float(os.getenv(key, str(default)))
    except (ValueError, TypeError): return default

def get_env_int(key: str, default: int) -> int:
    try: return int(os.getenv(key, str(default)))
    except (ValueError, TypeError): return default

def as_bool(x, default=False):
    if isinstance(x, bool): return x
    if x is None: return default
    s = str(x).strip().lower()
    if s in ("true","1","yes","on"): return True
    if s in ("false","0","no","off"): return False
    return default

def as_int(x, default=0):
    try: return int(x)
    except Exception: return default

def as_float(x, default=0.0):
    try: return float(x)
    except Exception: return default

# --- Config (env-defaulted) ---
HOST = os.getenv("MESH_HOST", "192.168.0.91")
REFRESH_EVERY = get_env_float("REFRESH_EVERY", 5.0)
SHOW_UNKNOWN = get_env_bool("SHOW_UNKNOWN", True)
SHOW_PER_PACKET = get_env_bool("SHOW_PER_PACKET", True)
LOG_TO_CSV = get_env_bool("LOG_TO_CSV", True)
LOG_PREFIX = os.getenv("LOG_PREFIX", "meshtastic_log")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = get_env_int("API_PORT", 8080)

HISTORY_MAXLEN = get_env_int("HISTORY_MAXLEN", 300)
HISTORY_SAMPLE_SECS = get_env_float("HISTORY_SAMPLE_SECS", 2.0)
MAX_MSGS_PER_CONV = get_env_int("MAX_MSGS_PER_CONV", 2000)

# --- Runtime Settings (persistent) ---
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "settings.json")
settings_lock = threading.Lock()
DEFAULT_SETTINGS = {
    "unit_temp": (os.getenv("UNIT_TEMP") or "F").upper(),     # "F" or "C"
    "want_ack_default": get_env_bool("WANT_ACK_DEFAULT", True),
    "default_channel_index": get_env_int("DEFAULT_CHANNEL_INDEX", 0),
    "history_maxlen": HISTORY_MAXLEN,
    "history_sample_secs": HISTORY_SAMPLE_SECS,
    "log_to_csv": LOG_TO_CSV,
    "show_unknown": SHOW_UNKNOWN,
    "show_per_packet": SHOW_PER_PACKET,
}
settings = DEFAULT_SETTINGS.copy()

# --- State ---
outq: "queue.Queue[tuple[str, object]]" = queue.Queue()
def say(msg: str): outq.put(("msg", msg))
def emit_packet(packet: dict): outq.put(("packet", packet))

nodes_lock = threading.Lock()
nodes: dict[str, dict] = {}
_connected = False

node_names: dict[str, str] = {}
g_iface = None
my_id: str | None = None

hist_lock = threading.Lock()
history: dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAXLEN))
last_hist_time: dict[str, float] = {}

msg_lock = threading.Lock()
messages: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_MSGS_PER_CONV))
last_msg_ts: dict[str, float] = {}

seen_pkt_ids_lock = threading.Lock()
seen_pkt_ids: deque[str] = deque(maxlen=10000)
seen_pkt_set: set[str] = set()

recent_sends_lock = threading.Lock()
recent_sends: deque[tuple[str,str,float]] = deque(maxlen=512)
RECENT_SEND_SUPPRESS_SECS = 5.0

# --- Settings helpers ---
def _resize_history(new_max: int):
    global HISTORY_MAXLEN
    with hist_lock:
        for nid, dq in list(history.items()):
            history[nid] = deque(dq, maxlen=new_max)
    HISTORY_MAXLEN = new_max

def _apply_settings_locked():
    global LOG_TO_CSV, SHOW_UNKNOWN, SHOW_PER_PACKET, HISTORY_SAMPLE_SECS
    LOG_TO_CSV = bool(settings.get("log_to_csv", LOG_TO_CSV))
    SHOW_UNKNOWN = bool(settings.get("show_unknown", SHOW_UNKNOWN))
    SHOW_PER_PACKET = bool(settings.get("show_per_packet", SHOW_PER_PACKET))
    new_ml = as_int(settings.get("history_maxlen", HISTORY_MAXLEN), HISTORY_MAXLEN)
    if new_ml != HISTORY_MAXLEN and new_ml > 0:
        _resize_history(new_ml)
    HISTORY_SAMPLE_SECS = as_float(settings.get("history_sample_secs", HISTORY_SAMPLE_SECS), HISTORY_SAMPLE_SECS)

def _load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            with settings_lock:
                for k in DEFAULT_SETTINGS.keys():
                    if k in data:
                        settings[k] = data[k]
                _apply_settings_locked()
    except Exception as e:
        say(f"[Settings] load failed: {e}")

def _save_settings():
    try:
        with settings_lock:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
    except Exception as e:
        say(f"[Settings] save failed: {e}")

# --- mesh helpers ---
def _refresh_names_from(iface):
    global my_id
    try:
        mi = getattr(iface, "myInfo", None)
        if isinstance(mi, dict):
            u = mi.get("user") or {}
            if u.get("id"): my_id = u["id"]
            nm = (u.get("longName") or u.get("shortName"))
            if my_id and nm: node_names[my_id] = nm

        nd = getattr(iface, "nodes", None) or {}
        for entry in nd.values():
            u = (entry.get("user") or {})
            fid = u.get("id")
            if not fid: continue
            name = u.get("longName") or u.get("shortName")
            if name: node_names[fid] = name
            if my_id is None and u.get("isLocal"): my_id = fid
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

def pair_conv_id(a: str, b: str) -> str:
    if not a or not b: return (a or b or "^all")
    a2, b2 = (a, b) if a <= b else (b, a)
    return f"pair:{a2}|{b2}"

def parse_pair_conv(cid: str):
    if not cid.startswith("pair:"): return None
    try:
        body = cid.split(":",1)[1]
        a, b = body.split("|",1)
        return a, b
    except Exception:
        return None

def disp_name(node_id: str | None) -> str:
    if not node_id: return "unknown"
    return node_names.get(node_id) or node_id

def _record_recent_send(to_id: str, text: str, ts: float):
    with recent_sends_lock:
        recent_sends.append((to_id, text, ts))

def _is_recent_send(from_id: str | None, to_id: str | None, text: str | None, now: float) -> bool:
    if not (from_id and to_id and text and my_id and from_id == my_id): return False
    with recent_sends_lock:
        for (tgt, txt, ts) in reversed(recent_sends):
            if tgt == to_id and txt == text and (now - ts) <= RECENT_SEND_SUPPRESS_SECS:
                return True
    return False

def _pkt_seen_once(pkt_id: str | None) -> bool:
    if not pkt_id: return False
    with seen_pkt_ids_lock:
        if pkt_id in seen_pkt_set: return True
        seen_pkt_set.add(pkt_id)
        seen_pkt_ids.append(pkt_id)
        if len(seen_pkt_ids) == seen_pkt_ids.maxlen:
            seen_pkt_set.clear(); seen_pkt_set.update(seen_pkt_ids)
    return False

# --- pubsub (bg thread) ---
def on_receive(packet, interface=None): emit_packet(packet)

def on_connection(interface=None):
    global _connected, g_iface
    _connected = True; g_iface = interface
    _refresh_names_from(interface)
    say("[Connection] Established.")
    t0 = time.time()
    while (my_id is None) and (time.time()-t0 < 5.0):
        _refresh_names_from(interface); time.sleep(0.2)

def on_connection_lost(interface=None):
    global _connected
    _connected = False
    say("[Connection] Lost.")

def on_node_updated(node=None, interface=None, **_):
    if interface: _refresh_names_from(interface)

# --- history helpers (store °F internally) ---
def _record_history(node_id: str, rec: dict, now: float):
    lt = last_hist_time.get(node_id, 0.0)
    if now - lt < HISTORY_SAMPLE_SECS: return
    last_hist_time[node_id] = now

    tf = rec.get("temp_f")
    if tf is None and rec.get("temp_c") is not None:
        try: tf = float(rec["temp_c"]) * 9/5 + 32
        except Exception: tf = None

    pt = {
        "t": now,
        "batt": rec.get("batt"),
        "temp": tf,        # Fahrenheit in history
        "press": rec.get("press_hpa"),
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

# --- packet processing ---
def handle_packet(pkt: dict):
    d = (pkt.get("decoded") or {})
    port = d.get("portnum")
    frm, to = pkt.get("fromId"), pkt.get("toId")
    rssi, snr = pkt.get("rxRssi"), pkt.get("rxSnr")
    now = time.time()

    global my_id
    if my_id is None and port == "TEXT_MESSAGE_APP" and to and to != "^all":
        my_id = to

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
                "name": friendly, "updated":None
            }
        rec = nodes[frm]
        rec["to"] = to
        if rssi is not None: rec["rssi"] = rssi
        if snr  is not None: rec["snr"]  = snr
        if friendly and not rec.get("name"): rec["name"] = friendly

    if settings.get("show_per_packet", True):
        if port == "TEXT_MESSAGE_APP":
            say(f"[TEXT] {frm} → {to} | rssi={rssi} snr={snr} | {d.get('text')}")
        elif port == "POSITION_APP":
            p = d.get("position") or {}
            say(f"[GPS]  {frm} → {to} | rssi={rssi} snr={snr} | lat={p.get('latitude')} lon={p.get('longitude')} alt={p.get('altitude')}")
        elif port not in ("TELEMETRY_APP",) and settings.get("show_unknown", True):
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
        if not _is_recent_send(frm, to, txt, now):
            conv = "^all" if scope=="broadcast" else pair_conv_id(frm or "", to or "")
            msg = {"epoch": now,"iso": datetime.now().isoformat(timespec="seconds"),
                   "fromId": frm,"toId": to,"text": txt,"rssi": rssi,"snr": snr,"scope": scope}
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
        t  = d.get("telemetry") or {}; dm = t.get("deviceMetrics") or {}; em = t.get("environmentMetrics") or {}
        with nodes_lock:
            if "batteryLevel" in dm: nodes[frm]["batt"] = dm.get("batteryLevel")
            if "voltage"      in dm: nodes[frm]["voltage"] = dm.get("voltage")
            if em:
                c  = em.get("temperature"); rh = em.get("relativeHumidity"); pa = _pressure_to_hpa(em.get("barometricPressure"))
                if c is not None:
                    try:
                        c = float(c)
                        nodes[frm]["temp_c"] = c
                        nodes[frm]["temp_f"] = c*9/5+32
                    except Exception: pass
                if rh is not None: nodes[frm]["rh"] = rh
                if pa is not None: nodes[frm]["press_hpa"] = pa
            nodes[frm]["updated"] = now
            rec = nodes[frm]
        _csv_write(base | {"battery": rec.get("batt"),"voltage": rec.get("voltage"),
                           "temp_c": rec.get("temp_c"),"temp_f": rec.get("temp_f"),
                           "humidity": rec.get("rh"),"pressure_hpa": rec.get("press_hpa")})
        changed = True
    else:
        if settings.get("show_unknown", True): _csv_write(base)

    if changed:
        _record_history(frm, rec, now)

# --- console table (°F shown) ---
def render_table():
    with nodes_lock:
        snap = list(sorted(nodes.items(), key=lambda kv: kv[1].get("updated") or 0, reverse=True))
    rows = []
    rows.append(("Node".ljust(16)+" | Batt% | V | T(°F) | P(hPa) | RH% | RSSI | SNR | Lat | Lon | Alt | " +
                 "Last Text".ljust(24)+" | Updated"))
    rows.append("-"*119)
    for nid, rec in snap:
        display = (rec.get("name") or nid)
        rows.append(" | ".join([
            str(display).ljust(16),
            _fmt(rec.get("batt"), "{:.0f}"), _fmt(rec.get("voltage")),
            _fmt(rec.get("temp_f")), _fmt(rec.get("press_hpa")),
            _fmt(rec.get("rh"), "{:.1f}"), _fmt(rec.get("rssi"), "{:.0f}"), _fmt(rec.get("snr")),
            _fmt_latlon(rec.get("lat")), _fmt_latlon(rec.get("lon")), _fmt_alt(rec.get("alt")),
            (rec.get("text") or "-")[:24].ljust(24), _fmt_time(rec.get("updated"))
        ]))
    return "\n".join(rows)

# --- JSON snapshots ---
def _nodes_json_snapshot():
    with nodes_lock, settings_lock:
        now_iso = datetime.now().isoformat(timespec="seconds")
        out = {"connected": _connected, "server_time": now_iso, "nodes": {}, "my_id": my_id,
               "my_name": (node_names.get(my_id) if my_id else None), "names": node_names,
               "settings": {k: settings[k] for k in settings}}  # include settings for client
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

def _conversations_snapshot():
    with msg_lock: conv_keys = set(messages.keys())
    known_ids = set(node_names.keys())
    with nodes_lock: known_ids.update(nodes.keys())
    if my_id:
        for nid in [x for x in known_ids if x != my_id]:
            conv_keys.add(pair_conv_id(my_id, nid))

    out = []
    for cid in conv_keys or []:
        if cid == "^all":
            nm = "Broadcast (^all)"; last_t = last_msg_ts.get(cid, 0.0)
        else:
            pair = parse_pair_conv(cid)
            if not pair:
                nm = cid; last_t = last_msg_ts.get(cid, 0.0)
            else:
                a,b = pair
                if my_id and (my_id==a or my_id==b):
                    peer = b if my_id==a else a
                    nm = disp_name(peer); last_t = last_msg_ts.get(cid, 0.0)
                    if last_t == 0.0:
                        with nodes_lock: last_t = (nodes.get(peer) or {}).get("updated") or 0.0
                else:
                    nm = f"{disp_name(a)} \u2194 {disp_name(b)}"; last_t = last_msg_ts.get(cid, 0.0)
        out.append({"id": cid, "name": nm, "last_epoch": last_t})

    out.sort(key=lambda x: (x["id"]!="^all", -x["last_epoch"], x["name"].lower()))
    return out

def _messages_snapshot(conv_id: str, limit: int | None = None, since: float | None = None, include_broadcast: bool = False):
    with msg_lock: base = list(messages.get(conv_id, deque()))
    pair = parse_pair_conv(conv_id)
    if include_broadcast and pair:
        a, b = pair
        with msg_lock: bcast = list(messages.get("^all", deque()))
        for m in bcast:
            if m.get("fromId") in (a, b):
                base.append(m | {"scope": "broadcast"})
    base.sort(key=lambda m: m.get("epoch", 0))
    if since is not None: base = [m for m in base if m.get("epoch", 0) > since]
    if limit is not None: base = base[-limit:]
    return base

# --- HTML UI (simple, pretty, and settings page) ---
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
async function fetchNodes(){ const r=await fetch('/api/nodes?t='+Date.now(),{cache:'no-store'}); return await r.json(); }
function asTable(snap){
  const unit=(snap.settings&&snap.settings.unit_temp)||'F';
  const header = ["Node".padEnd(16),"Batt%","V","T(°"+unit+")","P(hPa)","RH%","RSSI","SNR","Lat","Lon","Alt","Last Text".padEnd(24),"Updated"].join(" | ");
  const bar = "-".repeat(header.length);
  const rows=[header,bar];
  const items=Object.entries(snap.nodes).sort((a,b)=>(b[1].updated_epoch||0)-(a[1].updated_epoch||0));
  for(const [id,v] of items){
    const name=(v.name&&v.name.trim().length)?v.name:id;
    const t = (unit==='F') ? v.temp_f : (v.temp_c ?? (v.temp_f!=null? ((Number(v.temp_f)-32)*5/9) : null));
    const r = [
      String(name).padEnd(16),
      v.batt==null?"-":Number(v.batt).toFixed(0),
      v.voltage==null?"-":Number(v.voltage).toFixed(2),
      t==null?"-":Number(t).toFixed(2),
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
.nav button, .nav a{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:999px;padding:8px 14px;cursor:pointer;text-decoration:none;display:inline-block}
.nav .active{outline:2px solid var(--accent)}
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
</style>

<div class="wrap">
  <h1>Meshtastic · Dashboard & Chat</h1>
  <div class="nav">
    <button id="tabDash" class="active">Dashboard</button>
    <button id="tabChat">Messages</button>
    <a href="/api/settings" title="Settings">Settings</a>
  </div>
  <div class="meta" id="meta">loading…</div>

  <!-- DASHBOARD -->
  <div id="dash">
    <div class="grid" id="cards"></div>
    <div class="table">
      <table id="tbl"><thead><tr>
        <th>Node</th><th>Batt%</th><th>V</th><th id="thTemp">T(°F)</th><th>P(hPa)</th><th>RH%</th><th>RSSI</th><th>SNR</th><th>Lat</th><th>Lon</th><th>Alt</th><th>Updated</th>
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
</div>

<script>
let charts = {};
let activeConv = null;
let lastMsgSeen = 0;
let myId=null, myName=null, names={};
let sendTarget=null, convTimer=null, warnTimer=null;
let SET = {unit_temp:'F', default_channel_index:0, want_ack_default:true};

function battClass(b){ if(b==null) return ''; if(b<=15) return 'bad'; if(b<=30) return 'warn'; return 'ok'; }
function battPct(b){ return (b==null)?0:Math.max(0,Math.min(100,Number(b)||0)); }
function nice(v,d=2){ if(v==null||v===undefined) return '-'; const n=Number(v); return isFinite(n)?n.toFixed(d):v; }
function timeStr(iso){ return iso ? new Date(iso).toLocaleTimeString() : '-'; }
function dispName(id){ if(!id) return 'unknown'; if(id===myId) return myName || (names[id]||id); return names[id] || id; }
async function fetchJSON(url, opts){ const r=await fetch(url,opts||{cache:'no-store'}); return await r.json(); }

async function loadDashboard(){
  const sx = window.scrollX || 0, sy = window.scrollY || document.documentElement.scrollTop || 0;
  const [snap, hist] = await Promise.all([
    fetchJSON('/api/nodes?t='+Date.now()),
    fetchJSON('/api/history?n=150&t='+Date.now())
  ]);
  myId = snap.my_id || null; myName = snap.my_name || null; names = snap.names || {};
  SET = Object.assign(SET, (snap.settings||{}));
  const unit = (SET.unit_temp||'F').toUpperCase();
  document.getElementById('thTemp').textContent = 'T(°'+unit+')';

  document.getElementById('meta').textContent =
    `connected=${snap.connected} | server=${snap.server_time} | nodes=${Object.keys(snap.nodes).length}`;

  const cards = document.getElementById('cards');
  for (const k in charts){ try{ charts[k].destroy(); }catch(e){} delete charts[k]; }
  cards.innerHTML = '';

  const entries = Object.entries(snap.nodes).sort((a,b)=>(b[1].updated_epoch||0)-(a[1].updated_epoch||0));
  for(const [id, v] of entries){
    const name = (v.name&&v.name.trim().length)?v.name:id;
    const h = hist[id] || [];
    const cid = 'c_'+id.replace(/[^a-zA-Z0-9_]/g,'_');
    const lat = (v.lat==null)? null : Number(v.lat).toFixed(5);
    const lon = (v.lon==null)? null : Number(v.lon).toFixed(5);
    const alt = (v.alt==null)? null : Number(v.alt).toFixed(0);
    const mapLink = (lat && lon) ? `<a href="https://maps.google.com/?q=${lat},${lon}" target="_blank" rel="noopener">map</a>` : '';

    // temperature per unit
    const tDisp = (unit==='F')
      ? (v.temp_f!=null? nice(v.temp_f,1) : (v.temp_c!=null? nice((Number(v.temp_c)*9/5+32),1) : '-'))
      : (v.temp_c!=null? nice(v.temp_c,1) : (v.temp_f!=null? nice(((Number(v.temp_f)-32)*5/9),1) : '-'));
    const tLabel = unit==='F' ? '°F' : '°C';

    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <h3>${name}</h3>
      <div class="kv">
        <div>Battery<b class="${battClass(v.batt)}">${nice(v.batt,0)}%</b></div>
        <div>Voltage<b>${nice(v.voltage,2)} V</b></div>
        <div>Temp<b>${tDisp} ${tLabel}</b></div>
        <div>Pressure<b>${nice(v.press_hpa,1)} hPa</b></div>
        <div>RH<b>${nice(v.rh,1)} %</b></div>
        <div>RSSI<b>${nice(v.rssi,0)} dBm</b></div>
        <div>SNR<b>${nice(v.snr,2)} dB</b></div>
        <div>GPS<b>${lat && lon ? (lat+', '+lon) : '-'}</b>${mapLink}</div>
        <div>Alt<b>${alt ? (alt+' m') : '-'}</b></div>
        <div>Updated<b>${timeStr(v.updated_iso)}</b></div>
      </div>
      <div class="bar"><span style="width:${battPct(v.batt)}%"></span></div>
      <div class="small">${(v.text||'-')}</div>
      <canvas id="${cid}"></canvas>
    `;
    cards.appendChild(card);

    const ctx = card.querySelector('canvas').getContext('2d');
    const labels = h.map(p => new Date(p.t*1000).toLocaleTimeString());
    const dsBatt = h.map(p => p.batt);
    const dsTempF = h.map(p => p.temp);
    const dsTemp = (unit==='F') ? dsTempF
      : dsTempF.map(x => (x==null? null : ((Number(x)-32)*5/9)));
    const tempSeriesLabel = (unit==='F') ? 'Temp °F' : 'Temp °C';
    charts[cid]=new Chart(ctx, {
      type:'line',
      data:{labels, datasets:[
        {label:'Battery %', data: dsBatt, yAxisID:'y1', tension:.3},
        {label: tempSeriesLabel, data: dsTemp, yAxisID:'y2', tension:.3}
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
    const t = (unit==='F')
      ? (v.temp_f!=null? Number(v.temp_f).toFixed(1) : (v.temp_c!=null? (Number(v.temp_c)*9/5+32).toFixed(1) : '-'))
      : (v.temp_c!=null? Number(v.temp_c).toFixed(1) : (v.temp_f!=null? ((Number(v.temp_f)-32)*5/9).toFixed(1) : '-'));
    const tr=document.createElement('tr');
    const cells=[name, nice(v.batt,0), nice(v.voltage,2), t,
      nice(v.press_hpa,1), nice(v.rh,1), nice(v.rssi,0), nice(v.snr,2),
      v.lat==null?'-':Number(v.lat).toFixed(5),
      v.lon==null?'-':Number(v.lon).toFixed(5),
      v.alt==null?'-':Number(v.alt).toFixed(0),
      timeStr(v.updated_iso)];
    cells.forEach((c,idx)=>{
      const td=document.createElement('td'); td.textContent=c;
      if(idx===1){ if(v.batt!=null){ const b=Number(v.batt); if(b<=15) td.className='bad'; else if(b<=30) td.className='warn'; else td.className='ok'; } }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }

  requestAnimationFrame(()=>{ window.scrollTo(sx, sy); });
}

// --- Chat ---
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
  div.innerHTML = `${tag}${m.text}<div class="meta">${who} · ${new Date(m.epoch*1000).toLocaleTimeString()}${m.rssi!=null? ' · rssi '+m.rssi:''}</div>`;
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
  if(myId && (myId===a || myId===b)){ const peer = (myId===a)? b : a; return dispName(peer); }
  return `${dispName(a)} \u2194 ${dispName(b)}`;
}
let chatVisible=false;
async function selectConv(id){
  activeConv = id; lastMsgSeen = 0;
  const snap = await fetchJSON('/api/nodes?t='+Date.now());
  myId = snap.my_id || null; myName = snap.my_name || null; names = snap.names || {};
  SET = Object.assign(SET, (snap.settings||{}));
  const includeB = (id !== '^all');
  const list = await fetchMessages(id, null, includeB);

  function peerFromConv(){
    if(id==='^all') return '^all';
    const pair = id.startsWith('pair:') ? id.slice(5).split('|') : null;
    if(!pair) return null;
    const [a,b] = pair;
    if(myId && (myId===a || myId===b)) return (myId===a)? b : a;
    if(list && list.length){ const last = list[list.length-1]; if(last && last.fromId && last.fromId!=='^all') return last.fromId; }
    return b;
  }
  sendTarget = peerFromConv();

  const toLabel = (id==='^all') ? 'Broadcast' : (sendTarget ? dispName(sendTarget) : '(selecting peer…)');
  document.getElementById('convTitle').textContent = headerFor(id);
  document.getElementById('toWrap').textContent = (id==='^all') ? 'To: Broadcast' : ('To: ' + toLabel);
  document.getElementById('meInfo').textContent = myId ? `me=${dispName(myId)}` : '';

  document.getElementById('sendBtn').disabled = false;
  document.getElementById('msgBox').disabled = false;

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
  const input = document.getElementById('msgBox');
  const btn = document.getElementById('sendBtn');
  const text = input.value.trim();
  if(!text){ return; }
  if(!activeConv){ alert('Pick a conversation'); return; }
  btn.disabled = true; showSendWarn(false);
  try{
    const payload = (activeConv==='^all')
      ? { to: '^all', text, channelIndex: (SET.default_channel_index??0), wantAck: (SET.want_ack_default??true) }
      : { conv: activeConv, to: (sendTarget||''), text, channelIndex: (SET.default_channel_index??0), wantAck: (SET.want_ack_default??true) };
    const r = await fetch('/api/send', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const res = await r.json();
    if(!r.ok){ alert('Send failed: '+(res.error||r.status)); }
    else{
      const now = Date.now()/1000;
      const box = document.getElementById('msgs');
      const m = { epoch: now, fromId: myId||'me', toId: (payload.to===''?undefined:payload.to), text: text, scope: (activeConv==='^all'?'broadcast':'dm') };
      const dom = renderMsg(m);
      const meta = dom.querySelector('.meta'); if(meta){ meta.textContent += ' · ✓ sent'; }
      box.appendChild(dom); box.scrollTop = box.scrollHeight;
      lastMsgSeen = now; input.value = '';
      clearTimeout(warnTimer); warnTimer = setTimeout(()=>{ showSendWarn(true); }, 15000);
    }
  }catch(e){ alert('Send error'); }
  finally{ btn.disabled = false; input.focus(); }
}
const tabDash = document.getElementById('tabDash');
const tabChat = document.getElementById('tabChat');
const viewDash = document.getElementById('dash');
const viewChat = document.getElementById('chat');
tabDash.onclick = ()=>{ chatVisible=false; clearInterval(convTimer); tabDash.classList.add('active'); tabChat.classList.remove('active'); viewDash.classList.remove('hide'); viewChat.classList.add('hide'); };
tabChat.onclick = async ()=>{ chatVisible=true; tabChat.classList.add('active'); tabDash.classList.remove('active'); viewChat.classList.remove('hide'); viewDash.classList.add('hide'); await loadConversations(); clearInterval(convTimer); convTimer=setInterval(loadConversations, 3000); };
document.getElementById('sendBtn').onclick = sendCurrent;
document.getElementById('msgBox').addEventListener('keydown', (e)=>{ if(e.key==='Enter'){ sendCurrent(); } });
setInterval(loadDashboard, 2000);
setInterval(pollActive, 1000);
loadDashboard();
</script>
"""

SETTINGS_HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"/>
<title>Meshtastic Settings</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
body{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;background:#0b1020;color:#e8efff;margin:0}
.wrap{max-width:800px;margin:0 auto;padding:20px}
.card{background:#121a2e;border:1px solid #1f2b47;border-radius:14px;padding:16px}
h1{margin:0 0 12px}
label{display:block;margin:10px 0 4px;color:#9fb1cc}
input[type=text],input[type=number],select{width:100%;padding:10px;border-radius:10px;border:1px solid #1f2b47;background:#0b1020;color:#e8efff}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.btns{margin-top:14px;display:flex;gap:8px}
button, a.btn{background:#6fb6ff;border:none;color:#08111f;padding:10px 14px;border-radius:10px;cursor:pointer;text-decoration:none}
.small{color:#8391a7;margin-top:8px}
</style>
<div class="wrap">
  <h1>Settings</h1>
  <div class="card">
    <div class="row">
      <div>
        <label>Temperature Unit</label>
        <select id="unit">
          <option value="F">Fahrenheit (°F)</option>
          <option value="C">Celsius (°C)</option>
        </select>
      </div>
      <div>
        <label>Default Channel Index</label>
        <input id="ch" type="number" step="1" min="0" value="0"/>
      </div>
      <div>
        <label>Want ACK by default</label>
        <select id="ack">
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      </div>
      <div>
        <label>Log to CSV</label>
        <select id="csv">
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      </div>
      <div>
        <label>Show Unknown Packets in CSV</label>
        <select id="unk">
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      </div>
      <div>
        <label>Verbose Per-Packet Console</label>
        <select id="perpkt">
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      </div>
      <div>
        <label>History Max Points</label>
        <input id="hmax" type="number" step="1" min="10" value="300"/>
      </div>
      <div>
        <label>History Sample Seconds</label>
        <input id="hsecs" type="number" step="0.1" min="0.2" value="2.0"/>
      </div>
    </div>
    <div class="btns">
      <button id="save">Save</button>
      <a class="btn" href="/">← Back</a>
    </div>
    <div class="small" id="msg"></div>
  </div>
</div>
<script>
async function load(){
  const r = await fetch('/api/settings/get?t='+Date.now());
  const s = await r.json();
  document.getElementById('unit').value = (s.unit_temp||'F').toUpperCase();
  document.getElementById('ch').value = s.default_channel_index ?? 0;
  document.getElementById('ack').value = String(s.want_ack_default ?? true);
  document.getElementById('csv').value = String(s.log_to_csv ?? true);
  document.getElementById('unk').value = String(s.show_unknown ?? true);
  document.getElementById('perpkt').value = String(s.show_per_packet ?? true);
  document.getElementById('hmax').value = s.history_maxlen ?? 300;
  document.getElementById('hsecs').value = s.history_sample_secs ?? 2.0;
}
async function save(){
  const body = {
    unit_temp: document.getElementById('unit').value,
    default_channel_index: Number(document.getElementById('ch').value||0),
    want_ack_default: (document.getElementById('ack').value==='true'),
    log_to_csv: (document.getElementById('csv').value==='true'),
    show_unknown: (document.getElementById('unk').value==='true'),
    show_per_packet: (document.getElementById('perpkt').value==='true'),
    history_maxlen: Number(document.getElementById('hmax').value||300),
    history_sample_secs: Number(document.getElementById('hsecs').value||2.0)
  };
  const r = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const res = await r.json();
  const msg = document.getElementById('msg');
  msg.textContent = r.ok ? 'Saved.' : ('Error: '+(res.error||r.status));
  if(r.ok){ setTimeout(()=>{ msg.textContent=''; }, 1500); }
}
document.getElementById('save').onclick = save;
load();
</script>
"""

# --- HTTP Handler ---
class ApiHandler(BaseHTTPRequestHandler):
    server_version = "MeshDash/14-settings"

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
        try: return json.loads(data.decode("utf-8"))
        except Exception: return {}

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path == "/api/send":
                body = self._read_json()
                to = (body.get("to") or "").strip()
                conv = body.get("conv")
                text = (body.get("text") or "").strip()
                # default to settings if missing
                with settings_lock:
                    ch_default = as_int(settings.get("default_channel_index", 0), 0)
                    ack_default = as_bool(settings.get("want_ack_default", True), True)
                ch = as_int(body.get("channelIndex", ch_default), ch_default)
                wantAck = as_bool(body.get("wantAck", ack_default), ack_default)

                if not text:
                    self._hdr_json(400); self.wfile.write(json.dumps({"error":"empty text"}).encode("utf-8")); return
                if not _connected or g_iface is None:
                    self._hdr_json(503); self.wfile.write(json.dumps({"error":"not connected"}).encode("utf-8")); return

                peer_forced = None
                if conv and conv.startswith("pair:") and my_id:
                    pr = parse_pair_conv(conv)
                    if pr and (my_id in pr):
                        a,b = pr; peer_forced = b if my_id==a else a
                if peer_forced: to = peer_forced

                if (not to) and (conv != "^all"):
                    self._hdr_json(400); self.wfile.write(json.dumps({"error":"no destination"}).encode("utf-8")); return

                try:
                    ts = time.time()
                    if conv == "^all" or to == "^all":
                        g_iface.sendText(text, destinationId="^all", channelIndex=ch, wantAck=wantAck)
                        _record_recent_send("^all", text, ts)
                        msg = {"epoch": ts,"iso": datetime.now().isoformat(timespec="seconds"),
                               "fromId": my_id, "toId": "^all", "text": text, "scope": "broadcast"}
                        _append_msg("^all", msg)
                        self._hdr_json(200); self.wfile.write(json.dumps({"ok": True, "conv": "^all"}).encode("utf-8")); return
                    else:
                        g_iface.sendText(text, destinationId=to, channelIndex=ch, wantAck=wantAck)
                        _record_recent_send(to, text, ts)
                        conv_id = conv if conv else (pair_conv_id(my_id, to) if my_id else f"pair:{to}|{to}")
                        msg = {"epoch": ts,"iso": datetime.now().isoformat(timespec="seconds"),
                               "fromId": my_id or "me", "toId": to, "text": text, "scope": "dm"}
                        _append_msg(conv_id, msg)
                        self._hdr_json(200); self.wfile.write(json.dumps({"ok": True, "conv": conv_id}).encode("utf-8")); return
                except Exception as e:
                    self._hdr_json(500); self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8")); return

            if path == "/api/settings":
                body = self._read_json()
                with settings_lock:
                    for key in DEFAULT_SETTINGS.keys():
                        if key in body:
                            if key in ("log_to_csv","show_unknown","show_per_packet","want_ack_default"):
                                settings[key] = as_bool(body[key], DEFAULT_SETTINGS[key])
                            elif key in ("default_channel_index","history_maxlen"):
                                settings[key] = as_int(body[key], DEFAULT_SETTINGS[key])
                            elif key in ("history_sample_secs",):
                                settings[key] = as_float(body[key], DEFAULT_SETTINGS[key])
                            elif key in ("unit_temp",):
                                uv = str(body[key]).upper()
                                settings[key] = "C" if uv=="C" else "F"
                    _apply_settings_locked()
                    _save_settings()
                self._hdr_json(200); self.wfile.write(json.dumps({"ok": True, "settings": settings}).encode("utf-8")); return

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
                self.wfile.write(DASHBOARD_PRETTY.encode("utf-8")); return
            if path == "/simple":
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Cache-Control","no-store")
                self.end_headers()
                self.wfile.write(DASHBOARD_SIMPLE.encode("utf-8")); return
            if path == "/api/settings":
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Cache-Control","no-store")
                self.end_headers()
                self.wfile.write(SETTINGS_HTML.encode("utf-8")); return
            if path == "/api/settings/get":
                self._hdr_json(); 
                with settings_lock:
                    self.wfile.write(json.dumps(settings).encode("utf-8")); 
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

            self._hdr_json(404); self.wfile.write(json.dumps({"error":"not found"}).encode("utf-8"))
        except Exception as e:
            try: self._hdr_json(500); self.wfile.write(json.dumps({"error":str(e)}).encode("utf-8"))
            except Exception: pass

    def log_message(self, fmt, *args): pass

def start_api_server():
    httpd = HTTPServer((API_HOST, API_PORT), ApiHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True); t.start()
    say(f"[API] Live on http://{API_HOST}:{API_PORT} (pretty=/ , simple=/simple , settings=/api/settings)")

# --- main loop ---
def main():
    _load_settings()  # load and apply persistent settings
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





