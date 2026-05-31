# discord-companion-bot

A **Discord companion bot** designed to fill the hole that [Shapes Inc.](https://shapes.inc) used to fill — a personable AI bot that lives on your server, remembers people, and can be there when someone's having a hard time.

## What it does

- **Conversational memory.** Two-tier memory per user (Claude scratch-space "working notes" + user-controlled long-term entries). The bot remembers who you are across conversations.
- **Persona layer.** Bot character is a swappable config — name, pronouns, mannerisms, voice. Bundled: `plain` (no roleplay, just Claude) and `ressapanda` (a whimsical red panda). Write your own under `personas/`.
- **Distress detection (opt-in per user).** Three-tier keyword + sub-threshold-accumulator + Haiku-semantic-classifier system that catches indirect distress language and switches the bot into emotional-support mode. Designed for autistic users specifically — distinguishes anxiety spirals from real grievances and validates the latter without trying to ground-out the former. **Only fires for users explicitly listed in `monitored_users` config.**
- **Tarot readings.** Custom dual-engine system based on Magic: The Gathering's color pie. `!tarot` for a card pull, `!interpret` for AI interpretation.
- **YouTube transcription.** React 🎙️ to any message with a YouTube link and the bot will transcribe + summarize it (Whisper-backed). Useful for surfacing what's in long videos people share.
- **PluralKit-aware.** Detects PluralKit webhook proxies and handles alter switches gracefully.
- **MTG integration (optional).** If you also install the sibling [`discord-mtg-bot`](https://github.com/VIXAL-OS/discord-mtg-bot) engine on PYTHONPATH, this bot will auto-load the MTG game cog and you get both bots in one process.

## Quick start

```bash
# 1. Clone + create your config
git clone https://github.com/VIXAL-OS/discord-companion-bot.git
cd discord-companion-bot
cp config.json.example config.json
cp .env.example .env
# Edit .env with your Discord token + Anthropic API key

# 2. Install Python deps
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 3. (Optional) Install yt-dlp + Whisper for YouTube transcription
pip install yt-dlp openai-whisper
# Whisper also needs ffmpeg on your PATH

# 4. (Optional) Add a persona — edit config.json:
#    "bot_persona": "ressapanda"      (whimsical red panda)
#    "bot_persona": "plain"           (no roleplay, default)
#    or write your own under personas/

# 5. Run it
python bot.py
```

## Configuration

`config.json` has six knobs:

| Setting | What it does |
|---|---|
| `bot_persona` | Character layer (file under `personas/`). Default `plain`. |
| `monitored_users` | Discord user IDs to proactively monitor for distress. **Empty by default** — opt-in only. |
| `user_name_map` | Discord-ID → memory-file-name map. Lets you store per-user context in `memories/<name>.json`. |
| `excluded_channels` | Channel IDs where the bot never responds. |
| `plural_systems` | PluralKit alter → system-name map (helps the bot understand alters share an account). |
| `youtube_*` | Whisper model + duration cap + age-gate behavior for the transcription feature. |

See `config.json.example` for inline docs on each.

## Privacy posture for `monitored_users`

The distress-detection system is the most sensitive thing in this bot. A few guardrails baked in:

- **It's empty by default.** No user gets monitored without you explicitly listing their Discord ID.
- **Only the listed users get the semantic classifier.** Others' messages are buffered briefly for context but aren't classified separately or persisted past the 30-min rolling window.
- **Per-user memory files are gitignored.** If you write a `memories/alice.json` describing what triggers Alice, that file never enters git.
- **Distress context is opt-in per user.** The Haiku classifier reads a `distress_context` field from the user's memory file if present. Without that field, it runs on generic signals only.
- **Get consent.** Don't add someone to `monitored_users` without telling them. The bot WILL proactively respond to them when distress is detected, which is its whole point but also requires trust.

## Commands

```
# Tarot
!tarot              - Pull a 3-card spread
!interpret          - AI interpretation of your last pull
!suits              - Reference for the 5 MTG-color suits

# Memory
!remember <text>    - Permanently save something to long-term memory
!forget <pattern>   - Remove something from memory
!keep <pattern>     - Promote a working note to long-term
!memories           - See what the bot remembers about you

# Support
!ground             - Sensory grounding exercise
!breathe            - Box breathing guide
!panda              - Random comfort content (red panda images/gifs by default)
!search <query>     - Web search via Claude tool use
!summarize          - Summarize this thread's conversation
!help_support       - Full support-command reference

# Utility
!clear              - Clear this thread's conversation history (memories untouched)
!cost               - Lifetime API usage + cost summary
!context            - Show what context the bot has for this channel
```

## Architecture (high-level)

Three layers compose a response:

1. **Distress detector** runs on every monitored user's message. Score → mode (default / stressed / spiral).
2. **Mode → model**. Default: Sonnet. Stressed/spiral: Opus (richer support). Background distress classifier: Haiku.
3. **Persona + mode → system prompt.** Templated from the persona JSON + a built-in distress-response framework (validate grievances vs. ground spirals).

See [ARCHITECTURE.md](ARCHITECTURE.md) for the deeper tech overview.

## Discord setup checklist

1. Create a Discord application at <https://discord.com/developers/applications>
2. Add a Bot user; grab the **bot token** (`.env` as `DISCORD_TOKEN`)
3. Enable Privileged Gateway Intents:
   - **Server Members Intent**
   - **Message Content Intent**
4. Generate an OAuth2 invite URL with `bot` + `applications.commands` scopes and permissions: `Send Messages`, `Read Message History`, `Attach Files`, `Embed Links`, `Add Reactions`, `Use Slash Commands`, `Manage Threads`, `Create Public Threads`, `Send Messages in Threads`.
5. Invite the bot to your server.
6. Get an Anthropic API key from <https://console.anthropic.com>, put it in `.env` as `ANTHROPIC_API_KEY`.
7. Run `python bot.py`.

## Personas

`personas/plain.json` is the default — no roleplay, just Claude being friendly and direct. Switch to `ressapanda.json` for a whimsical red panda. Write your own by copying either file and editing per [`personas/README.md`](personas/README.md). Personas only affect voice; all capabilities (memory, distress detection, tarot, transcription) work the same regardless.

## Costs

| Use case | ~Cost |
|---|---|
| Casual chat (Sonnet) | $0.003 per message round-trip |
| Emotional support (Opus when distress detected) | $0.015 per round-trip |
| Background distress classifier (Haiku) | $0.0001 per message |
| YouTube transcription (Whisper, local) | Free (compute time only) |

Tracked in `data/api_costs.json` and queryable with `!cost`.

## Contributing

PRs welcome. Things this bot wants to grow into:

- **More personas.** Send a PR with `personas/<your-name>.json` and we'll include it.
- **Moderation features.** Auto-flagging of harmful patterns, configurable response policies, server-admin tooling.
- **Better distress models.** The current Haiku classifier is decent but generic; per-user calibration would help.
- **More languages.** Right now the distress detection is English-only.

Before opening a PR:
- `python -m py_compile bot.py` should pass
- If you change the distress detector, post a couple example transcripts demonstrating the new behavior

## License

MIT. See [LICENSE](LICENSE) if present, otherwise treat as MIT until one is added.
