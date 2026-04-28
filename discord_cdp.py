#!/usr/bin/env python3
"""Discord CDP Bot — steuert den laufenden Discord-Client via Chrome DevTools Protocol."""

import json
import urllib.request
import websocket

CDP_URL = "http://localhost:9222"


def get_discord_tab():
    tabs = json.loads(urllib.request.urlopen(f"{CDP_URL}/json/list").read())
    return next(
        (t for t in tabs if t.get("type") == "page" and "discord" in t.get("url", "")),
        None
    )


class DiscordCDP:
    def __init__(self):
        tab = get_discord_tab()
        if not tab:
            raise RuntimeError("Discord-Tab nicht gefunden. Läuft Discord mit --remote-debugging-port=9222?")
        print(f"Verbunden: {tab['title']}")
        self.ws = websocket.create_connection(
            tab["webSocketDebuggerUrl"],
            origin="http://localhost:9222"
        )
        self._id = 0

    def _call(self, method, params=None):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        target_id = self._id
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == target_id:
                return msg.get("result", {})

    def js(self, expression):
        r = self._call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        val = r.get("result", {})
        if val.get("type") == "object" and "value" not in val:
            return None
        return val.get("value")

    def get_current_channel(self):
        """Gibt Channel-ID und Server-ID aus der aktuellen URL."""
        url = self.js("window.location.pathname")
        parts = url.strip("/").split("/")
        # /channels/GUILD_ID/CHANNEL_ID
        if len(parts) >= 3 and parts[0] == "channels":
            return {"guild_id": parts[1], "channel_id": parts[2]}
        return {"url": url}

    def get_title(self):
        return self.js("document.title")

    def send_message(self, text):
        """Nachricht in den aktuellen Kanal senden via Discord-interne API."""
        channel = self.get_current_channel()
        channel_id = channel.get("channel_id")
        if not channel_id:
            raise ValueError("Kein Channel aktiv")
        escaped = text.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
        script = f"""
        (() => {{
            const req = webpackChunkdiscord_app.push([[Symbol()],{{}},r=>r]);
            webpackChunkdiscord_app.pop();
            const api = Object.values(req.c)
                .map(m => m?.exports?.default)
                .find(e => typeof e?.post === 'function' && typeof e?.get === 'function');
            if (!api) return 'REST-API nicht gefunden';
            api.post({{
                url: '/api/v9/channels/{channel_id}/messages',
                body: {{ content: `{escaped}` }}
            }});
            return 'gesendet';
        }})()
        """
        return self.js(script)

    def get_messages(self, limit=10):
        """Letzte Nachrichten aus dem aktiven Channel laden."""
        channel = self.get_current_channel()
        channel_id = channel.get("channel_id")
        if not channel_id:
            raise ValueError("Kein Channel aktiv")
        script = f"""
        (async () => {{
            const req = webpackChunkdiscord_app.push([[Symbol()],{{}},r=>r]);
            webpackChunkdiscord_app.pop();
            const api = Object.values(req.c)
                .map(m => m?.exports?.default)
                .find(e => typeof e?.get === 'function' && e?.HTTP);
            if (!api) return '[]';
            const r = await api.get({{url: '/api/v9/channels/{channel_id}/messages?limit={limit}'}});
            return JSON.stringify(r.body.map(m => ({{
                author: m.author.username,
                content: m.content,
                timestamp: m.timestamp
            }})));
        }})()
        """
        result = self.js(script)
        if result and result != "[]":
            try:
                return json.loads(result)
            except Exception:
                pass
        return []

    def close(self):
        self.ws.close()


# --- Demo ---
if __name__ == "__main__":
    bot = DiscordCDP()

    print("\n--- Aktueller Kanal ---")
    print(bot.get_current_channel())
    print("Titel:", bot.get_title())

    print("\n--- Letzte 5 Nachrichten ---")
    msgs = bot.get_messages(5)
    if msgs:
        for m in msgs:
            print(f"  [{m['author']}]: {m['content'][:80]}")
    else:
        print("  (keine Nachrichten geladen)")

    bot.close()
    print("\nFertig.")
