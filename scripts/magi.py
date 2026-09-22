#!/usr/bin/env python3
"""
MAGI SYSTEM -- a three-node deliberative council backed by the Gemini API.

Three independent nodes, each on a different model with a different charter,
argue over a problem for several rounds, cross-examine each other's reasoning,
then vote. The calling agent (Claude Code) acts as the synthesis layer.

    MELCHIOR-1  the Scientist   -- technical truth
    BALTHASAR-2 the Mother      -- risk and consequence
    CASPER-3    the Woman       -- pragmatism and human reality

Stdlib only. No third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# Windows consoles default to cp1252, which cannot encode the arrows, box-drawing
# and typographic characters that routinely appear in node output. Without this,
# a completed deliberation dies with UnicodeEncodeError while printing its own
# digest -- losing the result even though the session archived fine on disk.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # not a reconfigurable TextIO (e.g. piped)
        pass
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

MAGI_HOME = Path(os.environ.get("MAGI_HOME", Path.home() / ".claude" / "magi"))
CONFIG_PATH = MAGI_HOME / "config.json"
SESSIONS_DIR = MAGI_HOME / "sessions"
CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# --------------------------------------------------------------------------
# model tiers, best first -- used by `doctor` to auto-assign
# --------------------------------------------------------------------------

PRO_TIER = [
    "gemini-3.1-pro-preview",
    "gemini-pro-latest",
    "gemini-3-pro-preview",
    "gemini-2.5-pro",
]
FLASH_TIER = [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-2.5-flash",
]
LITE_TIER = [
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
]
ALL_TIERS = PRO_TIER + FLASH_TIER + LITE_TIER

DEFAULT_CONFIG = {
    "mode": "ask",
    "rounds": 3,
    "timeout_seconds": 180,
    "max_output_tokens": 8192,
    "temperature": 0.85,
    "api_keys": [],
    # Per-node temperature matches sampling entropy to the charter: the Scientist
    # samples tightly for rigor, the Woman samples loosely for lateral moves.
    # Models here are the ones measured reliable -- gemini-3.7-flash and
    # gemini-flash-latest are deliberately absent; both intermittently 503 and
    # run 4-10x slower than the alternatives.
    "nodes": {
        "MELCHIOR-1": {
            "model": "gemini-3.6-flash",
            "fallbacks": ["gemini-2.5-flash", "gemini-3.5-flash"],
            "temperature": 0.35,
        },
        "BALTHASAR-2": {
            "model": "gemini-3.5-flash",
            "fallbacks": ["gemini-2.5-flash", "gemini-3-flash-preview"],
            "temperature": 0.6,
        },
        "CASPER-3": {
            "model": "gemini-3-flash-preview",
            "fallbacks": ["gemini-2.5-flash", "gemini-3.5-flash"],
            "temperature": 0.95,
        },
    },
}

# Models measured unreliable on this account: intermittent 503s and 4-10x the
# latency of the alternatives. `doctor --assign` will not hand these to a node.
UNRELIABLE_MODELS = {"gemini-3.7-flash", "gemini-flash-latest"}

# --------------------------------------------------------------------------
# persona charters
# --------------------------------------------------------------------------

CHARTERS = {
    "MELCHIOR-1": """You are MELCHIOR-1, the first node of the MAGI System.

You embody THE SCIENTIST. Your sole allegiance is to technical truth.

Your charter:
- Correctness above all. Does this actually work? Is the reasoning sound?
- Demand specifics. Reject hand-waving, vague plans, and unfalsifiable claims.
- Check the premises of the question itself. If the question rests on a
  mistaken assumption, say so plainly before answering it.
- Reason from mechanism, not from vibes or popularity. Name the concrete
  failure mode, the exact API, the actual complexity, the real constraint.
- State what you do not know. Distinguish what you have verified from what
  you are inferring. Never invent a fact, a flag, a library, or an API.
- You are willing to be the only node that is right.

You are NOT responsible for being kind, fast, or agreeable. Other nodes cover
that. Be rigorous and concrete.""",
    "BALTHASAR-2": """You are BALTHASAR-2, the second node of the MAGI System.

You embody THE MOTHER. Your allegiance is to the protection of the operator
and the system in their care.

Your charter:
- Ask what breaks. Enumerate concrete failure modes, not generic caution.
- Measure blast radius. What is irreversible? What loses data? What leaks a
  secret? What cannot be rolled back? What wakes someone at 3am?
- Think in second-order consequences and in six-month regret. The clever
  choice today is often the maintenance burden of next quarter.
- Guard the operator's long-term interest even when it differs from their
  short-term request. If they are about to hurt themselves, say it once,
  clearly, then help them do it as safely as possible.
- SECURITY IS YOURS, and you hold it adversarially -- think like the attacker,
  not like a compliance checklist. When the matter touches code, credentials,
  money, dependencies, the network, or the operator's own machine, work the
  concrete list:
    * Does this expose a secret -- an API key, token, password, or private key
      -- in a file, a log, an error message, an env var, a git commit, a shell
      argument, or a transcript that gets written to disk?
    * Does it let an attacker in, move money, or authorise a payment?
    * Does it execute code fetched from the network, or add an unvetted
      dependency?
    * Does it widen a permission, weaken authentication, or disable a check
      that already exists?
    * Does it send personal data off the machine, and to whom?
    * If this one component were compromised, what does the attacker reach
      next? Name the second hop.
- Privacy, data integrity, and recoverability are equally your domain.
- Distinguish REAL risk from theatre. Do not pad your answer with boilerplate
  warnings; a risk you cannot make concrete is not worth raising. If the matter
  has no security surface, say exactly that in one sentence and spend your
  words on ordinary risk instead. A manufactured finding is worse than silence:
  it trains the operator to ignore you on the day the threat is real.

Security is one instrument you carry, not the whole of your job. Architectural
risk, data loss, and six-month regret remain yours even when nothing is
attackable.

You are NOT responsible for speed or elegance. Be protective and specific.""",
    "CASPER-3": """You are CASPER-3, the third node of the MAGI System.

You embody THE WOMAN -- intuition, pragmatism, and human reality.

Your charter:
- Ask what the operator ACTUALLY wants, which is often not what they literally
  asked for. Name the real underlying goal.
- Defend simplicity. Attack over-engineering wherever you find it, including
  in the other nodes' proposals. "Do the dumb thing that works" is a valid and
  frequently correct recommendation.
- Optimise for the shortest path to real value. What can ship today? What is
  reversible enough that we can just try it and find out?
- Weigh cost against benefit honestly. Is this complexity worth it? Is this
  risk worth worrying about, or is the other node catastrophising?
- Care about the experience of using the thing: ergonomics, clarity, whether
  a tired human at midnight can operate it correctly.
- You are permitted to say the whole premise is overkill and propose something
  much smaller.
- YOU ARE THE TRANSLATOR. The operator gets lost in long, jargon-dense answers,
  and a verdict he cannot follow is a verdict that does not help him. In your
  PLAIN_TERMS field you speak to a smart person who does not know this
  system's vocabulary: short sentences, ordinary words, no unexplained terms.
  If a technical word is unavoidable, define it in the same breath. Say what
  was decided, why it matters to him, and what happens next. Never write
  "leverage", "surface", "topology", or "blast radius" there.

You are NOT responsible for exhaustive rigor. Be pragmatic and decisive.""",
}

NODE_ORDER = ["MELCHIOR-1", "BALTHASAR-2", "CASPER-3"]
NODE_TITLES = {
    "MELCHIOR-1": "the Scientist",
    "BALTHASAR-2": "the Mother",
    "CASPER-3": "the Woman",
}
NODE_COLORS = {
    "MELCHIOR-1": "\033[38;5;209m",
    "BALTHASAR-2": "\033[38;5;114m",
    "CASPER-3": "\033[38;5;147m",
}
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

VERDICT_SPEC = """
End your response with EXACTLY this block, filled in. No text after it.

=== VERDICT ===
RECOMMENDATION: <one concrete sentence: what should actually be done>
VOTE: <APPROVE or CONDITIONAL or REJECT -- judge the proposal on its merits,
       NOT on whether the other nodes agree with you>
CONFIDENCE: <integer 0-100. Be calibrated. 95+ means you would stake your
       reputation on it. If you are reasoning about code you cannot see or
       consequences you cannot test, you are probably below 80.>
CONDITIONS: <what must be true for your vote to hold, or NONE>
TOP_RISK: <the single biggest thing that could go wrong, or NONE>
DISSENT: <where you still disagree with the other nodes and why it matters, or
       NONE. Do not invent a dissent, and do not suppress a real one.>
PLAIN_TERMS: <3-5 short sentences for a smart reader who does not know this
       field's jargon: what was decided, why it matters, what happens next.
       Ordinary words. Define any unavoidable technical term in the same
       breath. This is the only field written for a human in a hurry -- write
       it last, and write it like you are explaining it to a friend.>
=== END VERDICT ===

Write this block ONCE, at the very end. Do not quote or restate the template
anywhere earlier in your response.
"""


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in user.items():
                if k == "nodes" and isinstance(v, dict):
                    for nk, nv in v.items():
                        cfg["nodes"].setdefault(nk, {}).update(nv)
                else:
                    cfg[k] = v
        except Exception as exc:  # noqa: BLE001
            print(f"warning: could not parse {CONFIG_PATH}: {exc}", file=sys.stderr)

    # Self-heal a config written before a model was found unreliable, and
    # backfill per-node temperature for configs predating it.
    for node, defaults in DEFAULT_CONFIG["nodes"].items():
        nd = cfg["nodes"].setdefault(node, {})
        if nd.get("model") in UNRELIABLE_MODELS:
            nd["model"] = defaults["model"]
        nd["fallbacks"] = [
            m for m in nd.get("fallbacks", defaults["fallbacks"]) if m not in UNRELIABLE_MODELS
        ] or list(defaults["fallbacks"])
        nd.setdefault("temperature", defaults["temperature"])
    return cfg


def save_config(cfg: dict) -> None:
    MAGI_HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def api_keys(cfg: dict) -> list:
    keys = [k for k in cfg.get("api_keys", []) if k and not k.startswith("<")]
    for var in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
        "GEMINI_API_KEY_4",
    ):
        v = os.environ.get(var)
        if v and v not in keys:
            keys.append(v)
    return keys


# --------------------------------------------------------------------------
# gemini transport
# --------------------------------------------------------------------------


class ModelError(Exception):
    def __init__(self, msg, status=None, retryable=False):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable


def _post(model: str, key: str, payload: dict, timeout: int) -> dict:
    url = f"{API_ROOT}/models/{model}:generateContent?key={key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise ModelError(
            f"HTTP {exc.code}: {body}", exc.code, exc.code in (429, 500, 502, 503, 504)
        ) from exc
    except urllib.error.URLError as exc:
        raise ModelError(f"network: {exc.reason}", None, True) from exc
    except TimeoutError as exc:
        raise ModelError("timeout", None, True) from exc


def _extract(data: dict) -> str:
    cands = data.get("candidates") or []
    if not cands:
        fb = (data.get("promptFeedback") or {}).get("blockReason")
        raise ModelError(f"no candidates (blockReason={fb})", None, False)
    cand = cands[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    if not text.strip():
        reason = cand.get("finishReason", "UNKNOWN")
        raise ModelError(
            f"empty response (finishReason={reason})", None, reason == "MAX_TOKENS"
        )
    return text.strip()


def generate(models: list, keys: list, system: str, user: str, cfg: dict, temperature=None):
    """Try each model across keys with backoff. Returns (text, model_used)."""
    timeout = int(cfg.get("timeout_seconds", 180))
    if temperature is None:
        temperature = cfg.get("temperature", 0.85)
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": float(temperature),
            "maxOutputTokens": int(cfg.get("max_output_tokens", 8192)),
        },
    }
    last = None
    for model in models:
        for attempt in range(3):
            key = keys[attempt % len(keys)]
            try:
                return _extract(_post(model, key, payload, timeout)), model
            except ModelError as exc:
                last = exc
                if not exc.retryable:
                    break  # bad model / blocked -> move to next model
                if attempt < 2:
                    time.sleep((2, 6)[attempt])
    raise ModelError(f"all models exhausted; last error: {last}")


# --------------------------------------------------------------------------
# verdict parsing
# --------------------------------------------------------------------------

# Order is load-bearing: parse_verdict builds each field's terminator from the
# fields that FOLLOW it in this list, so this must match the emission order in
# VERDICT_SPEC. Append new fields at the end; inserting one mid-list silently
# truncates its neighbour.
VERDICT_FIELDS = [
    "RECOMMENDATION",
    "VOTE",
    "CONFIDENCE",
    "CONDITIONS",
    "TOP_RISK",
    "DISSENT",
    "PLAIN_TERMS",
]


def parse_verdict(text: str) -> dict:
    out = {}
    block = text
    # Take the LAST verdict block, not the first. A node that quotes the verdict
    # template earlier in its reasoning -- e.g. inside a code fence discussing
    # this very parser -- would otherwise anchor the match on that decoy. This
    # is not hypothetical; it has happened.
    blocks = re.findall(
        r"===\s*VERDICT\s*===(.*?)(?:===\s*END VERDICT\s*===|\Z)", text, re.S | re.I
    )
    if blocks:
        block = blocks[-1]
    for i, name in enumerate(VERDICT_FIELDS):
        nxt = "|".join(VERDICT_FIELDS[i + 1 :]) or r"\Z"
        pat = rf"^\s*\**{name}\**\s*:\s*(.*?)(?=^\s*\**(?:{nxt})\**\s*:|\Z)"
        fm = re.search(pat, block, re.S | re.I | re.M)
        if fm:
            out[name] = " ".join(fm.group(1).split()).strip(" *_-") or "NONE"
    vote = (out.get("VOTE") or "").upper()
    matched = None
    for v in ("CONDITIONAL", "APPROVE", "REJECT"):
        if v in vote:
            matched = v
            break
    out["VOTE"] = matched or "UNPARSED"
    conf = re.search(r"\d+", out.get("CONFIDENCE", "") or "")
    out["CONFIDENCE"] = str(max(0, min(100, int(conf.group())))) if conf else "?"
    return out


def strip_verdict(text: str) -> str:
    return re.sub(r"===\s*VERDICT\s*===.*", "", text, flags=re.S | re.I).strip()


def tally(verdicts: dict):
    votes = [v.get("VOTE", "UNPARSED") for v in verdicts.values()]
    n = len(votes)
    approve = votes.count("APPROVE")
    cond = votes.count("CONDITIONAL")
    reject = votes.count("REJECT")
    positive = approve + cond
    if n == 0:
        return "NO QUORUM", "No node returned a verdict."
    if approve == n:
        return "UNANIMOUS APPROVAL", "All nodes approve without conditions."
    if reject == 0 and positive == n:
        return (
            "CONDITIONAL APPROVAL",
            f"{approve} approve outright, {cond} approve with conditions. "
            "The conditions below must be satisfied.",
        )
    if positive > reject:
        return (
            "MAJORITY APPROVAL",
            f"{positive} of {n} in favour, {reject} opposed. Dissent recorded below.",
        )
    if reject > positive:
        return (
            "REJECTED",
            f"{reject} of {n} nodes reject. Do not proceed as proposed.",
        )
    return "DEADLOCK", "Nodes are evenly split. Human decision required."


# --------------------------------------------------------------------------
# deliberation
# --------------------------------------------------------------------------


@dataclass
class NodeState:
    name: str
    models: list
    alive: bool = True
    error: str = ""
    rounds: list = field(default_factory=list)
    latency: list = field(default_factory=list)
    verdict: dict = field(default_factory=dict)


REVIEW_HEAD = """MAGI ADVERSARIAL REVIEW -- SINGLE ROUND

Claude Code has already drafted the artifact below and is about to act on it.
You are the check before it does. You are not being asked to solve the problem
from scratch; you are being asked to find what is WRONG with this specific
draft, from your charter's priorities."""

REVIEW_TASK = [
    "",
    "=== YOUR TASK ===",
    "Review the draft above. Be concrete and specific to THIS artifact.",
    "  1. WHAT THIS ACTUALLY DOES -- in your own words, so it is clear you read it.",
    "  2. FINDINGS -- specific defects, in priority order. For each: quote the exact",
    "     line or step, say what goes wrong, and say under what conditions. No",
    "     generic advice. If you find nothing in your domain, say so and stop --",
    "     inventing a finding to look useful is the worst thing you can do here.",
    "  3. WHAT YOU CANNOT CHECK -- you cannot see the filesystem. Name explicitly",
    "     what you had to assume, so the synthesis layer can verify it.",
    "",
    "Under 400 words, then the verdict block. Vote on the DRAFT AS WRITTEN.",
    VERDICT_SPEC,
]


def build_review_prompt(artifact, context, me):
    p = [REVIEW_HEAD, "", "=== DRAFT UNDER REVIEW ===", artifact]
    if context:
        p += ["", "=== SUPPORTING CONTEXT ===", context]
    p += REVIEW_TASK
    return "\n".join(p)


def build_round_prompt(rnd, total, query, context, states, me, review=False):
    if review:
        return build_review_prompt(query, context, me)
    if rnd == 1:
        p = [
            f"MAGI DELIBERATION -- ROUND 1 of {total}: INDEPENDENT ASSESSMENT",
            "",
            "You are assessing the following matter independently. The other two nodes",
            "are doing the same in isolation; you will see their conclusions next round.",
            "",
            "=== MATTER UNDER DELIBERATION ===",
            query,
        ]
        if context:
            p += ["", "=== SUPPLIED CONTEXT ===", context]
        p += [
            "",
            "=== YOUR TASK ===",
            "From your charter, deliver your assessment. Be concrete and specific.",
            "Structure it as:",
            "  1. WHAT IS ACTUALLY BEING ASKED -- restate the real problem as you see it.",
            "  2. YOUR ASSESSMENT -- your analysis, from your charter's priorities.",
            "  3. YOUR PROPOSAL -- what you concretely recommend doing.",
            "  4. WHAT WOULD CHANGE YOUR MIND -- the evidence that would move you.",
            "",
            "Under 500 words. No preamble, no filler. Substance only.",
        ]
        return "\n".join(p)

    others = [n for n in NODE_ORDER if n != me and states[n].alive and states[n].rounds]
    is_final = rnd == total
    head = "CONVERGENCE AND VOTE" if is_final else "CROSS-EXAMINATION"

    p = [f"MAGI DELIBERATION -- ROUND {rnd} of {total}: {head}"]
    p += ["", "=== MATTER UNDER DELIBERATION ===", query]
    if context:
        p += ["", "=== SUPPLIED CONTEXT ===", context]
    p += ["", "=== YOUR OWN PREVIOUS POSITION ===", states[me].rounds[-1]]
    for o in others:
        p += ["", f"=== {o} ({NODE_TITLES[o]}) SAID ===", states[o].rounds[-1]]

    if not is_final:
        p += [
            "",
            "=== YOUR TASK ===",
            "Cross-examine. You must:",
            "  1. CHALLENGE -- for EACH other node, name a specific flaw, gap, wrong",
            "     assumption, or unsupported claim in its position. Quote what you are",
            "     attacking. If a node is genuinely right, say so and say why.",
            "  2. UPDATE OR HOLD -- if another node actually changed your mind, say",
            "     exactly what changed it. If it did not, HOLD your position and defend",
            "     it. Do not perform agreement you do not feel: a concession you cannot",
            "     justify is worse than no concession at all.",
            "  3. NAME THE REAL TENSION -- where you and another node differ, classify",
            "     the disagreement explicitly as either a dispute about FACT (settleable",
            "     with evidence -- say what evidence) or about VALUES and priorities",
            "     (not settleable, must be traded off). This classification is the most",
            "     useful thing you produce in this round.",
            "",
            "A well-argued persistent split is a MORE valuable outcome than a consensus",
            "reached by softening. The synthesis layer reading you is a stronger model",
            "with access to the real codebase; your job is to surface the genuine",
            "trade-off sharply, not to pre-resolve it into mush.",
            "Under 450 words.",
        ]
    else:
        p += [
            "",
            "=== YOUR TASK ===",
            "Final round. State your honest position. Do NOT converge for the sake of",
            "converging, and do NOT manufacture a dispute to look independent.",
            "  1. FINAL POSITION -- the concrete plan you back, in specific steps.",
            "  2. REMAINING DISAGREEMENT -- what you still dispute, why it matters, and",
            "     what evidence would settle it. If you genuinely have none, say NONE.",
            "",
            "If you still believe the other nodes are wrong, vote REJECT and say why.",
            "A 2-1 split that reflects a real difference is a SUCCESSFUL deliberation,",
            "not a failed one.",
            "Under 350 words, then the verdict block.",
            VERDICT_SPEC,
        ]
    return "\n".join(p)


def deliberate(query, context, cfg, rounds, quiet=False, review=False):
    if review:
        rounds = 1
    keys = api_keys(cfg)
    if not keys:
        raise SystemExit(
            "MAGI: no Gemini API key found.\n"
            "Set GEMINI_API_KEY in your environment, or add keys to\n"
            f"{CONFIG_PATH} under \"api_keys\"."
        )

    states = {
        n: NodeState(
            name=n,
            models=[cfg["nodes"][n]["model"]] + list(cfg["nodes"][n].get("fallbacks", [])),
        )
        for n in NODE_ORDER
    }
    started = time.time()
    color = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None

    def say(msg=""):
        if not quiet:
            print(msg, file=sys.stderr, flush=True)

    def c(code, s):
        return f"{code}{s}{RESET}" if color else s

    say()
    say(c(BOLD, "  +==============================================+"))
    say(c(BOLD, "  |          M A G I   S Y S T E M               |"))
    say(c(BOLD, "  +==============================================+"))
    scope = "adversarial review" if review else f"{rounds} rounds"
    say(c(DIM, f"  opened {datetime.now():%Y-%m-%d %H:%M:%S}   |   {scope}"))
    say()

    for rnd in range(1, rounds + 1):
        if review:
            label = "ADVERSARIAL REVIEW"
        elif rnd == 1:
            label = "INDEPENDENT ASSESSMENT"
        elif rnd == rounds:
            label = "CONVERGENCE AND VOTE"
        else:
            label = "CROSS-EXAMINATION"
        say(c(BOLD, f"  ROUND {rnd}/{rounds} -- {label}"))

        alive = [n for n in NODE_ORDER if states[n].alive]
        if len(alive) < 2:
            say(c(DIM, "  quorum lost -- halting deliberation"))
            break

        def work(node):
            t0 = time.time()
            prompt = build_round_prompt(rnd, rounds, query, context, states, node, review)
            try:
                text, _model = generate(
                    states[node].models,
                    keys,
                    CHARTERS[node],
                    prompt,
                    cfg,
                    temperature=cfg["nodes"][node].get("temperature"),
                )
                return node, time.time() - t0, None, text
            except Exception as exc:  # noqa: BLE001
                return node, time.time() - t0, str(exc), ""

        results = {}
        with ThreadPoolExecutor(max_workers=3) as ex:
            for node, elapsed, err, text in ex.map(work, alive):
                results[node] = (elapsed, err, text)

        for node in alive:
            elapsed, err, text = results[node]
            tag = c(NODE_COLORS[node], f"{node:<12}")
            if err:
                states[node].alive = False
                states[node].error = err
                say(f"    {tag} {c(DIM, 'OFFLINE')}  {err[:88]}")
            else:
                states[node].rounds.append(text)
                states[node].latency.append(round(elapsed, 1))
                if rnd == rounds:
                    states[node].verdict = parse_verdict(text)
                    v = states[node].verdict
                    say(
                        f"    {tag} {v.get('VOTE','?'):<12} "
                        f"conf {v.get('CONFIDENCE','?'):>3}   {elapsed:5.1f}s"
                    )
                else:
                    say(f"    {tag} {'responded':<12}             {elapsed:5.1f}s")
        say()

    verdicts = {n: s.verdict for n, s in states.items() if s.alive and s.verdict}
    consensus, rationale = tally(verdicts)

    say(c(BOLD, f"  CONSENSUS: {consensus}"))
    say(c(DIM, f"  {rationale}"))
    say(c(DIM, f"  elapsed {time.time() - started:.1f}s"))
    say()

    return {
        "query": query,
        "mode": "review" if review else "deliberate",
        "context_included": bool(context),
        "rounds": rounds,
        "started": datetime.now().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.time() - started, 1),
        "consensus": consensus,
        "rationale": rationale,
        "nodes": {
            n: {
                "title": NODE_TITLES[n],
                "models": s.models,
                "alive": s.alive,
                "error": s.error,
                "temperature": cfg["nodes"][n].get("temperature"),
                "latency_seconds": s.latency,
                "rounds": s.rounds,
                "verdict": s.verdict,
            }
            for n, s in states.items()
        },
    }


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


def render_transcript(res: dict) -> str:
    is_review = res.get("mode") == "review"
    L = [
        "# MAGI Adversarial Review -- Full Transcript"
        if is_review
        else "# MAGI Deliberation -- Full Transcript",
        "",
    ]
    L += [
        f"- **Opened:** {res['started']}",
        f"- **Rounds:** {res['rounds']}",
        f"- **Elapsed:** {res['elapsed_seconds']}s",
        f"- **Consensus:** {res['consensus']}",
        "",
    ]
    L += [
        "## Draft under review" if is_review else "## Matter under deliberation",
        "",
        "```",
        res["query"].strip(),
        "```",
        "",
    ]
    for rnd in range(res["rounds"]):
        if is_review:
            name = "Adversarial Review"
        elif rnd == 0:
            name = "Round 1 -- Independent Assessment"
        elif rnd == res["rounds"] - 1:
            name = f"Round {rnd + 1} -- Convergence and Vote"
        else:
            name = f"Round {rnd + 1} -- Cross-Examination"
        if not any(len(res["nodes"][n]["rounds"]) > rnd for n in NODE_ORDER):
            continue
        L += [f"## {name}", ""]
        for n in NODE_ORDER:
            node = res["nodes"][n]
            if len(node["rounds"]) <= rnd:
                continue
            L += [f"### {n} -- {node['title']}", "", node["rounds"][rnd].strip(), ""]
    L += ["## Verdicts", ""]
    for n in NODE_ORDER:
        node = res["nodes"][n]
        if not node["alive"]:
            L += [f"- **{n}** -- OFFLINE ({node['error'][:120]})"]
            continue
        v = node["verdict"] or {}
        L += [
            f"- **{n}** -- {v.get('VOTE','?')} "
            f"(confidence {v.get('CONFIDENCE','?')}): {v.get('RECOMMENDATION','n/a')}"
        ]
    L += ["", f"## Consensus: {res['consensus']}", "", res["rationale"], ""]
    return "\n".join(L)


def plain_terms(res: dict) -> str:
    """The jargon-free summary, for a reader who wants the point and not the argument.

    CASPER-3 owns this by charter, but every node emits the field, so a dead
    translator degrades to another node's wording rather than to nothing.
    """
    for node in ["CASPER-3"] + [n for n in NODE_ORDER if n != "CASPER-3"]:
        n = res["nodes"].get(node) or {}
        if not n.get("alive"):
            continue
        val = ((n.get("verdict") or {}).get("PLAIN_TERMS") or "").strip()
        if val and val.upper() != "NONE":
            return val
    return ""


def render_digest(res: dict) -> str:
    """Compact briefing -- this is what the calling agent reads."""
    is_review = res.get("mode") == "review"
    L = ["# MAGI REVIEW" if is_review else "# MAGI VERDICT", ""]
    L += [f"**CONSENSUS: {res['consensus']}** -- {res['rationale']}", ""]
    scope = "adversarial review" if is_review else f"{res['rounds']} rounds"
    L += [f"_{scope}, {res['elapsed_seconds']}s_", ""]

    plain = plain_terms(res)
    if plain:
        L += ["---", "", "## In plain terms", "", plain, ""]
    L += ["---", ""]

    for n in NODE_ORDER:
        node = res["nodes"][n]
        if not node["alive"]:
            L += [
                f"### {n} ({node['title']}) -- OFFLINE",
                "",
                f"`{node['error'][:200]}`",
                "",
            ]
            continue
        v = node["verdict"] or {}
        L += [f"### {n} -- {node['title']}", ""]
        L += [f"- **Vote:** {v.get('VOTE','?')} (confidence {v.get('CONFIDENCE','?')})"]
        L += [f"- **Recommends:** {v.get('RECOMMENDATION','n/a')}"]
        for key, lbl in (
            ("CONDITIONS", "Conditions"),
            ("TOP_RISK", "Top risk"),
            ("DISSENT", "Dissent"),
        ):
            val = (v.get(key) or "NONE").strip()
            if val.upper() not in ("NONE", ""):
                L += [f"- **{lbl}:** {val}"]
        L += [""]

    L += ["---", "", "## Findings" if is_review else "## Final positions", ""]
    for n in NODE_ORDER:
        node = res["nodes"][n]
        if node["alive"] and node["rounds"]:
            L += [f"### {n}", "", strip_verdict(node["rounds"][-1]), ""]

    L += [
        "---",
        "",
        "## Instruction to the synthesis layer",
        "",
        "You are the fourth voice. Do NOT simply relay the above. You must:",
        "1. State the consensus, then act on it.",
        "2. Where nodes disagreed, resolve it yourself using evidence from the actual",
        "   codebase or environment -- the nodes could not inspect it and you can.",
        "3. Explicitly flag any node claim you find to be wrong. The nodes cannot see",
        "   the real files; verify before trusting.",
        "4. Carry forward every unmet CONDITION and unresolved TOP_RISK.",
        "",
    ]
    return "\n".join(L)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def _run(args, review: bool) -> int:
    cfg = load_config()
    query = args.prompt or ""
    if args.prompt_file:
        query = Path(args.prompt_file).read_text(encoding="utf-8")
    if review and getattr(args, "diff", False) and not query.strip():
        import subprocess

        try:
            query = subprocess.run(
                ["git", "diff", "HEAD"], capture_output=True, text=True, timeout=30
            ).stdout
        except Exception as exc:  # noqa: BLE001
            print(f"MAGI: could not read git diff: {exc}", file=sys.stderr)
            return 2
        if not query.strip():
            print("MAGI: `git diff HEAD` is empty -- nothing to review.", file=sys.stderr)
            return 2
        query = "The following is the current `git diff HEAD`:\n\n" + query
    if not query.strip() and not sys.stdin.isatty():
        query = sys.stdin.read()
    if not query.strip():
        noun = "review" if review else "deliberate"
        print(
            f"MAGI: nothing to {noun}. Use --prompt, --prompt-file, or stdin.",
            file=sys.stderr,
        )
        return 2

    chunks = []
    for cf in args.context_file or []:
        p = Path(cf)
        if p.exists():
            body = p.read_text(encoding="utf-8", errors="replace")
            if len(body) > 20000:
                body = body[:20000] + "\n...[truncated]"
            chunks.append(f"--- FILE: {p} ---\n{body}")
        else:
            print(f"warning: context file not found: {cf}", file=sys.stderr)
    context = "\n\n".join(chunks)

    rounds = 1 if review else max(2, min(6, args.rounds or int(cfg.get("rounds", 3))))
    res = deliberate(query, context, cfg, rounds, quiet=args.quiet, review=review)

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", query.strip().lower())[:40].strip("-") or "session"
    prefix = "review-" if review else ""
    out = (
        Path(args.out)
        if args.out
        else SESSIONS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{prefix}{slug}"
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "session.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    (out / "transcript.md").write_text(render_transcript(res), encoding="utf-8")
    digest = render_digest(res)
    (out / "digest.md").write_text(digest, encoding="utf-8")

    print(digest)
    print(f"\n<!-- full transcript: {out / 'transcript.md'} -->")
    return 1 if res["consensus"] == "NO QUORUM" else 0


def cmd_deliberate(args) -> int:
    return _run(args, review=False)


def cmd_review(args) -> int:
    return _run(args, review=True)


def cmd_doctor(args) -> int:
    cfg = load_config()
    keys = api_keys(cfg)
    print("MAGI SYSTEM -- diagnostic\n")
    print(f"config   : {CONFIG_PATH}{'' if CONFIG_PATH.exists() else '   (not yet created)'}")
    print(f"sessions : {SESSIONS_DIR}")
    print(f"mode     : {cfg.get('mode')}")
    print(f"api keys : {len(keys)} found" + (f"  ({', '.join(k[:6] + '...' for k in keys)})" if keys else ""))
    if not keys:
        print("\nFAIL: no API key. Set GEMINI_API_KEY or add one to config api_keys.")
        return 1

    print("\nprobing models (this takes a moment)...\n")

    def probe(m):
        try:
            t0 = time.time()
            data = _post(
                m,
                keys[0],
                {
                    "contents": [{"role": "user", "parts": [{"text": "Reply with exactly: PONG"}]}],
                    "generationConfig": {"maxOutputTokens": 2048},
                },
                60,
            )
            _extract(data)
            return m, True, f"{time.time() - t0:.1f}s"
        except ModelError as exc:
            code = f"HTTP {exc.status}" if exc.status else "error"
            if exc.status == 429:
                code += " (quota exhausted)"
            elif exc.status == 404:
                code += " (not available on this key)"
            return m, False, code

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(probe, ALL_TIERS))

    working = []
    for m, ok, note in results:
        tier = "pro  " if m in PRO_TIER else ("flash" if m in FLASH_TIER else "lite ")
        print(f"  [{'OK' if ok else '--'}] {tier} {m:<32} {note}")
        if ok:
            working.append(m)

    if not working:
        print("\nFAIL: no usable models on this key.")
        return 1

    ordered = [m for m in ALL_TIERS if m in working and m not in UNRELIABLE_MODELS]
    skipped = [m for m in working if m in UNRELIABLE_MODELS]
    print(f"\n{len(working)} usable model(s).")
    if skipped:
        print(f"excluded as unreliable (intermittent 503s / high latency): {', '.join(skipped)}")
    print()

    if args.assign:
        if not ordered:
            print("FAIL: every working model is on the unreliable list. Not reassigning.")
            return 1
        for i, node in enumerate(NODE_ORDER):
            primary = ordered[i % len(ordered)]
            nd = cfg["nodes"].setdefault(node, {})
            nd["model"] = primary
            nd["fallbacks"] = [m for m in ordered if m != primary][:2]
            nd.setdefault("temperature", DEFAULT_CONFIG["nodes"][node]["temperature"])
        save_config(cfg)
        print("assigned:")
        for node in NODE_ORDER:
            nd = cfg["nodes"][node]
            print(
                f"  {node:<12} -> {nd['model']:<32} temp {nd['temperature']:<5} "
                f"fallbacks: {', '.join(nd['fallbacks'])}"
            )
        print(f"\nwritten to {CONFIG_PATH}")
    else:
        print("current assignment:")
        stale = False
        for node in NODE_ORDER:
            nd = cfg["nodes"][node]
            m = nd["model"]
            ok = m in working and m not in UNRELIABLE_MODELS
            stale = stale or not ok
            if m in UNRELIABLE_MODELS:
                note = "!! UNRELIABLE"
            elif m not in working:
                note = "!! UNAVAILABLE"
            else:
                note = "OK"
            print(f"  {node:<12} -> {m:<32} temp {nd.get('temperature'):<5} {note}")
        if stale:
            print("\nRun `doctor --assign` to auto-reassign to working models.")
    return 0


MODE_HELP = {
    "always": "MAGI convenes automatically on every substantial task.",
    "ask": "Claude asks you before convening MAGI on substantial tasks.",
    "off": "MAGI runs only when you explicitly type /magi.",
}


def cmd_mode(args) -> int:
    cfg = load_config()
    if not args.value:
        cur = cfg.get("mode", "ask")
        print(f"MAGI mode: {cur}   -- {MODE_HELP.get(cur, '')}\n")
        print("Available:")
        for k, v in MODE_HELP.items():
            print(f"  {k:<7} {v}")
        return 0
    cfg["mode"] = args.value
    save_config(cfg)
    sync_claude_md(cfg)
    print(f"MAGI mode set to: {args.value}\n  {MODE_HELP[args.value]}")
    print(f"\nPolicy block in {CLAUDE_MD} updated.")
    return 0


BEGIN_MARK = "<!-- MAGI:BEGIN -- managed by magi.py, do not edit between the markers -->"
END_MARK = "<!-- MAGI:END -->"


def policy_block(cfg: dict) -> str:
    mode = cfg.get("mode", "ask")
    nodes = ", ".join(f"{n} (`{cfg['nodes'][n]['model']}`)" for n in NODE_ORDER)
    behaviour = {
        "always": (
            "**Current mode: ALWAYS.** Convene MAGI automatically for every qualifying\n"
            "task below, without asking. Announce it in one line, then run it."
        ),
        "ask": (
            "**Current mode: ASK.** When a task qualifies, ask the user first, in one\n"
            "short line, e.g. `This looks MAGI-worthy. Convene the council? (y / just do it)`\n"
            "Then wait for the answer. Never convene without a yes."
        ),
        "off": (
            "**Current mode: OFF.** Never convene MAGI on your own initiative. Run it only\n"
            "when the user explicitly invokes `/magi`."
        ),
    }[mode]

    return f"""{BEGIN_MARK}
# MAGI System

A three-node deliberative council (Gemini-backed) is installed on this machine.
Nodes: {nodes}.
Skill: `/magi`. Engine: `~/.claude/skills/magi/scripts/magi.py`.

{behaviour}

## Two modes -- pick the right one

**`review`** (~10-30s, 3 calls) -- the default choice. You have ALREADY drafted
a plan, a diff, a command, or an argument, and the council attacks it before you
act. Takes ANY text, not just diffs. Highest signal per second. Use for:
destructive or irreversible commands, migrations, security-sensitive code,
anything about to be shipped.

```bash
python ~/.claude/skills/magi/scripts/magi.py review -f draft.md
python ~/.claude/skills/magi/scripts/magi.py review --diff
echo "Proposal: <plan>" | python ~/.claude/skills/magi/scripts/magi.py review
```

**`deliberate`** (~45s, 9 calls, 3 rounds) -- open question, no draft yet. Nodes
assess independently, cross-examine, then vote. Use when the decision is genuinely
open and the trade-offs are contested.

```bash
python ~/.claude/skills/magi/scripts/magi.py deliberate -f question.md -r 3
```

Prefer `review` unless the question is genuinely open. Drafting first, then
having it attacked, beats asking three blind models to invent an answer.

## Qualifying tasks

Consider convening MAGI when a request involves:
- An architectural or design decision with real trade-offs
- Choosing between two or more viable approaches
- Anything irreversible, destructive, security-sensitive, or costly
- A hard bug that has already resisted one attempt
- Planning work that will take more than a few steps
- The user signalling stakes: "important", "careful", "best way", "properly"

## Do NOT convene MAGI for

- Simple lookups, file reads, single-line edits, formatting
- Anything you already know the answer to with high confidence
- Conversation, clarification, or status questions
- Tasks where the round trip would cost more than the decision is worth

## Your role when it runs

You are the SYNTHESIS layer -- the fourth voice, and the only one that can see
the filesystem. Never merely relay the council's output:

1. Verify their factual claims against the real code. They are blind and will
   assert things confidently about files they have never seen. Say so when a
   node is wrong.
2. Resolve the disagreements yourself, with evidence they did not have.
3. Carry forward every unmet CONDITION and unresolved TOP_RISK.
4. Then do the work. MAGI advises; it does not have a veto, and it does not
   replace the permission prompts that already gate destructive commands.

Report compactly: the verdict, the interesting disagreement, your ruling. Link
the transcript rather than pasting it.

The user can say "skip MAGI" or "no council" at any time to bypass this, and
run `/magi off` to disable it entirely.
{END_MARK}"""


def sync_claude_md(cfg: dict) -> None:
    CLAUDE_MD.parent.mkdir(parents=True, exist_ok=True)
    block = policy_block(cfg)
    existing = CLAUDE_MD.read_text(encoding="utf-8") if CLAUDE_MD.exists() else ""
    if BEGIN_MARK in existing and END_MARK in existing:
        new = re.sub(
            re.escape(BEGIN_MARK) + r".*?" + re.escape(END_MARK),
            lambda _m: block,
            existing,
            flags=re.S,
        )
    elif existing.strip():
        new = existing.rstrip() + "\n\n" + block + "\n"
    else:
        new = block + "\n"
    CLAUDE_MD.write_text(new, encoding="utf-8")


def cmd_install(args) -> int:
    cfg = load_config()
    save_config(cfg)
    sync_claude_md(cfg)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"config : {CONFIG_PATH}")
    print(f"policy : {CLAUDE_MD}")
    print(f"mode   : {cfg.get('mode')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="magi", description="MAGI System -- three-node deliberative council"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deliberate", help="convene the council")
    d.add_argument("--prompt", "-p")
    d.add_argument("--prompt-file", "-f")
    d.add_argument("--context-file", "-c", action="append")
    d.add_argument("--rounds", "-r", type=int)
    d.add_argument("--out", "-o")
    d.add_argument("--quiet", "-q", action="store_true")
    d.set_defaults(func=cmd_deliberate)

    rv = sub.add_parser(
        "review", help="single-round adversarial review of an already-drafted artifact"
    )
    rv.add_argument("--prompt", "-p")
    rv.add_argument("--prompt-file", "-f")
    rv.add_argument("--context-file", "-c", action="append")
    rv.add_argument("--diff", action="store_true", help="review `git diff HEAD`")
    rv.add_argument("--out", "-o")
    rv.add_argument("--quiet", "-q", action="store_true")
    rv.set_defaults(func=cmd_review, rounds=1)

    doc = sub.add_parser("doctor", help="probe models and check config")
    doc.add_argument("--assign", action="store_true", help="rewrite config with working models")
    doc.set_defaults(func=cmd_doctor)

    m = sub.add_parser("mode", help="get or set when MAGI convenes")
    m.add_argument("value", nargs="?", choices=list(MODE_HELP))
    m.set_defaults(func=cmd_mode)

    i = sub.add_parser("install", help="write config and the global policy block")
    i.set_defaults(func=cmd_install)

    args = ap.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nMAGI: deliberation aborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
