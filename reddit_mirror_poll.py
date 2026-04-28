#!/usr/bin/env python3
"""
Reddit Mirror — Crontab-Poller (alle 5 Min)
Spiegelt neue Posts aus r/LocalLLaMA → Discord IntelOneAPI_B70
"""

import json, os, sys, time
import urllib.request, urllib.error
import requests

SUBREDDIT    = "LocalLLaMA"
TARGET_GUILD = "1498286166607265793"  # IntelOneAPI_B70
BOT_KEY_FILE = os.path.join(os.path.dirname(__file__), "discrent.key")
STATE_FILE   = os.path.join(os.path.dirname(__file__), "reddit_mirror_state.json")
LOCK_FILE    = "/tmp/reddit_mirror.lock"
LOG_FILE     = "/tmp/discord_mirror.log"
REDDIT_UA    = "discord-mirror/1.0 by gerontec"

BOT_TOKEN = (
    os.environ.get("DISCORD_BOT_TOKEN")
    or (open(BOT_KEY_FILE).read().strip() if os.path.exists(BOT_KEY_FILE) else "")
)
if not BOT_TOKEN:
    print("ERROR: Bot-Token fehlt.")
    sys.exit(1)

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [Reddit] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ── Lock ───────────────────────────────────────────────────────────────────

def acquire_lock():
    if os.path.exists(LOCK_FILE):
        if time.time() - os.path.getmtime(LOCK_FILE) < 300:
            log("Lock aktiv, abbruch.")
            sys.exit(0)
    open(LOCK_FILE, "w").close()

def release_lock():
    try: os.remove(LOCK_FILE)
    except: pass

# ── State ──────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_seen_id": None, "channel_id": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Bot API ────────────────────────────────────────────────────────────────

_bot = requests.Session()
_bot.headers.update({
    "Authorization": f"Bot {BOT_TOKEN}",
    "Content-Type":  "application/json",
    "User-Agent":    "DiscordBot (discord-mirror, 1.0)",
})

def bot_get(path):
    r = _bot.get(f"https://discord.com/api/v9{path}", timeout=15)
    return r.json() if r.ok else None

def bot_post(path, data):
    for _ in range(3):
        r = _bot.post(f"https://discord.com/api/v9{path}", json=data, timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 3))); continue
        if r.status_code in (200, 201):
            return r.json()
        log(f"Bot POST {path} HTTP {r.status_code}: {r.text[:80]}")
        return None
    return None

# ── Discord-Channel sicherstellen ──────────────────────────────────────────

def ensure_channel(state):
    if state.get("channel_id"):
        return state["channel_id"]

    channels = bot_get(f"/guilds/{TARGET_GUILD}/channels") or []
    existing = next((c for c in channels if c["name"] == f"reddit-{SUBREDDIT.lower()}" and c["type"] == 0), None)

    if existing:
        state["channel_id"] = existing["id"]
        return existing["id"]

    # Kategorie suchen (localllm-mirror)
    cat = next((c for c in channels if c["name"] == "localllm-mirror" and c["type"] == 4), None)
    payload = {"name": f"reddit-{SUBREDDIT.lower()}", "type": 0, "topic": f"Mirror von r/{SUBREDDIT}"}
    if cat:
        payload["parent_id"] = cat["id"]

    r = bot_post(f"/guilds/{TARGET_GUILD}/channels", payload)
    if r and "id" in r:
        log(f"Channel #reddit-{SUBREDDIT.lower()} erstellt")
        state["channel_id"] = r["id"]
        return r["id"]

    log("Channel-Erstellung fehlgeschlagen")
    return None

# ── Reddit pollen ──────────────────────────────────────────────────────────

def fetch_new_posts(after_id=None):
    url = f"https://www.reddit.com/r/{SUBREDDIT}/new.json?limit=25&sort=new"
    headers = {"User-Agent": REDDIT_UA}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        posts = r.json()["data"]["children"]
    except Exception as e:
        log(f"Reddit-Fehler: {e}")
        return []

    posts = [p["data"] for p in posts]
    posts = sorted(posts, key=lambda p: p["created_utc"])

    if after_id:
        seen = False
        new_posts = []
        for p in posts:
            if seen:
                new_posts.append(p)
            if p["id"] == after_id:
                seen = True
        return new_posts

    return posts

# ── Post als Embed spiegeln ────────────────────────────────────────────────

FLAIR_COLORS = {
    "News":        0xFF4500,
    "Discussion":  0x5865F2,
    "Question":    0x57F287,
    "Tutorial":    0xFEE75C,
    "Project":     0xEB459E,
    "Model":       0x9B59B6,
    "Meme":        0xED4245,
}

def post_to_discord(channel_id, post):
    title   = post["title"][:256]
    author  = post["author"]
    score   = post["score"]
    url     = f"https://reddit.com{post['permalink']}"
    flair   = post.get("link_flair_text") or ""
    selftext = (post.get("selftext") or "")[:800]
    thumb   = post.get("thumbnail")
    is_img  = post.get("post_hint") == "image"
    img_url = post.get("url") if is_img else None

    color = FLAIR_COLORS.get(flair, 0xFF4500)

    desc = selftext
    if not desc and not is_img:
        desc = post.get("url", "")[:300]

    embed = {
        "title":       title,
        "url":         url,
        "description": desc[:4000] if desc else None,
        "color":       color,
        "author": {
            "name":     f"u/{author}",
            "url":      f"https://reddit.com/u/{author}",
            "icon_url": "https://www.redditstatic.com/desktop2x/img/favicon/favicon-32x32.png",
        },
        "footer": {
            "text": f"r/{SUBREDDIT} • {flair} • {score}↑"
        },
    }

    if img_url and img_url.startswith("http"):
        embed["image"] = {"url": img_url}
    elif thumb and thumb.startswith("http"):
        embed["thumbnail"] = {"url": thumb}

    return bot_post(f"/channels/{channel_id}/messages", {"embeds": [embed]})

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    acquire_lock()
    try:
        state = load_state()

        channel_id = ensure_channel(state)
        if not channel_id:
            sys.exit(1)

        posts = fetch_new_posts(state.get("last_seen_id"))

        if not posts:
            if not state.get("last_seen_id"):
                # Erster Lauf: Checkpoint setzen
                all_posts = fetch_new_posts()
                if all_posts:
                    state["last_seen_id"] = all_posts[-1]["id"]
                    save_state(state)
                    log(f"Initialisierung: {len(all_posts)} Posts als Checkpoint, nächster Lauf spiegelt neue.")
            else:
                log("Keine neuen Posts")
            return

        mirrored = 0
        for post in posts:
            r = post_to_discord(channel_id, post)
            if r:
                mirrored += 1
                state["last_seen_id"] = post["id"]
                time.sleep(0.5)
            else:
                log(f"Post fehlgeschlagen: {post['title'][:60]}")

        save_state(state)
        log(f"{mirrored} Posts gespiegelt")

    finally:
        release_lock()

if __name__ == "__main__":
    main()
