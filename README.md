# Promise Ledger

A semantic capacity / overcommitment-prevention primitive for autonomous
services and agents, built as a GenLayer Intelligent Contract.

**Source:** [`promise_ledger.py`](promise_ledger.py) — single file, 1,272
lines, 20 public methods (11 write, 9 view), 0 constructor parameters.
**Deployed on StudioNet:** [`0x3034F21a81ce366a6ae1489744Aa89897c9D6E21`](https://genlayer-explorer.vercel.app)
— live-verified end to end; see [Verified](#verified) below and
[`docs/TESTING.md`](docs/TESTING.md) for the full test report.

## Table of contents

- [The problem](#the-problem)
- [Design boundary: what's deterministic vs. what needs consensus](#design-boundary-whats-deterministic-vs-what-needs-consensus)
- [Why this shouldn't produce an UNDETERMINED consensus result](#why-this-shouldnt-produce-an-undetermined-consensus-result)
- [The consensus-verified write paths](#the-consensus-verified-write-paths)
- [State model](#state-model)
- [Escrow summary](#escrow-summary)
- [Full method reference](#full-method-reference)
- [Repository layout](#repository-layout)
- [Why this is a reusable primitive, not a one-off demo](#why-this-is-a-reusable-primitive-not-a-one-off-demo)
- [Reading order for reviewers](#reading-order-for-reviewers)
- [Verified](#verified)

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
