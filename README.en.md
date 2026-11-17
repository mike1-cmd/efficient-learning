# efficient-learning

**An AI learning coach that refuses to let you skip steps.**

[![test](https://github.com/mike1-cmd/efficient-learning/actions/workflows/test.yml/badge.svg)](https://github.com/mike1-cmd/efficient-learning/actions/workflows/test.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)](.)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

English · [中文](README.md)

Give it a topic. It walks you through six gated stages: **target → diagnose → core → practice → map → retest**.
Every stage must leave a verifiable artifact on disk; `scripts/stage.py` checks it and blocks `advance` if anything is missing.

Passing the final retest only earns you **"can do it right now."**
It takes unaided recall on **two different days** plus a 7-day memory stability to earn **"mastered."**

> Works as a skill in Claude Code, WorkBuddy, CodeBuddy, or any agent that reads `SKILL.md`.
> Zero dependencies — Python standard library only. All data stays local.

```
Learning loop
target → ① diagnose → ② core → ③ practice → ④ map → ⑤ retest → passed
            ↑                        │                    │
            └── wrong answer → only that concept ─────────┘
                                                          ↓
                                            "can do it now" (same-day only)
                                                          │
Retention loop                                            │
per-concept forgetting curve → due → unaided recall → recalled → longer interval → mastered
                                                 └→ forgot → interval collapses → redo concept
```

## What makes it different

| Typical AI study plan | What actually happens | What this does |
| --- | --- | --- |
| Generate a 30-day roadmap | Pretty outline, never opened again | Stage 1 runs a real task first, so the plan targets your actual gaps |
| Explain all 9 concepts | Zero hands-on work | Every concept needs a 5–30 min action, and the artifact path must exist |
| AI summarizes the knowledge map | Looks nice, your head is still scattered | Stage 4 gives drawing rules only; you draw, it critiques |
| "Got it?" "Yep." | Fluency mistaken for mastery | Six question angles rotate; repeating one angle is not three pieces of evidence |
| Done when the course ends | Back to zero in two weeks | Each concept gets its own FSRS-4.5 schedule and unaided-recall checks |
| Same-day 4/5 → "mastered" | Only proves working memory | Mastery requires cross-day retrieval ≥2 days + stability ≥7 days |

## Four mechanisms

**1. Order is enforced by code, not by prompt.**
`stage.py` is a state machine. It checks for a verifiable success criterion (vague words like "familiar with" get rejected),
real task evidence, 3–9 concepts, artifact paths that actually exist, a hand-drawn map, and a retest where ≥4 of 5 items
are bound to a concept name. Non-zero exit means the agent must report the failure verbatim and stop.

**2. Two tiers of mastery.**
*Can do it now* = angle coverage ≥4, recent accuracy ≥0.75, max passed difficulty ≥3.
*Mastered* = that, **plus** successful retrieval on ≥2 distinct days and stability S ≥7 days.
`report` prints exactly what's still missing.

**3. One forgetting curve per concept.**
FSRS-4.5 family. Each concept tracks **S** (stability, days) and **D** (difficulty, 1–10); intervals are computed,
not guessed. A second attempt on the same concept on the same day counts as practice only — one piece of evidence
can't be counted twice, or intervals get inflated to "three weeks out" when you'll have forgotten by tomorrow.

**4. Question selection that grades itself.**
Five buckets (**due review > resume > remediation > practice > new**), six angles
(definition / boundary / counterexample / operation / relation / transfer), five difficulty levels.
Difficulty is solved via 1PL IRT to land in a **70–85% success band**.
Then the prediction gets audited: `coach.py calibration` computes a Brier score against real outcomes, and
`--apply` writes the systematic bias back into your personal model.

## Install

Requires **Python 3.10+**. Nothing to `pip install`.

```bash
git clone https://github.com/mike1-cmd/efficient-learning.git
cp -r efficient-learning ~/.workbuddy/skills/efficient-learning
```

Claude Code: `~/.claude/skills/efficient-learning`. Project-scoped: `<project>/.workbuddy/skills/`.

Open a **new** conversation (running sessions can't see new skills) and say:

> I want to learn closures.

A correctly loaded skill always opens with `【第 0 阶 · 定靶】` (Stage 0 · Target).

```bash
python scripts/new_session.py "closures" --path ./learn
```

## A day in the loop

```bash
$ python scripts/coach.py today .
【今天】2026-11-17　连续第 5 天
到期召回   3 个（约 9 分钟）
补弱       1 个（闭包：最近答对率 33%）
现在做这一件：召回「闭包」（63% 还记得，拖到明天约 45%）
```

```bash
$ python scripts/coach.py next .
【取题】补弱 · 闭包
角度    边界　← 刚在这个角度答错，先补到过关再换角度
难度    3/5（预计做对率 78%，目标带 70%–85%）
理由    最近答对率 33%
```

```bash
$ python scripts/coach.py recall .
【召回 1/3】闭包　现在还能想起的概率 63%
规则：不看书、不翻笔记、不问我。先讲，讲完再对。
限时 90 秒：① 是什么 ② 什么时候失效 ③ 举个例子 ④ 和谁有关
```

The rubric is withheld until after you answer — the rubric is itself a hint.
`--json` exposes it for the grading agent, never for the learner.

## Commands

| Command | Purpose |
| --- | --- |
| `stage.py status / check / advance` | where am I, validate only, move one stage up |
| `stage.py retry <dir> --concept X` | wrong on retest → back to stage 3 for that one concept, clears its artifacts, logs a lapse on the curve |
| `stage.py reset <dir> --reason "..."` | same concept wrong twice in a row → archive the round, restart at stage 0 |
| `stage.py due <dir> [--all] [--cap N]` | due queue, most-forgotten first; `--all` merges every topic |
| `stage.py grade / stats / nodes` | record a review, read retention + calibration + evidence rate, inspect curves |
| `coach.py today / next / recall` | one action, one question, one unaided recall |
| `coach.py add / record / sync` | store an item, log an attempt (3 places at once), write back to the retest table |
| `coach.py report / profile / calibration` | two-tier status, cross-topic learner profile, dual-model audit |

Grades are four levels: `错` / `勉强` / `对` / `轻松`. "Barely right" and "effortless" shouldn't earn the same interval.
**Fluency counts**: correct but slower than 2.5× the concept's own median time is downgraded to "barely."

## Data

Plain local files, human-readable, git-diffable, no account, no cloud:

`.stage.json` · `.learning.json` · `.items.json` · `.attempts.jsonl` · `_coach/events.jsonl` · `_coach/model.json`

## Tests

```bash
python scripts/selftest.py    # 295 assertions, ~40s
```

## Honest limitations

- **Grading is still done by the same model.** Fresh-context blind grading is a discipline written in `SKILL.md`, not an enforced mechanism. `--evidence` (storing the learner's exact words) is the compensating control.
- **Only the global difficulty offset is calibrated.** `BETA_STEP` and the 70–85% band remain priors.
- **Weak-angle bias only applies to untested angles.**
- **Python required** — without it, the gates degrade to prompt-level suggestions.
- **Chinese-first.** Column alignment, terminology, and prompts are tuned for Chinese; English works but isn't optimized.

## License

MIT
