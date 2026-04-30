# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.

## Highrise Music Bot

- **File**: `bot.py` — Python bot using `highrise-bot-sdk` + `yt-dlp` + `ffmpeg`
- **Runtime**: Python 3.11 (`.pythonlibs/bin/python`)
- **Workflow**: `Highrise Bot` — runs `bot.py` continuously
- **Commands**: `!play <url>`, `!skip`, `!stop`, `!queue`
- **Streaming**: ffmpeg encodes YouTube audio as MP3 → curl PUT streams to Icecast

### Secrets

| Secret | Purpose |
|--------|---------|
| `HIGHRISE_BOT_TOKEN` | Highrise bot API token |
| `HIGHRISE_ROOM_ID` | Target Highrise room ID |
| `ZENO_SOURCE_URL` | Icecast source URL (`icecast://source:<pass>@host:port/mount`) |
| `GITHUB_TOKEN` | GitHub PAT (classic, `repo` scope) for `amirboussafsaf03-ctrl` |

### GitHub Repository

- **URL**: https://github.com/amirboussafsaf03-ctrl/highrise-music-bot
- The GitHub integration was dismissed by the user; pushes use the GitHub Contents API via `GITHUB_TOKEN` (stored as env var). Direct `git push` requires the token embedded in the remote URL.
- Note: the Replit GitHub connector (`connector:ccfg_github_01K4B9XD3VRVD2F99YM91YTCAF`) was dismissed — use the token approach going forward.
