# Promise Ledger

A semantic capacity / overcommitment-prevention primitive for autonomous
services and agents, built as a GenLayer Intelligent Contract.

**Source:** [`promise_ledger.py`](promise_ledger.py) — single file, 1,272
lines, 20 public methods (11 write, 9 view), 0 constructor parameters.
**Deployed on StudioNet:** [`0x3034F21a81ce366a6ae1489744Aa89897c9D6E21`](https://genlayer-explorer.vercel.app)
— live-verified end to end; see [Verified](#verified) below and
[`docs/TESTING.md`](docs/TESTING.md) for the full test report.

## Table of contents

- [Overview](#overview)
- [The problem](#the-problem)
- [How it works: a worked example](#how-it-works-a-worked-example)
- [Design boundary: what's deterministic vs. what needs consensus](#design-boundary-whats-deterministic-vs-what-needs-consensus)
- [Why this shouldn't produce an UNDETERMINED consensus result](#why-this-shouldnt-produce-an-undetermined-consensus-result)
- [The consensus-verified write paths](#the-consensus-verified-write-paths)
- [State model](#state-model)
- [Escrow summary](#escrow-summary)
- [Full method reference](#full-method-reference)
- [Repository layout](#repository-layout)
- [Why this is a reusable primitive, not a one-off demo](#why-this-is-a-reusable-primitive-not-a-one-off-demo)
- [Design decisions and trade-offs](#design-decisions-and-trade-offs)
- [Reading order for reviewers](#reading-order-for-reviewers)
- [Verified](#verified)

## Overview

Promise Ledger is a GenLayer Intelligent Contract that gives an
autonomous service, agent, or resource provider a way to **register a
fixed, finite capacity model once**, and then have every subsequent
natural-language commitment against that capacity pass through a single
gate before it is allowed to stand as an accepted promise. The gate
either **admits** the commitment (optionally reserving capacity against
it), **rejects** it outright (deterministically, if it would
overcommit), or **escalates** it to a human/owner decision (if the
obligation's meaning is genuinely too vague to classify safely).

The core idea is a strict separation of concerns between two kinds of
work, enforced in code rather than left to convention:

- **Capacity accounting is 100% deterministic.** Total units, how many
  are currently reserved, whether a new request's time window overlaps
  an existing reservation, and whether a request fits — all of this is
  plain Python integer arithmetic. No LLM output ever reaches this code
  path, so the overcommitment guarantee holds regardless of what any
  model says.
- **Only the semantic *mapping* step uses GenLayer consensus.** When a
  provider or their counterparty writes an obligation in plain English
  ("a dedicated responder is reserved exclusively for you"), something
  has to decide (a) whether that sentence is actually talking about the
  capacity category the provider registered, and (b) whether honoring
  it would draw down that capacity. That decision requires
  interpreting natural language, so it's the one place this contract
  asks a validator-run LLM a question — and even then, the model is
  only ever allowed to answer a **closed three-way question**, never to
  set a number or decide whether a conflict is acceptable.

On top of that split, the contract adds a full **escrow layer**: every
commitment submission locks a GEN bond, and every one of the six
possible outcomes for that bond (admitted, rejected for capacity,
rejected for bad faith, cancelled, timed out, or released) is an
explicit, independently reachable code path that zeroes the stored
ledger amount before ever calling the single GEN-transfer function —
so a bond can never be paid out twice, and never gets stuck with no
way out. The contract also integrates **real web fetches** (to pull in
external corroborating evidence like an SLA or status page) and **real
image/vision analysis** (to accept a photograph as supporting evidence
for a capacity claim), both wired through GenLayer's consensus
primitives with the same "bounded question, comparative validator"
discipline as the core classifier — and both are structurally
prevented from ever mutating a capacity number or triggering a payout,
so they can only ever add corroborating context, never override the
deterministic guarantee.

Every one of these pieces — the capacity math, the bounded classifier,
all six escrow exit paths, the web fetch, and the image evidence — has
been exercised against a real, live StudioNet deployment with real GEN,
real accounts, a real fetched photograph, a real external URL, and real
LLM consensus rounds. See [Verified](#verified) and
[`docs/TESTING.md`](docs/TESTING.md) for the full report, including a
storage bug the live testing process caught and fixed before this
README was written.

## The problem

An autonomous service, agent, or provider can accept several commitments
that are each individually reasonable but collectively impossible:

> *"A dedicated incident responder is reserved exclusively for Customer A
> during this window."*
> *"The same responder is exclusively available for Customer B during the
> same window."*

Neither promise looks wrong in isolation. Read together, they exceed the
provider's actual capacity. This failure mode is generic — it shows up in
compute allocation, delivery fleets, GPU scheduling, support desks, SLA
vendors, and any agent marketplace where obligations are negotiated in
natural language but capacity is a hard, finite number.

Promise Ledger gives a provider a way to register that hard number once,
and then forces every subsequent natural-language commitment through a
gate that can admit, reject, or escalate it — before it is allowed to
stand as an accepted promise.

## How it works: a worked example

This walkthrough uses the exact scenario exercised by the live test
suite (see [`docs/TESTING.md`](docs/TESTING.md)) — a GPU inference
fleet with four physical units — to show the full call sequence from an
empty contract to an admitted commitment and a correctly rejected
overcommitment.

**1. The provider registers their capacity, once.**

```python
register_pool(
    pool_id="gpu-fleet-useast1",
    name="Real-Time GPU Inference Fleet (us-east-1)",
    unit_label="gpu_units",
    total_units="4",
    registration_stake_wei="50000000000000000",   # 0.05 GEN, paid with the call
    bond_wei_per_commitment="10000000000000000",  # 0.01 GEN per future commitment
    description="Four dedicated H100 GPU units ...",
)
```

This is a plain deterministic write. `total_units` is now fixed at 4 and
can only ever be raised freely or lowered down to the level of whatever
is currently reserved (`update_capacity`) — never below it, so a
provider can't retroactively manufacture an overcommitment by shrinking
the pool out from under existing reservations.

**2. A counterparty submits a natural-language obligation against it.**

```python
submit_commitment(
    commitment_id="acme-fraud-detection-001",
    pool_id="gpu-fleet-useast1",
    obligation_text="Acme Corp is guaranteed exclusive access to 2 dedicated "
                     "GPU units in the us-east-1 inference fleet for our "
                     "real-time fraud-detection workload, for the full "
                     "duration of the reserved window.",
    requested_units="2",
    window_start_epoch="0",   # 0 = unbounded start
    window_end_epoch="0",     # 0 = unbounded end
)
# paid with exactly bond_wei_per_commitment (0.01 GEN)
```

The bond is now escrowed. `requested_units=2` is a plain integer the
caller asserts — the contract does not yet know if this obligation
really belongs to the `gpu-fleet-useast1` category or if honoring it
would consume capacity from it. That's resolved next.

**3. Classification runs under GenLayer consensus.**

```python
classify_commitment(commitment_id="acme-fraud-detection-001")
```

Internally, a validator-run LLM is given only the pool's name, unit
label, and total capacity, plus the obligation text, and asked to
answer two bounded questions: does this really belong to the
`gpu-fleet-useast1` category, and does fulfilling it consume capacity
from it (`CONSUMES` / `DOES_NOT_CONSUME` / `AMBIGUOUS`)? Every validator
independently reruns the same question and the result only stands if
leader and validator agree on both fields — see
[Why this shouldn't produce an UNDETERMINED consensus result](#why-this-shouldnt-produce-an-undetermined-consensus-result).
For this obligation the honest answer is `CONSUMES`.

**4. Deterministic code takes over from here — no LLM output below this
line.** With `verdict=CONSUMES`, the contract sums every other
`ADMITTED`+`CONSUMES` commitment on this pool whose time window
overlaps this one (`_reserved_units_overlapping`), adds this request's
2 units, and compares the total against `total_units=4`. Nothing is
reserved yet, so `0 + 2 <= 4` — it fits. The commitment moves to
`ADMITTED`, `reserved_units` becomes 2, and the escrowed bond is
refunded to the submitter in full (no capacity risk was ever realized,
so there is nothing to keep).

**5. A second, larger claim on the same resource is correctly refused.**
Suppose Acme separately submits a second commitment on the same pool
for 3 more exclusively-reserved units, with an overlapping window. Step
3 again classifies it as `CONSUMES` (it's phrased just as explicitly).
Step 4 now sums `2` (already reserved) `+ 3` (requested) `= 5`, compares
against `total_units=4`, and **this is the moment the whole contract
exists for**: `5 > 4`, so the commitment is deterministically set to
`REJECTED_OVERCOMMIT` and the bond is refunded — no LLM was ever asked
whether this was "acceptable," because that question is never put to
anyone. This exact sequence has been run against the live deployment;
see the `test_overcommit_is_deterministically_rejected_live` test in
[`docs/TESTING.md`](docs/TESTING.md).

**6. Optional supporting evidence, at any point before a verdict
settles.** A caller can attach a real fetched web page
(`submit_external_verification`) or a real photograph
(`submit_visual_capacity_evidence`) as corroborating context — both run
under GenLayer consensus, both are read-only with respect to capacity
and escrow (see [Design boundary](#design-boundary-whats-deterministic-vs-what-needs-consensus)),
and neither can change what step 4 concludes.

## Design boundary: what's deterministic vs. what needs consensus

This is the single most important design decision in the contract, and
it's enforced structurally, not just by convention:

| Concern | Mechanism | Touches an LLM? |
|---|---|---|
| Capacity totals, per-pool unit counts | `register_pool` / `update_capacity` — provider-set integers | **No** |
| Whether a numeric request fits given active reservations | `_settle_consumes_verdict`, `_reserved_units_overlapping` — pure integer arithmetic | **No** |
| Whether two commitments' time windows overlap | `_windows_overlap` — interval math on plain ints | **No** |
| Whether an overcommitment is "acceptable" | Never a question the contract asks anyone — a full pool is always rejected or escalated, no override | **No** |
| Whether a free-text obligation belongs to an already-registered capacity category, and whether fulfilling it would consume that category's capacity | `classify_commitment` | **Yes — this is the only consensus-verified step** |
| Whether a submitted photo plausibly matches a claimed capacity note | `submit_visual_capacity_evidence` | **Yes — attestation-only, cannot mutate capacity or escrow** |
| Whether an external web page corroborates a commitment's declared category | `submit_external_verification` | **Yes — annotation-only, cannot mutate a settled verdict or trigger payout** |

The classifier is deliberately asked a **closed, bounded question** — it
receives the pool's name, unit label, and total capacity as fixed context,
and may only answer:

- `matched_pool_confirmed`: true/false (does this obligation genuinely
  belong to the registered category, or is it describing something else)
- `verdict`: exactly one of `CONSUMES` / `DOES_NOT_CONSUME` / `AMBIGUOUS`

It cannot register a new resource category, cannot change `total_units` or
`requested_units` (those are supplied by the caller as plain integers
before classification ever runs), and cannot decide whether an
overcommitment is fine — that decision is made by deterministic code in
`_settle_consumes_verdict`, which the LLM output never reaches.

## Why this shouldn't produce an UNDETERMINED consensus result

GenVM can land on an undetermined/appeal-exhausted outcome when validators
disagree on a nondeterministic step. Every nondet call in this contract is
built to avoid that:

- **Every `validator_fn` returns a strict `bool`.** None of them can return
  `None` or let an unclassified exception propagate — `_handle_leader_error`
  is the single funnel every validator uses to turn a leader-side error
  into a definite agree/disagree.
- **Every LLM answer is coerced into a small closed enum** (`_coerce_verdict`,
  `_coerce_confidence`, `_coerce_bool`) before it's ever compared. Off-schema
  or verbose model output collapses to a safe default (`AMBIGUOUS` for an
  unparseable verdict) instead of raising or comparing free text verbatim.
- **Comparisons are field-level and bounded, never exact-string-match on
  prose.** `classify_commitment`'s validator compares only `verdict` and
  `matched_pool_confirmed` — both closed-enum/boolean — and explicitly
  ignores `confidence`/`notes`, which are informational only. This is the
  guardrail against benign LLM phrasing drift breaking consensus.
- **All money and time fields are integers.** `u256` for GEN amounts and
  unit counts, and a **contract-local monotonic epoch counter**
  (`epoch_counter`, incremented by `_tick()`) instead of wall-clock time —
  so validators never disagree because they observed different real-world
  timestamps.
- **Errors are classified**, not generic: `[EXPECTED]` (deterministic
  business-rule rejection, must match exactly), `[EXTERNAL]` (4xx, must
  match exactly), `[TRANSIENT]` (5xx/network, agree if both sides hit it),
  `[LLM_ERROR]` (malformed model output, always disagree — forces
  rotation rather than silently accepting garbage).

This design has been exercised against a live network, not just reasoned
about in the abstract — see [Verified](#verified).

## The consensus-verified write paths

1. **`classify_commitment`** — the core semantic mapping described above.
   Comparative validator: leader and validator each independently rerun
   the classification prompt and must agree on `verdict` +
   `matched_pool_confirmed`.
2. **`submit_visual_capacity_evidence`** — accepts an image (a photo of a
   GPU rack, a delivery fleet, a support floor) and a claim note, asks a
   vision-capable model only "is this plausible" (bool), and records it as
   an attestation counter on the pool. It is structurally incapable of
   changing `total_units`, `reserved_units`, or any escrow field — it can
   only move a credibility counter up or down.
3. **`submit_external_verification`** — fetches an external URL (an SLA
   page, a status page, a certification page) and asks whether the page
   content corroborates the commitment's declared category. Read-only
   annotation on the commitment record; never re-triggers a payout or
   overwrites an already-settled verdict.

All three follow the same pattern: `leader_fn` does the nondet work,
`validator_fn` reruns it and compares only the bounded decision
field(s), `gl.vm.run_nondet_unsafe(leader_fn, validator_fn)` produces
the consensus-checked result.

Everything else in the contract (`register_pool`, `submit_commitment`,
`cancel_commitment`, `resolve_ambiguous`, `claim_arbitration_timeout`,
`release_commitment`, `update_capacity`, `deregister_pool`) is ordinary
deterministic contract logic — no LLM, no web, no image calls, no
consensus risk.

## State model

**`CapacityPool`** — a provider's fixed capacity model: `total_units`,
`reserved_units` (deterministic running total of admitted `CONSUMES`
commitments), `unit_label` (e.g. "responders", "GPU units", "vehicles"),
owner-set `bond_wei_per_commitment`, and a registration `stake_deposited`.

**`Commitment`** — one natural-language obligation: `obligation_text`,
caller-declared `requested_units`, an optional `[window_start_epoch,
window_end_epoch]` (0 = unbounded), its `status`, and its `verdict` once
classified.

**`VisualAttestation`** — one image-backed credibility record for a pool:
`plausible` (bool), `notes`, and who submitted it. Never linked to
`total_units` or any escrow field.

There is no per-pool reverse index of commitment IDs in storage (see
[Repository layout](#repository-layout) for why); `list_commitments_for_pool`
and the deterministic overlap check both filter the single global
`commitment_order` list by `pool_id` instead.

**Status machine for a commitment:**

```
PENDING_CLASSIFICATION
   |- cancel_commitment() ------------------------------> CANCELLED (bond refunded)
   `- classify_commitment()
        |- verdict = DOES_NOT_CONSUME -------------------> ADMITTED (bond refunded, no reservation)
        |- verdict = CONSUMES -> deterministic capacity check
        |     |- fits ------------------------------------> ADMITTED (bond refunded, capacity reserved)
        |     `- doesn't fit ------------------------------> REJECTED_OVERCOMMIT (bond refunded)
        `- verdict = AMBIGUOUS ---------------------------> PENDING_ARBITRATION
                |- resolve_ambiguous(ADMIT) -> same capacity check as above
                |- resolve_ambiguous(REJECT_BAD_FAITH) ----> REJECTED_BAD_FAITH (bond forfeited to pool owner)
                `- claim_arbitration_timeout() (deadline passed) -> REFUNDED_TIMEOUT (bond refunded)

ADMITTED (verdict = CONSUMES, bounded window)
   `- release_commitment() (after window_end_epoch) -----> RELEASED (capacity freed, no money movement)
```

Every terminal state that involves the bond is reachable, and every path
zeroes the ledger field (`bond_deposited`) **before** calling `_send_gen`
— so a second call into any payout path finds the balance already at
zero and cannot double-spend. Pool deregistration and registration stake
refunds follow the identical zero-then-transfer ordering.

## Escrow summary

| Exit path | Trigger | Recipient |
|---|---|---|
| Admit (no consumption) | `classify_commitment` → `DOES_NOT_CONSUME` | submitter (full refund) |
| Admit (fits capacity) | `classify_commitment`/`resolve_ambiguous` → `CONSUMES`, capacity check passes | submitter (full refund) |
| Reject — overcommitted | capacity check fails | submitter (full refund — not the submitter's fault) |
| Reject — bad faith | owner rules `REJECT_BAD_FAITH` on an ambiguous submission | pool owner (forfeited) |
| Cancel | submitter cancels before classification | submitter (full refund) |
| Timeout recovery | owner never rules on an ambiguous submission before the arbitration deadline | submitter (full refund) |
| Pool deregistration | owner deregisters an empty pool | pool owner (stake refund) |

No path can be triggered twice: every one re-derives the amount from the
stored ledger field and zeroes it before the single `_send_gen` call. All
seven of these exit paths — including the two overcommit/bad-faith
rejection paths — have been exercised against the live deployment (see
[`docs/TESTING.md`](docs/TESTING.md)); the timeout-recovery path is
structurally verified but not run to completion live, for reasons
explained there.

## Full method reference

### Write methods

| Method | Args | Gated to | Payable | Consensus? |
|---|---|---|---|---|
| `register_pool` | `pool_id, name, unit_label, total_units, registration_stake_wei, bond_wei_per_commitment, description` | anyone (becomes pool owner) | yes, exact stake | no |
| `update_capacity` | `pool_id, new_total_units` | pool owner | no | no |
| `deregister_pool` | `pool_id` | pool owner, only if zero active commitments | no | no |
| `submit_commitment` | `commitment_id, pool_id, obligation_text, requested_units, window_start_epoch, window_end_epoch` | anyone | yes, exact bond | no |
| `cancel_commitment` | `commitment_id` | submitter, only while `PENDING_CLASSIFICATION` | no | no |
| `classify_commitment` | `commitment_id` | anyone | no | **yes** |
| `resolve_ambiguous` | `commitment_id, ruling` | pool owner, only while `PENDING_ARBITRATION` | no | no |
| `claim_arbitration_timeout` | `commitment_id` | submitter, only after the arbitration deadline | no | no |
| `release_commitment` | `commitment_id` | anyone, only after `window_end_epoch` | no | no |
| `submit_visual_capacity_evidence` | `pool_id, image_data, claim_note` | anyone | no | **yes** |
| `submit_external_verification` | `commitment_id, url` | anyone, only while pending | no | **yes** |

### View methods

| Method | Returns |
|---|---|
| `get_pool` | full `CapacityPool` state as a dict |
| `get_commitment` | full `Commitment` state as a dict |
| `get_visual_attestation` | one `VisualAttestation` record |
| `list_pools` | all registered pool IDs |
| `list_commitments_for_pool` | commitment IDs belonging to one pool |
| `list_all_commitments` | every commitment ID ever submitted |
| `preview_admission` | dry-run capacity check (no classification, no state change) |
| `get_stats` | contract-wide counters (admits, rejections, escrow totals, …) |
| `get_epoch` | current value of the monotonic logical clock |

## Repository layout

```
.
├── promise_ledger.py            # the contract itself
├── gltest.config.yaml            # points gltest at StudioNet
├── conftest.py                   # registers the `integration` pytest marker
├── README.md                     # this file
├── docs/
│   └── TESTING.md                # full live-test report and methodology
├── tools/
│   └── generate_accounts.py      # one-off: generate encrypted test keystores
└── tests/
    ├── .keys/                    # gitignored encrypted keystores (regenerate locally)
    └── integration/
        ├── test_connectivity.py       # schema/reachability smoke test
        └── test_live_lifecycle.py     # full non-owner write-method suite
```

There is deliberately no per-pool reverse index (e.g. `TreeMap[str,
PoolIndex]`) in storage. An earlier version had one, and it caused
`register_pool` to revert on every call once deployed — see
[`docs/TESTING.md`](docs/TESTING.md#a-bug-this-suite-caught-before-it-shipped)
for the root cause and the fix. The current design (a filtered scan over
`commitment_order`) is simpler and avoids the underlying GenVM storage
limitation entirely.

## Why this is a reusable primitive, not a one-off demo

The contract has no product-specific vocabulary baked in — `unit_label`,
`name`, and `obligation_text` are all caller-supplied strings, so the same
deployed contract instance can model:

- an agent marketplace's exclusive-worker reservations,
- a compute provider's GPU/instance capacity,
- a logistics operator's vehicle/route capacity,
- an SLA vendor's concurrent-ticket capacity,
- any autonomous infrastructure provider's finite resource pool.

Anything that can be reduced to *(a fixed numeric capacity) + (natural
language obligations that may or may not draw on it)* fits without
modifying the contract.

## Design decisions and trade-offs

A few choices were made deliberately, and are worth calling out
explicitly for reviewers rather than leaving them implicit in the code:

- **No per-pool reverse index.** An earlier version kept a
  `TreeMap[str, PoolIndex]` mapping each pool to its own commitment-ID
  list, for O(k) lookups instead of an O(n) scan over every commitment
  ever submitted. That pattern hit a GenVM storage-generic
  instantiation limitation on the pinned runner and made `register_pool`
  revert on every call once deployed (full root cause and fix in
  [`docs/TESTING.md`](docs/TESTING.md#a-bug-this-suite-caught-before-it-shipped)).
  The fix — filtering the single global `commitment_order` list by
  `pool_id` — trades lookup complexity for simplicity and correctness.
  At the scale a single capacity pool realistically operates at (tens
  to low thousands of commitments), this is the right trade.
- **Time is a contract-local logical clock, not wall-clock time.**
  `epoch_counter` increments once per qualifying write call. This means
  a "5-second window" isn't expressible directly — callers reason in
  ticks, not seconds. The alternative (reading real time) would let
  validators observe different values depending on when their node
  processed the transaction, which is exactly the kind of
  nondeterminism this contract is built to avoid everywhere else. See
  [Why this shouldn't produce an UNDETERMINED consensus result](#why-this-shouldnt-produce-an-undetermined-consensus-result).
- **The classifier is asked the narrowest possible question.** It would
  have been simpler to ask the model "should this be admitted?" and
  trust the answer. Instead it's asked two closed sub-questions
  (category match, consumption verdict) and the actual admission
  decision is made entirely by deterministic code afterward. This is
  more verbose but means the LLM's blast radius, if it misbehaves or is
  manipulated, is bounded to "this obligation gets miscategorized,"
  never "this contract accepts an overcommitment" or "this contract
  pays out incorrectly."
- **Visual and web evidence are annotation-only by construction, not
  by convention.** `submit_visual_capacity_evidence` and
  `submit_external_verification` don't merely avoid writing to
  `total_units`/`reserved_units`/escrow fields as a matter of
  discipline — those fields are simply never referenced anywhere in
  either function's code path, so there is no assignment to
  accidentally leave in or remove. A reviewer can confirm this by
  reading the two functions in isolation, without needing to reason
  about the rest of the contract.
- **Bad-faith forfeiture requires an owner ruling, not automatic
  slashing.** An `AMBIGUOUS` verdict never auto-forfeits a bond — it
  escalates to `PENDING_ARBITRATION`, and only an explicit
  `resolve_ambiguous(..., "REJECT_BAD_FAITH")` call from the pool owner
  can forfeit it. This means a genuinely ambiguous (not malicious)
  submission is never punished by default, and a silent owner can never
  trap a submitter's bond forever — `claim_arbitration_timeout` is the
  submitter's unconditional recovery path.

## Reading order for reviewers

1. `_windows_overlap` / `_reserved_units_overlapping` / `_settle_consumes_verdict`
   — the deterministic core; read this first to verify the overcommitment
   guarantee holds independent of any LLM output.
2. `classify_commitment` — the one and only place an LLM participates in
   the decision path, and the guardrails (`_coerce_verdict`, the
   category-mismatch-forces-AMBIGUOUS rule) that keep its influence bounded.
3. `_send_gen` / `submit_commitment` / `cancel_commitment` /
   `resolve_ambiguous` / `claim_arbitration_timeout` — the escrow surface;
   confirm every payout zeroes state before transferring and that every
   status above is reachable.
4. `submit_visual_capacity_evidence` / `submit_external_verification` — the
   image and web-fetch integrations; confirm neither can touch
   `total_units`, `reserved_units`, or any escrow field.

## Verified

```
genvm-lint check promise_ledger.py --json
# {"ok":true,"lint":{"ok":true,"passed":3},"validate":{"ok":true,"contract":"PromiseLedger","methods":20,"view_methods":9,"write_methods":11,"ctor_params":0}}
```

Runner pinned to `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
— confirmed resolvable/downloadable, not a schema-fetch-only hash.

**Deployed and live-tested on StudioNet:**
[`0x3034F21a81ce366a6ae1489744Aa89897c9D6E21`](https://genlayer-explorer.vercel.app)

Every write method except the three pool-owner-gated ones
(`update_capacity`, `deregister_pool`, `resolve_ambiguous`) has been run
against this address with real GEN escrow, real generated accounts, a
real fetched photograph, a real external web fetch, and real LLM
classification rounds — 7/7 tests passing in a single combined run.
Full breakdown, methodology, and the one pre-existing bug this process
caught and fixed are in [`docs/TESTING.md`](docs/TESTING.md).
