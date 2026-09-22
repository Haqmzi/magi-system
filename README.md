# MAGI System

A three-node deliberative council for Claude Code, backed by Gemini.

Three independent models — each with a different charter — assess a plan,
diff, or open question, cross-examine each other, and vote before you commit
to something risky.

```
MELCHIOR-1   the Scientist   technical truth, correctness, mechanism
BALTHASAR-2  the Mother      risk, blast radius, six-month regret, security
CASPER-3     the Woman       pragmatism, simplicity, plain language
```

See [SKILL.md](SKILL.md) for full usage, modes (`review` vs `deliberate`),
and configuration.

## Setup

Requires a Gemini API key:

```bash
export GEMINI_API_KEY=your-key-here
python scripts/magi.py doctor
```

## Usage

```bash
python scripts/magi.py review -f plan.md
python scripts/magi.py deliberate -f question.md -r 3
```
