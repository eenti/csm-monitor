# CSM Bot

A Telegram bot that watches Lido's Community Staking Module and reports on operator activity once
a week.

It collects data daily, sends one brief every Monday, and fires a small number of threshold alerts
in between. It contains no AI at runtime — every line it outputs is computed deterministically from
recorded data.

## What it is for

CSM is a mature product. This tool is not a growth dashboard; it exists to answer two questions:

1. **Is CSM operationally stable?** Are validators performing, are frames publishing on time, is
   anyone about to be ejected.
2. **What is happening with operators?** Who joined, who left, which cohort they belonged to, and
   what the chain says about the circumstances of their departure.

The originating case: in July 2026 a batch of addresses was made eligible for a CSM gate, and a
month later none of them had claimed. Nobody noticed until someone thought to check by hand. This
bot notices.

## Design rules

These are constraints, not preferences. When a feature conflicts with one of them, the feature loses.

**Certainty over coverage.** These numbers get forwarded to other people. Every figure the bot
prints must be traceable to a specific contract call at a specific block, or to a specific file at a
specific commit. Fewer trustworthy metrics beat more shaky ones.

**Reproducible after the fact.** Collection stores raw responses, not just computed values. Any
brief can be re-derived from the database months later and must produce the same numbers.

**Fails loudly.** If a data source is unreachable or stale, the brief says so in plain language. It
never silently omits a block, and it never shows yesterday's number as if it were today's.

**No AI at runtime.** The weekly headline is chosen by a priority-ordered list of conditions, each
mapping to a sentence template filled with real numbers. Same input, same output, always.

**No heuristics smuggled in as facts.** Anything derived from an assumption is labelled as such, or
it does not ship.

## What it reports

### Weekly brief — Mondays

| Block | Contents |
|---|---|
| Headline | One deterministic sentence: the most significant condition that is true this week |
| Window | The date every delta below is measured from, and the week's movement in one line |
| Operators | New and departed operators, split by cohort. The main block |
| Claim funnel | Eligible → claimed → keys uploaded → active, per gate list and assessment round |
| Capacity | Stake share, headroom under the limit, 7-day rate, projected runway, binding constraint |
| Stake allocation | Why CSM did or did not receive stake this week |
| Performance | Frame summary. Only appears in weeks where a frame was published |

Blocks with nothing to report are omitted rather than padded.

**Every delta is seven days, and says so.** The brief names the snapshot it is comparing against and
the span that comparison actually covers, because a missed collection makes the real window six days
or eight. Movement is printed even when it is zero — `(0)` beside a level, "none claimed" beside a
gate — since a number that did not move and a number that was never measured must not look alike.

### Alerts

| Alert | Fires when |
|---|---|
| Capacity runway | Projected days-to-limit falls below 30, then 14, then 7 |
| Constraint flip | CSM switches between supply-constrained and capacity-constrained for 3 consecutive days |
| Frame published | A new rewards frame lands — summary plus operators below threshold |
| Frame late | A frame passes its deadline without publishing |
| Ejection risk | An operator reaches one strike below the ejection threshold. Names the operator |
| Exit anomaly | Daily validator exits exceed the 30-day baseline by a wide margin |
| Unclaimed list | A batch made eligible N days ago still has a claim rate below threshold |
| Round cutoff | 14 days and 3 days before an ICS/IDVTC assessment cutoff, with current funnel state |

Alerts deduplicate: each condition fires once per episode, not once per run.

### Deliberately excluded

- **Governance tracking.** Motions, votes and proposals are followed through other channels.
- **Module-versus-module framing.** The other Lido staking modules are not competitors. What matters
  is how Staking Router allocation mechanics decide where a deposit lands.
- **Per-validator incident monitoring.** That is the committee's remit and is covered by
  `ethereum-validators-monitoring`.
- **Price, TVL, and anything already arriving through another channel.**

## What it cannot tell you

**Why an operator left.** Intent is not observable on-chain — nobody signs a transaction stating
their reason. The bot reports the circumstances so a hypothesis can be formed and the operator
contacted: tenure, cohort, whether the exit was voluntary or protocol-triggered, strikes accumulated
beforehand, penalties applied, performance in preceding frames, bond state, and rewards left
unclaimed.

That is the difference between "this operator left after two penalised frames and never claimed
0.4 ETH" and "this operator left". The first is worth an email. The bot finds them; the conversation
is still yours.

**Weekly performance.** Per-operator performance is published by the CSM oracle once per 28-day
frame. Computing it weekly would mean deriving attestation rates from the consensus layer directly,
which means running and trusting a beacon node. That is a different class of infrastructure and it
is not worth it here.

## Verifying a number

Every figure in a brief carries the block it was read at. To check one:

```bash
docker compose run --rm csm-bot python -m csmbot.main verify --metric capacity --date 2026-08-24
```

This re-reads the stored raw response, recomputes the metric, and prints the contract address,
selector, block number and decoded return value used to produce it.

## Operating it

Runs as a Docker container on Coolify.

- Collection runs daily; the brief is assembled and sent Monday at a fixed UTC hour.
- The SQLite database lives on a persistent volume so it survives redeploys. Losing it means losing
  history that cannot be reconstructed — most of these metrics are only observable live.
- On startup the bot backfills published rewards-frame history and, if today's scheduled collection
  was missed, collects immediately. Failed daily collections are recorded as gaps; past daily reads
  lost while the container was offline cannot be reconstructed.
- Secrets come from environment variables. See `.env.example`.

### Runtime settings

Send `/settings` from the configured Telegram chat to change the weekly brief day/hour, the daily
collection hour, and assessment-round cutoffs. Schedule values can be adjusted with inline buttons;
rounds use explicit commands so dates and labels can be reviewed before they are saved:

```text
/weekly tue 09:00
/collect 08:00
/round add 2026-09-07 ICS Round 6
/round set 1 2026-09-08 ICS Round 6
/round remove 1
```

Coolify environment variables are the defaults. Telegram changes are stored in SQLite under `/data`,
take effect immediately, and survive redeploys. `/settings reset` restores the Coolify defaults.

Only `CSM_TELEGRAM_CHAT_ID` is allowed to read commands or press settings buttons. In a group, that
means all group members share access; messages and callbacks from every other chat are ignored.
Secrets such as the bot token, RPC URLs and Pinata JWT cannot be viewed or changed through Telegram.

## Layout

```
csmbot/
  config.py     environment config, validated at startup
  chain.py      RPC client, block pinning, ABI decoding
  sources.py    data collection from chain, GitHub and the oracle
  store.py      SQLite: raw snapshots and derived metrics
  metrics.py    capacity, operators, funnel, allocation, performance
  brief.py      weekly brief assembly and headline selection
  alerts.py     threshold evaluation and deduplication
  settings.py   persistent schedules and assessment rounds controlled through Telegram
  telegram.py   delivery
  main.py       CLI: collect, brief, alerts, verify, backfill
```
