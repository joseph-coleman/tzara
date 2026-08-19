# Tzara

A personal, local-only wiki that serves your markdown as web pages - with inline Jupyter execution, git versioning, local-LLM document Q&A, and background agents and editors.

It works as a web frontend for an Obsidian vault (those files are just markdown), and it's designed to run on your own machine for personal note-taking. Nothing leaves your computer: the language models run locally via Ollama (or Lemonade).

## Quick start

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine + Compose v2). Then either download the zip from this repo or clone it with git:

```bash
git clone https://github.com/joseph-coleman/tzara.git tzara
cd tzara
```

You run setup **twice** - once to create your config, you then edit it, and then once more to build. The first run copies `.env.template` to `.env`, generates a strong random `POSTGRES_PASSWORD`, and then **stops** so you can review your settings before anything is built (Postgres bakes the password in on first start and can't change it afterward, so this pause matters):

```bash
./setup.sh          # macOS / Linux
```
```powershell
.\setup.ps1         # Windows PowerShell
```

Now open `.env` and check the settings that are painful to change later. The [configuration guide](app/seed/system/help/configurations.md) walks through the handful worth getting right on the first install (where your vaults live, which models to use, GPU vs. external inference). The defaults "just work" if you only want to test.  You can always change your docker configuration later. 

When you're happy, run the **same command again** - this time it builds and starts the stack:

```bash
./setup.sh          # (or .\setup.ps1) - second run builds and starts
```

On this first build it also pulls the two default local models (`llama3.2:3b` for chat, `embeddinggemma:300m` for embeddings) via the `ollama-init` service and this can take several minutes on a fresh machine.  If you're using Lemonade then I highly recommend `Llama-3.2-3B-Instruct-GGUF:latest` and `embeddinggemma-300m-qat-q8_0-GGUF-Q8_0`.  But if you have the RAM, then go bigger for your completion + tools chat model. 

When it's up:

- Wiki:            http://localhost:8000/
- Readiness check: http://localhost:8000/health

`/health` returns a single JSON status for Postgres, Redis, Ollama, and whether your chat + embedding models are present.  Check it out first if anything looks broken.

Prefer to do it by hand? `setup` is just a convenience wrapper - the equivalent manual steps are, basically:

```bash
cp .env.template .env
# edit .env - at minimum set a real POSTGRES_PASSWORD (setup generates one for
# you; by hand you must pick one BEFORE the first `up`, as it can't change later)
docker compose up --build
```

## Configuration (`.env`) a.k.a. The Fun Stuff

All configuration lives in `.env` (copied from `.env.template`). It is read at **runtime** by the containers - change a value and `docker compose up` again; no image rebuild needed. `.env` is git-ignored and docker-ignored, so your secrets never end up committed or baked into an image layer.

The defaults are chosen to "just work" with zero edits. For the settings most worth getting right on the first install - and *why* they're hard to change later, see the [configuration guide](app/seed/system/help/configurations.md) (the same doc is available inside the wiki under **help**). The handful you're most likely to touch:

| Variable | What it does | Default |
|----------|--------------|---------|
| `VAULTS_LOCATION` | Parent dir of your vaults; each subdir is one isolated vault. Point at your notes root. | `./app/vaults` |
| `HISTORY_LOCATION` | Where per-vault git history is stored (keep off any Dropbox/OneDrive-synced path). | `./app/vault-history` |
| `DEFAULT_VAULT` | Which vault the landing page opens. | `main` |
| `PORT` | Web server port. | `8000` |
| `POSTGRES_PASSWORD` | Postgres auth. `setup` writes a random one for you; set your own if configuring by hand. Baked in on first start - can't change afterward. | *(generated)* |
| `OLLAMA_MODEL` / `OLLAMA_EMBED_MODEL` | Chat / embedding model names. | `llama3.2:3b` / `embeddinggemma:300m` |

Storage for Postgres and Ollama models defaults to Docker-managed **named volumes** (`pg_data`, `ollama_models`) so there are no host paths to create. To store that data at a specific host path instead, set `POSTGRES_DATA_LOCATION` / `OLLAMA_MODELS` to an absolute path in `.env`.  This is recommended, but not needed if you're just trying this out. 

The `ADVANCED` block in `.env.template` documents everything else (LLM backend provider, etc.).

## GPU acceleration / external Ollama

The base `docker-compose.yml` runs Ollama on CPU. To change that, layer an
overlay by editing `COMPOSE_FILE` in `.env`:

```bash
# NVIDIA GPU
COMPOSE_FILE=docker-compose.yml;docker-compose.nvidia.yml
# AMD ROCm GPU
COMPOSE_FILE=docker-compose.yml;docker-compose.amd.yml
# Use an inference server you run elsewhere - stock Ollama, Lemonade, vLLM,
# LocalAI, etc. (set OLLAMA_URL + LLM_PROVIDER to point at it)
COMPOSE_FILE=docker-compose.yml;docker-compose.external-inference.yml
```

(`COMPOSE_PATH_SEPARATOR` is `;` on Windows/Docker Desktop, `:` on macOS/Linux.)
With the external-inference overlay, no local Ollama container is started and the first-run model bootstrap is skipped - the models live on your external server. The overlay is topology-only (it just removes the local container); *which* server you talk to and *how* is set by `OLLAMA_URL` + `LLM_PROVIDER` in `.env`.

To catch the easy mistake, `setup` refuses to build if `OLLAMA_URL` points at an external server but this overlay *isn't* in `COMPOSE_FILE` - otherwise it would spin up a local Ollama container and download models you don't need. It tells you exactly what to add to `.env`, then re-run.

## The services

| Service | Role |
|---------|------|
| `tzaraserver` | Starlette web app (the wiki UI + API) |
| `tzaraworker` | Taskiq background worker (indexing, git, agents, editors) |
| `redisserver` | Redis, backing Taskiq |
| `pgserver` | PostgreSQL + pgvector (RAG index) |
| `ollamaserver` | Local LLM server for chat / embeddings, you can bring your own |
| `ollama-init` | One-shot: pulls the default models on first `up`, then exits |
| `jupyterserver` | Per-page Jupyter kernels (inline code execution) |
| `jupyterserver-agent` | Isolated kernel for agent and editor custom-tool code (no vault mount) |

## Agents

Background agents are markdown files in the system vault (`vaults/dada/agents/*.md`): a prompt, a set of granted capabilities, and optional human-authored Python tools. They can run manually, on a `schedule:` ("daily @ 4:30 pm", "2nd saturday", or a cron expression like "0 */4 * * *"), or on an `on:` event trigger ("any agent failed", "uploads in inbox/") - schedules and triggers compose as OR. Writes are governed by a per-agent `mode:` (propose = staged for review in `/agents`; act-with-checkpoint = applied with a pre-image commit). Event triggers carry loop guards: self-exclusion, a chain depth cap, per-agent cooldown and hourly budget (deferred events wait in a pool), and load-time cycle detection.

### Hey, know this:

The agent scheduler and event triggers ship **disabled** - set `AGENT_SCHEDULER_ENABLED=true` and/or `EVENT_TRIGGERS_ENABLED=true` in `.env` to turn them on once you've read the authoring guide (`Authoring_Agents` in the system vault). Activity, staged proposals, and recent events are at `/agents`.

## Updating the help pages

Help documentation is seeded into the system vault once, on first run, and is yours thereafter - upgrading Tzara never overwrites it. To pull in a newer version's help pages when you want them:

```bash
./refresh-docs.sh                             # dry run: report what would change
./refresh-docs.sh --apply                     # refresh pages you still have
./refresh-docs.sh --apply --restore-missing   # also re-add ones you deleted
```

On Windows, run `refresh-docs.bat`. 

The document refresh scripts touche only `help/` and the root pages (example agents and editors are opt-in per file via `--include`), never deletes, and commits each page's previous version first, so the whole run is revertable from the vault's git history.

## License

Tzara is free software, licensed under the **GNU Affero General Public License, version 3.0 or later** (`AGPL-3.0-or-later`). You are free to use, study, modify, and redistribute it under those terms. Because the AGPL covers *network* use, if you run a modified version of Tzara as a service, you must offer your users the corresponding source. The full license text is in [LICENSE.txt](LICENSE.txt).

Copyright (C) 2026 Joseph E. Coleman.

### Third-party components

Tzara bundles a few third-party front-end libraries, each under its own permissive license (retained as allowed by AGPL-3.0 §7). Full texts and details are in [`licenses/`](licenses/):

| Component | Version | License |
|-----------|---------|---------|
| [D3](https://d3js.org) | 5.7.0 | BSD-3-Clause |
| [mpld3](https://mpld3.github.io) | 0.5.12 | BSD-3-Clause |
| [CodeMirror](https://codemirror.net) + [Lezer](https://lezer.codemirror.net) | 6 | MIT |

