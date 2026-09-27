# VOX

A realtime voice agent for Windows that **sees and controls your desktop** and
**remembers things between sessions**.

It talks, it looks at your screen, it operates your machine, and it knows things
you told it last week. Everything that changes the machine is gated in code, not
in the prompt.

Built on [LiveKit Agents](https://docs.livekit.io/agents/) 1.8, with the LLM
served by an OpenAI-compatible gateway on your own machine.

```
41 tools by default:  30 for the desktop   11 for the brain
```

---

## What it actually does

| | |
|---|---|
| **Talks** | Realtime speech in, speech out. Interruptible mid-sentence. |
| **Sees** | Screenshots go into the model's context, so it can answer "what's on my screen?" and aim clicks at real coordinates. |
| **Acts** | Launch apps, focus windows, type, click, drag, run commands. |
| **Remembers** | Persistent memory with hybrid recall, so "what did we decide about X?" works next week. |
| **Knows** | A library of 32 skill packs — playbooks for coding, design, research, git and more — that it looks up and follows. |
| **Asks first** | Everything that changes state is gated in code, not in the prompt. |

---

## The safety model

This is the part that matters, because a voice agent is driven by a *fallible
transcription* and acts on your *real machine*. Telling the model "be careful"
is not a control. Every tool therefore asks a `SafetyController` for permission,
and the permission is bound to the exact arguments requested.

```
safe   read-only          always available, even while disarmed
       look_at_screen, list_windows, get_clipboard,
       recall_memory, find_skills, read_skill, ...

low    changes state      requires the agent to be ARMED
       focus_window, launch_app, type into a NAMED window, move_mouse

high   hard to undo       requires ARMED *and* a spoken yes, per action
       click_screen, drag_mouse, press_key, close_window,
       run_powershell_command, remember_this, ask_knowledge
```

**The agent starts disarmed every session.** Ask questions freely; it just
cannot touch anything until you say "arm".

### The brain does not get a way around the gate

This is the central design constraint, and it is worth being explicit about it.
Adding a brain to a voice agent creates an obvious escalation path: if "ask the
brain to do this" were a single low-risk tool, one utterance would start a
multi-step run with shell and filesystem access, and the entire model above
would be bypassed in a single hop.

So the brain has no privileged path. It has no LLM of its own, no HTTP surface,
and no login. It is a library called in-process by tools that ask the same
`SafetyController` the mouse does, and it holds no reference to that controller
at all. Reading the brain works while disarmed; changing it does not.

### Why writing a memory is high risk

A clipboard write is `low` — the user can see it and clear it. A memory write is
invisible, outlives the session, and silently steers every later one. Since the
input is speech, a misheard *"remember that ..."* would otherwise become
permanent.

So every memory write needs a spoken confirmation, which makes the memory store
visible and deliberate:

> **You:** remember that we deploy on Fridays.
> **VOX:** I'll remember that you deploy on Fridays. Shall I save that?
> **You:** yes.
> **VOX:** Saved.

Two further guards, because the input is speech:

- **Credentials are refused outright.** Text matching `password`, `api key`,
  `secret`, `bearer` and similar never reaches storage, armed or not. The audit
  log redacts key-shaped *argument names* but does not inspect values, and a
  spoken *"remember my API key is ..."* must not become a row.
- **Nothing is ever remembered unasked.** agent-os extracted "facts" out of the
  model's own final answer with four regexes. For a voice agent that is a
  memory-poisoning primitive — one misheard sentence, stored forever. Here the
  model must call `remember_this` explicitly, and only when the user asked.

### Confirmations can't be replayed

When a high-risk tool is called without a confirmation, it refuses and returns an
id. The model must describe the action, ask "shall I go ahead?", and only after
a clear yes re-call the tool with that id. The gate then compares a **SHA-256
signature of the tool name plus every argument** against what was recorded when
the user said yes:

- `remember_this("deploy on Fridays")` confirmed → a different note is rejected
- `remember_this` confirmed → `press_key` is rejected
- the same id works once, then is consumed
- confirmations expire (`CONFIRMATION_TTL`, default 90s)
- disarming drops all pending confirmations
- the pending queue is capped at 8, so a confused model cannot bank consents

There is a test for each of these. See `tests/test_safety.py` and
`tests/test_brain.py`.

### Other guards

- **Recalled memories are labelled as data, not orders.** Anything the user ever
  said can come back out of memory, so every recall result is prefixed to make
  clear it is a record rather than an instruction.
- **Skill files cannot be escaped.** `read_skill_file` resolves and then
  containment-checks the path, so a model cannot walk out of the skills tree
  with `../..`.
- **Shell is off by default.** Even with `ALLOW_SHELL=1`, a denylist blocks
  `rm -rf`, `format`, `reg delete`, `Remove-Item -Recurse`, `Invoke-Expression`,
  and ~20 others. One command per call; no `;` or newline chaining.
- **Typing risk depends on destination.** Typing into a window the user *named*
  is `low`. Typing into whatever happens to be focused is `high`.
- **Auto-disarm** after 15 idle minutes by default.
- **Audit log** at `logs/audit.jsonl` — every gated call with its arguments,
  outcome, and redaction of anything key-shaped. Truncated arguments are marked.
- **pyautogui's failsafe is left on**: slam the cursor into a screen corner to
  abort a runaway action.
- **The refusal is a wall, not a suggestion.** The prompt tells the model not to
  route around a `REFUSED`, and the model has no way to bypass the gate.

---

## The brain

Three parts, all optional, all in-process.

### Memory

SQLite at `.data/brain.db`, with **hybrid recall**: a keyword arm (tokenised
OR-match) and a vector arm (cosine), fused with Reciprocal Rank Fusion. Keyword
alone misses paraphrases; vector alone surfaces loosely-related rows; RRF gets
both without tuning a weight.

The default embedder has **zero dependencies** — a stateless hashed word vector,
so vectors written today stay comparable with queries made next week. Installing
`sentence-transformers` switches to real semantic recall for ~2GB of torch.

Measured on agent-os's own paraphrase set: hashed 2/3, sentence-transformers
4/4. Neither handles team jargon ("daily sync" → "standup").

### Skills

32 packs in `skills/`, each a `SKILL.md` with frontmatter and a Markdown
playbook. `find_skills` ranks them by description overlap; `read_skill` returns
a size-capped digest; `read_skill_file` fetches a specific supporting file for
the large packs.

> **This is a fix, not a port.** agent-os shipped these 32 packs and its loader
> read only `name` and `description`, discarding the body. What reached the model
> was a comma-joined list of names — `skills: shadcn, canvas-design` — so a
> 20,000-line `shadcn/cli.md` was never consulted by anything. The packs were
> documentation pretending to be capability. Here the body is actually read.

Because a spoken turn has a small prompt budget, briefs are capped
(`BRAIN_SKILL_BRIEF_CHARS`, default 6000) and truncated packs report which
files they hold, so the model can ask for the one it needs instead of being
handed 88 KB of prose.

### Document store (off by default)

The `data-kernel` runtime — objects, graph, SQL, a Python sandbox, provenance,
grounding, time-travel — behind three tools. It imports pandas and numpy at
module level, so it stays off until asked for:

```powershell
pip install -e ".[kernel]"
# then set BRAIN_DATA_KERNEL=true
```

Its compute sandbox is an AST filter, not a true isolation boundary, and this
process also holds permission to move the mouse. So `ask_knowledge` is `high`
risk and needs confirmation, exactly like a shell command.

---

## What was merged

`vox` is the union of a realtime voice agent and **agent-os**, with
agent-os's five services collapsed into one process.

| Kept | From | Why |
|---|---|---|
| Realtime voice loop, 30 desktop tools, safety model, console, overlay | desktop voice agent | already worked |
| Persistent memory + hybrid recall | `loom_core/memory.py` | genuinely stateful, hard to rewrite |
| 32 skill packs, now readable | `supervisor/.agents/skills/` | the descriptions were real; the bodies were wasted |
| Data kernel | `data-kernel/` | 660 tests of real functionality |
| Specialist capability taxonomy | `supervisor` config | a useful vocabulary for routing |

| Dropped | Why |
|---|---|
| `gateway/llm-gateway.js` | a second LLM path to the same router. Also in **mock mode** — `ROUTER_KEY` was empty, so every reply was a canned string. The voice agent's 9Router path works, so the brain has no LLM of its own. |
| `supervisor/server.js` | see below |
| `studio/` | a visual pipeline builder; nothing in the brain depended on it |
| `AGENT_OS_TOKEN` / bearer roles | with no network surface there is nothing to authenticate, and one permission model beats two |
| The ReAct loop | it has **no native function calling** — tools are described in a prompt string and the model must emit `THOUGHT:/ACTION:/ACTION_INPUT:` text. The LiveKit session already has real function calling, so the session *is* the reasoning loop. This also removed up to 10 sequential non-streaming LLM round-trips per turn, which is not survivable in conversation. |
| Fact extraction from final answers | a memory-poisoning vector under speech input |

**The supervisor was already non-functional**, which is worth knowing before
mourning it. With `providers.routerKey` empty, `callLLM` skipped the gateway
entirely, fell through to Ollama, and returned `null` — so every run answered
*"No LLM answered this run, so nothing was actually done."* Its own log confirms
~0 tokens consumed. Alongside that: the `runId` in the `202` response never
matched the `runId` in its own SSE events, the server emitted `approval_required`
while its bundled UI listened for `approval_needed` (so the approval card never
rendered), `chatHistory` was global rather than per-session (concurrent sessions
cross-contaminated), and raising `guard.maxSteps` above 8 did nothing because
two hard `.slice(0, 8)` caps ran first.

Two things it did that are worth keeping in mind if you extend this: the
`guard.maxTokensPerRun` budget was the *only* budget it enforced, and it does
not survive the merge — in-process there is no supervisor to meter the run.

---

## Setup

### 1. Prerequisites

| What | Why | Where |
|---|---|---|
| Python 3.11+ | runtime | |
| An OpenAI-compatible LLM gateway | the model | 9Router, or a hosted provider |
| **LiveKit Cloud** | carries the audio | <https://cloud.livekit.io> — free tier |
| **Deepgram** | speech-to-text | <https://deepgram.com> — free tier |
| **Cartesia** | text-to-speech | <https://cartesia.ai> — free tier |

LiveKit is not optional: it is the transport for the realtime audio.

### 2. Configure

```powershell
Copy-Item .env.example .env
```

Fill in four values:

```ini
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=
DEEPGRAM_API_KEY=
CARTESIA_API_KEY=
```

And point the LLM at your gateway with a real key:

```ini
LLM_BASE_URL=http://localhost:20128/v1
LLM_API_KEY=sk-...
LLM_MODEL=ag/claude-sonnet-4-6
```

Check everything at any time:

```powershell
python -m voice_os doctor
```

It prints the missing keys, the resolved settings, whether the LLM endpoint is
reachable, which models it offers, whether `LLM_MODEL` is one of them, and the
brain's status.

> Do not put `OPENAI_BASE_URL` in `.env`. The LiveKit OpenAI plugin reads it as
> an override, so it silently takes precedence over `LLM_BASE_URL`. Use
> `LLM_BASE_URL`.

### 3. Run

```powershell
.\run.ps1
```

That installs dependencies, starts 9Router bound to `127.0.0.1`, verifies the
config, starts the worker, and opens the console at <http://127.0.0.1:8787>.

Press **Connect**, allow microphone access, and talk.

> 9Router binds to `0.0.0.0` by default, which exposes your model gateway and
> its keys to everything on your network. `run.ps1` passes `--host 127.0.0.1`
> for this reason. If you start it by hand, do the same.

### The corner overlay

```powershell
.\run.ps1 -Overlay
```

Puts a small always-on-top HUD in one corner: the teal fluid orb plus live
subtitles. It works alongside the console rather than replacing it, and joins
the **same** LiveKit room, so one agent serves both.

- **Click the orb to disarm.** That is the only thing the click does — an
  accidental tap on a floating widget must never *grant* control, so arming
  stays deliberate. This is the panic button.
- **Right-click the orb** to hide it and leave just the subtitles.
- Move the corner with `OVERLAY_CORNER=bottom-right` in `.env`.
- Run it standalone with `python -m voice_os overlay` while the agent is running.

It runs as a **separate process** on purpose: pywebview needs the main thread
for its native window, and the worker needs that thread for asyncio.

### Other modes

```powershell
python -m voice_os doctor     # config + brain check, then exit
python -m voice_os dev        # reload the worker on file changes
python -m voice_os console    # watch an in-flight session
python -m voice_os client     # control server only, no worker
python -m voice_os overlay    # corner orb only, agent must be running
```

Worker-CLI flags are forwarded after `--`:

```powershell
python -m voice_os worker -- --log-level DEBUG
```

To run against a different profile, point `VOICE_OS_ENV` at another env file:

```powershell
$env:VOICE_OS_ENV = "C:\secrets\prod.env"
python -m voice_os worker
```

---

## Talking to it

> "What's on my screen?" — read-only, works while disarmed
>
> "Arm." — now it may focus windows, open apps, type into windows you name
>
> "What do you remember about the deploy process?" — recall, still disarmed
>
> "How do you normally set up a new design system?" — it finds a skill pack
>   and follows it
>
> "Remember that we deploy on Fridays." — describes it, asks, saves on yes
>
> "Forget that." — the note is gone, and you can check with `list_preferences`
>
> "Open Notepad." / "Search for the Declaration of Independence."
>
> "Click the Save button." — looks at the screen, asks *shall I go ahead?*,
>   clicks after you say yes
>
> "Stop." / "Disarm." — immediately, no confirmation, always works

---

## The console

`http://127.0.0.1:8787` is a small operator console: live transcript, current
permission state, pending confirmations, and a tool-activity feed. The **ARM**
button and saying "arm" drive the same controller, so the two can never
disagree. It also mints the LiveKit token, so you need no external tooling.

`/api/brain` reports skill count, memory counts, preferences and the database
path.

It is loopback-only and unauthenticated **by design** — it is a single-user
local console. Don't change `CONTROL_HOST` without adding auth.

---

## Using a different model

Tool calling matters more than raw quality here: a model that *talks about*
clicking instead of calling the tool is useless. The gateway's model ids
change between versions, so ask it rather than trusting a copied id:

```powershell
curl http://localhost:20128/v1/models -H "Authorization: Bearer $env:LLM_API_KEY"
```

`doctor` does this for you and warns when `LLM_MODEL` is not one of the ids the
gateway actually offers — a mismatch otherwise shows up only as a 401 or a
model that never calls a tool.

A model that has reliably worked for this is a Claude-family id. Smaller flash
models have been observed answering in prose and never calling the tool at all,
which makes the agent useless however good their benchmarks are. If the agent
starts narrating instead of acting, that is the first thing to check.

```ini
LLM_MODEL=<an id from /v1/models>
```

Or skip the gateway entirely with a hosted provider:

```ini
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4.1
```

---

## Layout

```
src/voice_os/
  config.py           env config, no import-time side effects
  safety.py           the permission model: arm state, consent, audit
  pipeline.py         STT / LLM / TTS / VAD / turn-detection wiring
  agent.py            persona, instructions, session events
  control_server.py   token minting, arm endpoints, serves both UIs
  overlay.py          the always-on-top corner window (pywebview)
  main.py             entrypoint + `doctor`
  tools/
    _common.py        gate wrapper, lazy imports, result formatting
    screen.py         look_at_screen (real image into context), layout
    input.py          click, drag, scroll, type, keys  <- highest risk
    windows.py        list, focus, resize, close, read window text
    apps.py           launch, open URL, search, shell, clipboard, volume
    guard_tools.py    arm, disarm, confirm, safety_status
    brain.py          memory, skills and the document store, all gated
  brain/
    __init__.py       the Brain facade: one LLM-free, network-free object
    memory_store.py   persistent memory + hybrid recall
    embeddings.py     hashed-word default, sentence-transformers optional
    skills.py         skill pack index, ranking, size-capped reads
    data_kernel/      the data kernel runtime (imported on demand)
skills/               32 skill packs, each a SKILL.md
web/index.html        operator console
web/overlay.html      corner orb + subtitles
tests/                83 tests
```

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

83 tests, no credentials and no network: the LiveKit token is a locally signed
JWT and the brain is in-process, so the whole system is testable offline. The
suite uses its own ports, so it is safe to run while the agent is live.

Roughly half are on the safety layer and the brain gate, because "the brain did
not become a way around the permission model" is the claim that must not
silently regress.

---

## Troubleshooting

**"Cannot reach this page on 127.0.0.1:8787"** — nothing is running. The console
only exists while a process is up. `run.ps1` always leaves a reachable console,
even with an incomplete configuration, because the console is what lists the
missing keys.

**401 from the LLM** — `LLM_API_KEY` must be a real key from your gateway's
dashboard, not a placeholder. `/v1/models` is often public, so it working does
not prove your key is right.

**The model narrates instead of acting** — swap to a model with working tool
calling. This is a model capability issue, not a code issue. `doctor` lists what
your router offers.

**It forgets things** — check `BRAIN_ENABLED` and that `BRAIN_REMEMBER` is not
`false`, then look at `.data/brain.db`. `list_preferences` and the
`brain_status` tool report what it actually holds.

**A skill search returns nothing useful** — ranking is lexical, so a query in
completely different words will miss. `list_skills` prints every pack with its
description; ask for one by id.

**Robots / cut-off replies** — the agent is listening for end-of-turn, not just
silence. Say "stop" and it stops. `ALLOW_INTERRUPTIONS=false` makes it wait its
turn.

**Clicks land in the wrong place** — have it call `look_at_screen` right before
clicking. Multi-monitor setups can report negative coordinates.

**Nothing happens and it says "disarmed"** — say "arm" or press the ARM button.
Read-only desktop *and* brain questions still work while disarmed.

---

## Bugs fixed during the merge

Two pre-existing bugs were found while wiring the two repos together. Both are
worth knowing about because neither was visible from the tests.

**The deprecated turn detector could not start the worker.**
`TURN_DETECTION` defaulted to `multilingual`, which loads
`livekit.plugins.turn_detector` — deprecated in livekit-agents 1.8, and needing a
job context it does not get when the pipeline is built at CLI scope. Every run
died with `RuntimeError: no job context found, are you running this code inside
a job entrypoint?`, including `doctor`. The default is now `hosted`, which is
the supported path, is faster, and is the only option with backchannel
awareness. Selecting `local` now fails with a message that says what is wrong
instead of one aimed at nobody.

**A pipeline built for inspection could not exist.** `build_vad` was called from
`build_pipeline`, so there was no way to validate the providers without also
loading a model. The VAD is now loaded in `prewarm` — where LiveKit's own docs
put it, since it is blocking and slow on first call — and `build_pipeline` takes
it as an argument, so `doctor` can check the configuration without a job.

## Verified on this machine

- The safety model, including the replay and argument-binding cases, under test.
- The brain gate: memory writes refused while disarmed, refused without
  confirmation when armed, credential-shaped text refused even armed, and
  nothing written while consent is outstanding.
- Skill retrieval ranks sensibly, large packs truncate and list their files, and
  `../..` out of a pack is refused.
- `doctor` reaches the live LLM endpoint, lists the models it offers, and warns
  when `LLM_MODEL` is not one of them.
- The data kernel degrades to a clear `UNAVAILABLE` with the install hint when
  its dependencies are absent, rather than crashing.
- 83/83 tests pass offline.

### Not yet verified

The live audio path still needs real credentials: an actual LiveKit room join,
Deepgram transcripts, and Cartesia audio. Everything up to the room join is
exercised. The prewarm/entrypoint split in particular has not been run against a
real dispatched job.
