---
name: magi
description: Convene the MAGI System — three Gemini-backed nodes (MELCHIOR-1 the Scientist, BALTHASAR-2 the Mother, CASPER-3 the Woman) that independently assess a problem, cross-examine each other over multiple rounds, then vote on a verdict. Use when the user types /magi, asks to "convene the council", "ask the MAGI", "get a second and third opinion", or wants multiple AI models to debate and check each other before committing to an answer. Also use for high-stakes calls — architecture decisions, irreversible or destructive operations, choosing between viable approaches, or a hard bug that already resisted one attempt.
---

# MAGI System

Three independent nodes, each on a **different** Gemini model with a different
charter, deliberate over N rounds and vote. You are the **fourth voice** — the
synthesis layer. The council advises; you verify and act.

```
MELCHIOR-1   the Scientist   technical truth, correctness, mechanism
BALTHASAR-2  the Mother      risk, blast radius, six-month regret, SECURITY
CASPER-3     the Woman       pragmatism, simplicity, plain language
```

## Security and plain language ride along free

Two standing concerns are built into the existing charters rather than into
extra nodes — so both run on **every** invocation of both modes, at no extra
API call and no extra latency.

**BALTHASAR-2 holds security adversarially.** Secret and API-key exposure (in
files, logs, env vars, git history, shell arguments, transcripts), money loss,
`curl | bash` and unvetted dependencies, widened permissions, personal data
leaving the machine, and the second hop after a compromise. It is explicitly
instructed to say "no security surface here" in one sentence and move on when
there is none — a manufactured finding trains the operator to ignore the real
one. Verified: it approves a benign variable rename at confidence 100 with no
conditions raised.

**CASPER-3 is the translator.** Its verdict block carries a `PLAIN_TERMS`
field, rendered as `## In plain terms` at the top of every digest — what was
decided, why it matters, what happens next, in ordinary words. Every node
emits the field, so if CASPER-3 goes OFFLINE the summary degrades to another
node's wording instead of vanishing.

A fourth "Guardian" node and a separate simplifier pass were both proposed and
**rejected unanimously by the council itself** — they would have cost 33% more
calls, made a 2-2 `DEADLOCK` reachable for the first time, and split one job
across two nodes. The transcript is at
`~/.claude/magi/sessions/20260905-233755-matter-should-magi-gain-a-security-node/`.

## Two modes — choose deliberately

### `review` — ~10s, 3 calls — **prefer this**

You have already drafted something and the council attacks it before you act.
One parallel round, no cross-examination needed: the draft is the shared
context, so the nodes are already grounded in the same concrete thing.

**`review` takes ANY text, not just diffs.** A plan, a design proposal, a
migration script, a shell command, a paragraph arguing for a decision — anything
you can put in a file or pipe on stdin. `--diff` is a convenience shortcut, not
the mode's purpose.

```bash
python ~/.claude/skills/magi/scripts/magi.py review -f plan.md      # any text
python ~/.claude/skills/magi/scripts/magi.py review --diff          # git diff HEAD
echo "Proposal: shard the users table by tenant_id" | \
  python ~/.claude/skills/magi/scripts/magi.py review               # stdin
```

Use for: destructive or irreversible commands, migrations, auth and crypto code,
anything about to ship. Highest signal per second in the system.

### `deliberate` — ~45s, 9 calls, 3 rounds — for genuinely open questions

| Round | Phase | What happens |
|---|---|---|
| 1 | Independent assessment | Each node answers alone, in isolation. No cross-talk. |
| 2 | Cross-examination | Each sees the others' output. Must challenge specifics, concede what it got wrong, revise. |
| 3 | Convergence and vote | Each emits a structured verdict: recommendation, vote, confidence, conditions, top risk, dissent. |

`--rounds 4..6` inserts extra cross-examination rounds; `--rounds 2` skips
straight from assessment to vote.

```bash
python ~/.claude/skills/magi/scripts/magi.py deliberate -f question.md -r 3
```

**Draft first, then have it attacked** — that beats asking three blind models to
invent an answer from nothing. Reach for `deliberate` only when the decision is
genuinely open and the trade-offs are contested.

Either way the engine tallies votes into `UNANIMOUS APPROVAL`,
`CONDITIONAL APPROVAL`, `MAJORITY APPROVAL`, `REJECTED`, or `DEADLOCK`.

## How to run it

Write the matter to a file first — never inline a long prompt as a shell
argument, quoting will bite you.

Steps:

1. **Compose the brief.** Write the question to a scratch file. Include the real
   constraints: language, framework, versions, what has already been tried, what
   the user actually cares about. A vague brief produces three vague opinions.
   The nodes **cannot see the filesystem** — anything they need must be in the
   brief or passed with `-c`.
2. **Attach evidence** with `-c <file>` (repeatable) for relevant source files.
   Each is truncated at 20k chars.
3. **Run it.** Takes 30–90s for 3 rounds. Progress prints to stderr; the digest
   prints to stdout.
4. **Synthesize.** Read the digest and act — see below.

Useful flags:

- `-r 2` quick call (assess + vote, no cross-examination)
- `-r 4` / `-r 5` deeper argument, for genuinely hard calls
- `-o <dir>` pin the output location
- `-q` suppress the progress banner

## Your job as the synthesis layer

**Do not paste the digest back at the user and call it done.** That is the one
failure mode this system exists to avoid. You must:

1. **State the consensus** and what it means for the task at hand.
2. **Verify the claims.** The nodes are blind to the filesystem and will
   confidently assert things about code they have never seen. Check their
   factual claims — file paths, APIs, library behaviour — against reality with
   your own tools. Say plainly when a node is wrong.
3. **Resolve the disagreements.** Where nodes split, you break the tie with
   evidence they did not have. Explain which side you took and why.
4. **Carry forward** every unmet `CONDITION` and unresolved `TOP_RISK` into the
   plan or the work.
5. **Then do the work.** MAGI decides *what*; you execute it.

On `DEADLOCK` or `REJECTED`, stop and bring it to the user with the specific
split, rather than picking a side silently.

Report it to the user compactly — the verdict, the interesting disagreement, and
your ruling. Link the transcript rather than reproducing it.

## Configuration

```bash
python ~/.claude/skills/magi/scripts/magi.py mode           # show current mode
python ~/.claude/skills/magi/scripts/magi.py mode always    # auto-convene
python ~/.claude/skills/magi/scripts/magi.py mode ask       # ask first (default)
python ~/.claude/skills/magi/scripts/magi.py mode off       # only on /magi
python ~/.claude/skills/magi/scripts/magi.py doctor         # probe models
python ~/.claude/skills/magi/scripts/magi.py doctor --assign  # auto-repair models
```

`mode` rewrites the managed block in `~/.claude/CLAUDE.md`, which is what makes
the policy apply to every future conversation. When the user asks to change when
MAGI fires, run the command — do not hand-edit `CLAUDE.md`.

Config lives at `~/.claude/magi/config.json`: per-node models, fallbacks and
`temperature`, plus `rounds`, `timeout_seconds`, and an optional `api_keys`
array (otherwise `GEMINI_API_KEY` / `GEMINI_API_KEY_2..4` from the environment
are used, round-robined across retries).

**Per-node temperature** matches sampling entropy to the charter — MELCHIOR-1 at
`0.35` for rigor, BALTHASAR-2 at `0.6`, CASPER-3 at `0.95` for lateral moves.
Raising MELCHIOR-1's temperature makes it less reliable at its actual job.

**Known-unreliable models.** `gemini-3.7-flash` and `gemini-flash-latest`
intermittently return 503 and run 4–10x slower than alternatives (measured:
27.8s / 17.9s / 503 / 64.8s on a trivial prompt). They are listed in
`UNRELIABLE_MODELS` in the engine; `doctor --assign` will not hand them to a
node, and `load_config` self-heals any config that still pins one. If Google
stabilises them, remove them from that set.

Sessions are archived under `~/.claude/magi/sessions/<timestamp>-<slug>/` as
`digest.md`, `transcript.md`, and `session.json`.

## Failure behaviour

- A node that errors is marked **OFFLINE**; deliberation continues with the rest.
- Below two live nodes, the session halts — no quorum.
- Each node falls back through its configured backup models, and retries
  429/5xx with backoff.
- If a Pro model shows `quota exhausted` in `doctor`, that is the API key's
  free-tier limit, not a bug. Flash models carry the council fine.
