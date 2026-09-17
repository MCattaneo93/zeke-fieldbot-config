# Zeke + Fieldbot config

Private config and source for the two OpenClaw agents running at POS.com:

- **`zeke/`** — Matthew's personal assistant. Behavioral config only (identity,
  boundaries, skills). No state, no logs, no business data — those stay on the
  gateway host.
- **`fieldbot-openclaw-plugin/`** — the guarded Odoo tool plugin Fieldbot (the
  team ticket bot) and Zeke both use. TypeScript tool definitions + Python
  bridge to Odoo, with an offline test suite (`npm run typecheck`,
  `python3 -m unittest discover -s python/tests`).

## What's deliberately not here

No tokens, API keys, or passwords — those live in the gateway's environment or
a root-owned secret file, never in this repo or in code. See
`fieldbot-openclaw-plugin/python/config.py` for how the plugin reads them.

Also excluded: build output (`dist/`), packed artifacts (`*.tgz`), and anything
under `zeke/`'s `state/`, `notes/`, `media/`, or `outbox/` — those are runtime
data on the live gateway, not config.

## Layout on the gateway host

This repo mirrors:

```
~/zeke/workspace/{AGENTS,IDENTITY,SOUL,TOOLS,USER,HEARTBEAT}.md
~/zeke/workspace/skills/
~/fieldbot-openclaw/plugin/
```

Pulling changes here does not update the live gateway automatically — copy
back to those paths and restart `openclaw-gateway` to pick them up.
