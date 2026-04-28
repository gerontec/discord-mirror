#!/usr/bin/env python3
"""
Discord Mirror — Crontab-Poller (alle 5 Min)
Lesen:   User-Token (zwanglos) via CDP aus laufendem Discord-Client
Schreiben: DiscRent Bot via Webhooks — postet mit Original-Autorname + Avatar
"""

import json, os, sys, time
import urllib.request, urllib.error, urllib.parse
import websocket
import requests as _requests

_session = _requests.Session()
_session.headers.update({
    "User-Agent": "DiscordBot (discord-mirror, 1.0)",
    "Content-Type": "application/json",
})

SOURCE_GUILD  = "1391911978150264944"  # LocalLLM
TARGET_GUILD  = "1498286166607265793"  # IntelOneAPI_B70
CDP_URL       = "http://localhost:9222"
BOT_KEY_FILE  = os.path.join(os.path.dirname(__file__), "discrent.key")  # nicht im Repo!
STATE_FILE    = os.path.join(os.path.dirname(__file__), "discord_mirror_state.json")
LOCK_FILE     = "/tmp/discord_mirror.lock"
LOG_FILE      = "/tmp/discord_mirror.log"

# Bot-Token aus Datei oder Umgebungsvariable DISCORD_BOT_TOKEN
BOT_TOKEN = (
    os.environ.get("DISCORD_BOT_TOKEN")
    or (open(BOT_KEY_FILE).read().strip() if os.path.exists(BOT_KEY_FILE) else "")
)
if not BOT_TOKEN:
    print("ERROR: Bot-Token fehlt. discrent.key anlegen oder DISCORD_BOT_TOKEN setzen.")
    sys.exit(1)

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ── Lock ───────────────────────────────────────────────────────────────────

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        try:
            if time.time() - os.path.getmtime(LOCK_FILE) < 300:
                log("Anderer Lauf aktiv (Lock), abbruch.")
                sys.exit(0)
        except: pass
    open(LOCK_FILE, "w").close()

def release_lock():
    try: os.remove(LOCK_FILE)
    except: pass

# ── State ──────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"token": None, "channel_map": {}, "last_seen": {}, "webhooks": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── User-Token via CDP (nur zum Lesen) ────────────────────────────────────

def capture_token():
    try:
        version = json.loads(urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=5).read())
        bws_url = version["webSocketDebuggerUrl"]
    except Exception as e:
        log(f"CDP nicht erreichbar: {e}"); return None

    bws = websocket.create_connection(bws_url, origin="http://localhost:9222", timeout=15)
    c = [0]

    def send(method, params=None, session=None):
        c[0] += 1
        msg = {"id": c[0], "method": method, "params": params or {}}
        if session: msg["sessionId"] = session
        bws.send(json.dumps(msg)); return c[0]

    mid = send("Target.getTargets")
    targets = []
    bws.settimeout(5)
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            r = json.loads(bws.recv())
            if r.get("id") == mid:
                targets = r.get("result", {}).get("targetInfos", []); break
        except: break

    page = next((t for t in targets if t.get("type") == "page" and "discord" in t.get("url", "")), None)
    if not page:
        bws.close(); return None

    mid2 = send("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True})
    session_id = None
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            r = json.loads(bws.recv())
            if r.get("id") == mid2:
                session_id = r.get("result", {}).get("sessionId"); break
        except: break

    if not session_id:
        bws.close(); return None

    send("Fetch.enable", {"patterns": [{"urlPattern": "https://discord.com/api/v9/*", "requestStage": "Request"}]}, session_id)
    send("Page.navigate", {"url": f"https://discord.com/channels/{SOURCE_GUILD}"}, session_id)

    token = None
    bws.settimeout(2)
    deadline = time.time() + 20
    while time.time() < deadline and not token:
        try:
            msg = json.loads(bws.recv())
            if msg.get("sessionId") == session_id and msg.get("method") == "Fetch.requestPaused":
                p = msg["params"]
                for k, v in p.get("request", {}).get("headers", {}).items():
                    if k.lower() == "authorization" and len(v) > 20:
                        token = v
                send("Fetch.continueRequest", {"requestId": p["requestId"]}, session_id)
                if token: break
        except websocket.WebSocketTimeoutException: pass

    try:
        send("Fetch.disable", {}, session_id)
        bws.close()
    except: pass
    return token

# ── REST: User-Token (Lesen) ───────────────────────────────────────────────

USER_TOKEN = None

def api_get(path):
    req = urllib.request.Request(
        f"https://discord.com/api/v9{path}",
        headers={"Authorization": USER_TOKEN, "User-Agent": "Mozilla/5.0"}
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            time.sleep(float(e.headers.get("Retry-After", 3)))
            return api_get(path)
        return {"error": e.code, "body": e.read().decode()[:100]}

# ── REST: Bot-Token (Schreiben / Webhooks erstellen) ──────────────────────

_bot_session = _requests.Session()
_bot_session.headers.update({
    "Authorization": f"Bot {BOT_TOKEN}",
    "Content-Type":  "application/json",
    "User-Agent":    "DiscordBot (discord-mirror, 1.0)",
})

def bot_post(path, data):
    for _ in range(3):
        r = _bot_session.post(f"https://discord.com/api/v9{path}", json=data, timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 3))); continue
        if r.status_code in (200, 201, 204):
            return r.json() if r.content else True
        log(f"Bot POST {path} HTTP {r.status_code}: {r.text[:80]}")
        return None
    return None

def bot_patch(path, data):
    for _ in range(3):
        r = _bot_session.patch(f"https://discord.com/api/v9{path}", json=data, timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 3))); continue
        if r.status_code in (200, 204):
            return r.json() if r.content else True
        return None
    return None

def bot_post_embed(channel_id, username, avatar_url, content, attachments=None, embeds_src=None):
    """Bot postet eine Embed-Nachricht mit Original-Autorinfo."""
    desc = content[:4000] if content else ""
    for att in (attachments or []):
        desc += f"\n📎 [{att.get('filename','Anhang')}]({att.get('url','')})"
    for emb in (embeds_src or []):
        title = emb.get("title", "")
        url   = emb.get("url", "")
        d     = (emb.get("description") or "")[:300]
        if title or url:
            desc += f"\n🔗 **{title}** {url}"
        if d:
            desc += f"\n> {d}"

    embed = {
        "description": desc[:4000] or "(leer)",
        "author": {
            "name":     username,
            "icon_url": avatar_url,
        },
        "color": 0x5865F2,
    }
    return bot_post(f"/channels/{channel_id}/messages", {"embeds": [embed]})

# ── Webhook-Map aufbauen ───────────────────────────────────────────────────

def ensure_webhooks(channel_map, webhook_map):
    """Erstellt fehlende Webhooks via User-Token in jedem Mirror-Kanal."""
    for src_id, tgt_id in channel_map.items():
        if tgt_id in webhook_map:
            continue
        # User-Token erstellt den Webhook (Owner-Rechte), Posting läuft anonym durch URL
        body = json.dumps({"name": "LocalLLM Mirror"}).encode()
        req = urllib.request.Request(
            f"https://discord.com/api/v9/channels/{tgt_id}/webhooks",
            data=body,
            headers={"Authorization": USER_TOKEN, "User-Agent": "Mozilla/5.0",
                     "Content-Type": "application/json"},
            method="POST"
        )
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=15).read())
            if "url" in r:
                webhook_map[tgt_id] = r["url"]
                log(f"Webhook erstellt für channel {tgt_id}")
                time.sleep(0.3)
            else:
                log(f"Webhook-Erstellung fehlgeschlagen für {tgt_id}: {r}")
        except urllib.error.HTTPError as e:
            log(f"Webhook HTTP {e.code} für {tgt_id}: {e.read().decode()[:80]}")
    return webhook_map

# ── Channel-Map aufbauen ───────────────────────────────────────────────────

def build_channel_map():
    src_channels = api_get(f"/guilds/{SOURCE_GUILD}/channels")
    tgt_channels = api_get(f"/guilds/{TARGET_GUILD}/channels")

    if isinstance(src_channels, dict) or isinstance(tgt_channels, dict):
        log("Fehler beim Laden der Channels"); return {}

    src_text   = {c["id"]: c for c in src_channels if c.get("type") in (0, 5, 11, 15)}
    tgt_by_name = {c["name"]: c for c in tgt_channels}

    mirror_cat = next((c for c in tgt_channels if c["name"] == "localllm-mirror" and c["type"] == 4), None)
    if not mirror_cat:
        mirror_cat = bot_post(f"/guilds/{TARGET_GUILD}/channels", {"name": "localllm-mirror", "type": 4})
        time.sleep(0.5)

    cat_id = mirror_cat.get("id") if mirror_cat else None
    channel_map = {}

    for src_id, src_ch in src_text.items():
        name  = src_ch["name"]
        tgt_ch = tgt_by_name.get(name)
        if not tgt_ch:
            payload = {"name": name, "type": 0}
            if cat_id: payload["parent_id"] = cat_id
            r = bot_post(f"/guilds/{TARGET_GUILD}/channels", payload)
            if r and "id" in r:
                tgt_ch = r
                log(f"Channel #{name} erstellt")
                time.sleep(0.5)
            else:
                continue
        channel_map[src_id] = tgt_ch["id"]

    return channel_map

# ── Kanäle nach Aktivität sortieren ───────────────────────────────────────

def reorder_channels_by_activity(active_channel_ids):
    tgt_channels = api_get(f"/guilds/{TARGET_GUILD}/channels")
    if not isinstance(tgt_channels, list): return

    mirror_cat = next((c for c in tgt_channels if c["name"] == "localllm-mirror" and c["type"] == 4), None)
    if not mirror_cat: return
    cat_id = mirror_cat["id"]

    mirror_chs = [c for c in tgt_channels if c.get("parent_id") == cat_id and c.get("type") == 0]
    if not mirror_chs: return

    active_set = set(active_channel_ids)
    ordered = (
        [c for c in mirror_chs if c["id"] in active_set] +
        [c for c in mirror_chs if c["id"] not in active_set]
    )
    positions = [{"id": c["id"], "position": i, "parent_id": cat_id} for i, c in enumerate(ordered)]
    bot_patch(f"/guilds/{TARGET_GUILD}/channels", positions)

# ── Nachrichten spiegeln via Webhook ──────────────────────────────────────

def mirror_new_messages(channel_map, webhook_map, last_seen):
    mirrored = 0
    active_target_channels = []

    for src_id, tgt_id in channel_map.items():
        webhook_url = webhook_map.get(tgt_id)
        if not webhook_url:
            continue

        after = last_seen.get(src_id, "0")
        path  = f"/channels/{src_id}/messages?limit=50"
        if after != "0":
            path += f"&after={after}"

        msgs = api_get(path)
        if not isinstance(msgs, list) or not msgs:
            continue

        msgs = sorted(msgs, key=lambda m: m["id"])
        channel_had_new = False

        for m in msgs:
            if m.get("author", {}).get("bot"):
                last_seen[src_id] = m["id"]
                continue

            author    = m["author"]
            username  = author.get("global_name") or author.get("username", "?")
            avatar_id = author.get("avatar")
            user_id   = author.get("id")
            content   = m.get("content", "")

            if avatar_id:
                avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_id}.png"
            else:
                avatar_url = f"https://cdn.discordapp.com/embed/avatars/{int(user_id) % 5}.png"

            if not content.strip() and not m.get("attachments") and not m.get("embeds"):
                last_seen[src_id] = m["id"]
                continue

            result = bot_post_embed(
                tgt_id, username, avatar_url, content,
                m.get("attachments", []), m.get("embeds", [])
            )

            if not result:
                log(f"Embed-Post fehlgeschlagen für channel {tgt_id}")
                time.sleep(1)
            else:
                mirrored += 1
                channel_had_new = True
                time.sleep(0.3)

            last_seen[src_id] = m["id"]

        if channel_had_new:
            active_target_channels.append(tgt_id)

    if active_target_channels:
        reorder_channels_by_activity(active_target_channels)

    return mirrored

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    global USER_TOKEN
    acquire_lock()
    try:
        state = load_state()

        # User-Token (Lesen)
        USER_TOKEN = state.get("token")
        if USER_TOKEN:
            r = api_get("/users/@me")
            if isinstance(r, dict) and r.get("error") == 401:
                USER_TOKEN = None

        if not USER_TOKEN:
            log("User-Token erneuern via CDP...")
            USER_TOKEN = capture_token()
            if not USER_TOKEN:
                log("Kein Token — Discord läuft nicht?")
                sys.exit(1)
            log("User-Token OK")
            state["token"] = USER_TOKEN

        # Channel-Map
        channel_map = state.get("channel_map", {})
        if not channel_map:
            log("Baue Channel-Map auf...")
            channel_map = build_channel_map()
            state["channel_map"] = channel_map
            log(f"{len(channel_map)} Kanäle gemappt")

        # Webhooks
        webhook_map = state.get("webhooks", {})
        missing = [tgt for tgt in channel_map.values() if tgt not in webhook_map]
        if missing:
            log(f"{len(missing)} Webhooks fehlen, erstelle...")
            webhook_map = ensure_webhooks(channel_map, webhook_map)
            state["webhooks"] = webhook_map

        last_seen = state.get("last_seen", {})

        # Erster Lauf: nur Checkpoint setzen
        if not last_seen:
            log("Erster Lauf — setze Checkpoint (keine alten Posts spiegeln)...")
            for src_id in channel_map:
                msgs = api_get(f"/channels/{src_id}/messages?limit=1")
                last_seen[src_id] = msgs[0]["id"] if isinstance(msgs, list) and msgs else "0"
            state["last_seen"] = last_seen
            save_state(state)
            log("Initialisierung abgeschlossen.")
            return

        n = mirror_new_messages(channel_map, webhook_map, last_seen)
        state["last_seen"] = last_seen
        state["webhooks"]   = webhook_map
        save_state(state)

        log(f"{n} Nachrichten gespiegelt" if n > 0 else "Keine neuen Nachrichten")

    finally:
        release_lock()

if __name__ == "__main__":
    main()
