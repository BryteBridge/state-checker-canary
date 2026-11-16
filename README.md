# State checker canary

About every 3 hours, one batch checks all 25 states the eligibility checker serves,
end to end, from GitHub Actions: one job per state, side by side, then one
summary table for the batch. A state fails loudly when its checker stops
giving the right answers.

In the Actions tab, each run that checked the states is one batch. Its summary
page is the batch table, with each state's detail folded underneath; its jobs
are the states. GitHub starts the workflow every hour, and a start checks the
states only when the last batch started 2.5 hours ago or more
(`canary/due.py`): GitHub delays and drops scheduled runs when it is busy, and
on a plain 3-hour schedule the batches came three or four times a day instead
of eight. Asking every hour keeps them close to 3 hours apart; it is best
effort, as nothing runs while GitHub drops every start. A start that is not due
ends after its first job, green, having checked nothing. A run cancelled before
its checks, or a manual run of a few states, does not count as a batch.

## What one check does

1. **Reads the checker's configuration** to see whether the state is served.
2. **Looks up 5 entities live**, drawn at random from the state's pool
   (`pools/<code>.txt`: 200 ids the checker verified in its benchmark runs).
   One is drawn from the ids marked `active` (storage serves active entities,
   so the round trip below always has one), the other 4 from the whole pool.
   Each batch draws a fresh 5, with a random 5 to 15 second pause between
   lookups, so no entity is looked up on a fixed rhythm and no registry sees a
   burst. It calls `?live=true`, so the checker scrapes the registry right then.
   Each answer must be well formed, say it came from the registry, follow the
   checker's decision table, and be `verified`. A registry that does not answer
   gets one more try a minute later. One lookup in five that still gets no
   answer from the registry only warns, when the others answered: from
   2026-11-04 to 11-14 every red check was a registry not answering, never a
   wrong answer, and most of Virginia's were exactly one lookup of five.
3. **Round-trips through storage.** Right after a live answer, a normal
   lookup must be served from R2 with an observation this run produced. This
   proves answers are saved and read back.

Whether each state *should* be served is set here, in `canary/states.py`
(Indiana is not, since its firewall blocks the checker). A checker that drops
a state by mistake therefore fails its check rather than excusing itself.

| Fails the run (someone has to act) | Only warns (the run stays green) |
|---|---|
| The checker unreachable, or its configuration unreadable | A state meant not to be served (Indiana) |
| A state served, or not served, against `canary/states.py` | No answer in the run that storage would serve |
| An answer that is malformed, not live when asked live, or not verified | |
| The registry not answering, twice in a row, for more than one lookup in five | The registry not answering, twice, for one lookup in five |
| Storage not serving this run's answer back, or serving it back different | |
| The 20-minute budget running out, or the check itself crashing | |

**Failing loudly** means:
- the state's red job in the batch, with one error annotation per problem;
- the batch table, which lists every state with its first problem;
- an issue labelled `canary` and `canary-<state>`, assigned to the repository owner, closed by the first pass. While it stays open, a later failure comments on it (and so notifies) only when the failure changed, say from the registry not answering to storage failing; the same failure again rewrites the issue's text in place with how long it has been failing ("Failing since ..., 9 batches in a row"), which notifies nobody;
- one **outage issue** (`canary-outage`) when half or more of the served states (at least six) fail in the same batch of all of them: the likeliest cause is something they share (the proxies, the checker's server, storage), so one issue lists those first, instead of a dozen state issues. It notifies when it opens and when the kinds of failure change (say, storage starts failing too), not when states join or leave it. The failing states' own issues are left as they are while it lasts, so their "in a row" counts skip those batches. It closes when a batch of all states has fewer than half failing; a manual batch of a few states neither opens nor closes it;
- GitHub's failure email for scheduled runs, if the owner's notification settings have Actions emails on.

Issues are opened only for runs of `main`; a batch dispatched from a branch is
testing that branch.

## Layout

```
canary/                  the logic, Python standard library only
  states.py              the 25 states, in the console's order
  pools.py               reads pools/<code>.txt
  check.py               one state's check
  client.py              the checker's HTTP API, with transport retries
  batch.py               reads a batch's reports: the summary table, each state's result
  issue.py               a state's issue, and the outage issue: open, update, comment on change, close
  due.py                 whether an hourly start checks the states
  __main__.py            python -m canary plan | due | check | batch
.github/workflows/
  check.yml              the batch: plan, one check job per state, summary
  ci.yml                 lint, tests, workflow lint (actionlint, zizmor)
pools/<code>.txt         200 entity ids per state, verified before; "active" marks active ones
```

```
plan ── check CA ─┐
     ├─ check CT ─┤
     ├─ ...       ├── batch summary (table, each state's issue, push to the console)
     └─ check TX ─┘
```

Each state's job is independent: one failing never stops another, and none
waits on another. The workflow only runs Python; what a check does is in
`canary/`.

## Setup

Two repository secrets:

| Secret | Value |
|---|---|
| `CHECKER_BASE_URL` | The checker's https origin, no path (for example `https://checker.example.com`) |
| `CANARY_PUSH_SECRET` | Signs each batch's push to the checker's console; the same value as the checker's `CANARY_PUSH_SECRET` |

The checks call the checker's public lookup API, so the registry scraping, the
proxies, the captcha handling and the R2 credentials all stay on the checker's
server. None of them is in this repository or its secrets.

## Limits

- **Nothing here notices the canary stopping.** A disabled workflow, a dropped
  schedule or an exhausted Actions allowance runs no step, so no step can
  report it. The checker's console can: each batch pushes its results there
  (`canary/publish.py`), and past four hours without one the console says the
  checks have stopped reporting. Nobody is alerted until someone looks.
- **Cost.** 27 jobs a batch (plan, 25 states, summary), 8 batches a day, plus
  about 16 hourly starts a day that are not due and end after the plan job.
  Five spaced lookups and a round trip take about 2 to 3 minutes per state,
  and a private repository bills each job by the started minute: about 75
  minutes a batch, about 18,000 a month, more while states fail. GitHub Free
  includes about 2,000. Public repositories run standard runners free.
- **Load on the registries.** About 1,000 live lookups a day (5 per state per
  batch) plus the round trips, spread at random over each pool of 200.
- **What it cannot see.** The checks call the checker's public API, so they see
  an outage's effect, not its cause: when the proxy account stops serving
  (HTTP 402), every state answers as a registry that did not answer. The
  outage issue names the likely causes and how long the failed lookups took
  (about a second each points at the proxies); finding which one it is happens
  on the checker's server.
- **A state that is down stays one open issue.** It notifies when it opens and
  when its failure changes, not every batch; read its "failing since" line.

## Running it

- **On demand:** Actions, then *State checks*, then *Run workflow*. Leave
  *states* as `all`, or name some (`oh,wa`) to rerun just those. Set
  *count* to look up more than 5 entities per state (up to 20).
- **Locally:**
  ```sh
  CHECKER_BASE_URL=https://checker.example.com python -m canary check --state wa
  ```
- **Adding a state:** add a line to `canary/states.py`. The next batch gives
  it a job.
- **Development:** `uv sync`, then `uv run pytest`, `uv run ruff check .`,
  `uv run actionlint` and `uv run zizmor .github/workflows`.
