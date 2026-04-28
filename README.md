# Discord Mirror

Spiegelt alle Posts eines Discord-Servers in einen eigenen Server.

## Setup

1. Bot-Token in `discrent.key` ablegen (oder `DISCORD_BOT_TOKEN` setzen)
2. `pip install requests websocket-client`
3. Crontab: `*/5 * * * * python3 discord_mirror_poll.py`

## Funktionsweise

- Liest neue Nachrichten via User-Token (CDP aus laufendem Discord-Client)
- Postet via Bot-Token mit Original-Autorname + Avatar als Embed
- Speichert State in `discord_mirror_state.json`
