# Testing Promise Ledger

## Static checks

No network required.

```bash
genvm-lint check promise_ledger.py --json
```

Expected: `{"ok":true,"lint":{"ok":true,"passed":3},"validate":{"ok":true,...}}`.
This runs both AST safety checks (forbidden imports, non-deterministic
patterns, header structure) and SDK semantic checks (types, decorators,
storage field types, method signatures) in one pass.

## Live integration suite

Two files under `tests/integration/` drive the live suite:

- **`test_connectivity.py`** — one fast sanity check: confirms the
  deployed address is reachable, its schema can be fetched, and
  `get_stats()` returns the expected shape. Run this first if you're
  pointing the suite at a new deployment.
- **`test_live_lifecycle.py`** — drives every **non-owner** write method
  against the real deployed contract on StudioNet: real GEN escrow, real
  generated accounts, a real fetched photograph for visual evidence, a
  real external web fetch, and real LLM classification rounds.

`update_capacity`, `deregister_pool`, and `resolve_ambiguous` are
intentionally out of scope: all three are gated to whichever address
registered the pool. Since this suite registers its own pool with its
own freshly-generated `provider` account, it *could* exercise them too —
they're excluded on purpose to keep the suite scoped to the
submitter-facing, provider-onboarding, and consensus-classification
surface, which is where the contract's core guarantee (overcommitment
prevention) actually lives.

### Running it

```bash
python3 -m pytest tests/integration/ -v -s -m integration
```

Or target one file/test:

```bash
python3 -m pytest tests/integration/test_live_lifecycle.py::test_overcommit_is_deterministically_rejected_live -v -s -m integration
```

Config: [`gltest.config.yaml`](../gltest.config.yaml) points at
StudioNet. [`conftest.py`](../conftest.py) registers the `integration`
pytest marker. Test accounts are freshly generated, encrypted keystores
under `tests/.keys/` (gitignored — never committed, since the
decryption password lives in the test file itself). Regenerate them any
time with:

```bash
python3 tools/generate_accounts.py
```

### What each test does

Tests share three accounts (`provider`, `submitter_a`, `submitter_b`)
and a single pool (`gpu-fleet-useast1`), so run them sequentially rather
than in parallel to avoid nonce collisions and to let capacity math
build on prior state within the run:

1. **`test_contract_is_reachable_and_schema_matches`** — calls
   `get_stats()` on the deployed address and confirms the expected keys
   come back.
2. **`test_register_pool_and_admit_consumes_commitment_live`** —
   `register_pool` (skipped if the pool already exists from a prior run
   against the same address) → `submit_commitment` → `classify_commitment`,
   asserting the result lands on `CONSUMES` + `PENDING_OWNER_APPROVAL`, then
   having the provider call `approve_commitment` before asserting `ADMITTED`.
   The text is written to be unambiguous, so this is hard-asserted.
3. **`test_external_verification_and_does_not_consume_commitment_live`**
   — `submit_commitment` → `submit_external_verification` (a real fetch
   of a Wikipedia page) → `classify_commitment`. The external-evidence
   fields are hard-asserted (`corroborates`/`does_not_corroborate`,
   `HIGH`/`MEDIUM`/`LOW`); the classification verdict is printed and
   handled either way (see [below](#why-two-things-are-documented-rather-than-forced)).
4. **`test_overcommit_is_deterministically_rejected_live`** — submits a
   second, larger exclusive-reservation claim on the same pool. If the
   classifier lands on `CONSUMES` (expected, given the wording), the
   deterministic capacity check must reject it as
   `REJECTED_OVERCOMMIT` since the pool no longer has room — this
   assertion is hard, because it's the contract's core guarantee, not a
   classification-dependent detail.
5. **`test_cancel_commitment_live`** — `submit_commitment` →
   `cancel_commitment` before classification ever runs.
6. **`test_visual_evidence_and_bounded_release_live`** —
   `submit_visual_capacity_evidence` with a real fetched photograph
   (asserted: an attestation record with a `plausible` boolean was
   created), then, if the pool still has spare capacity, a
   bounded-window commitment carried through `classify_commitment` →
   `ADMITTED` → (after advancing the contract's monotonic epoch counter
   with further real `submit_commitment`/`cancel_commitment` calls)
   `release_commitment`.
7. **`test_ambiguous_classification_and_arbitration_timeout_live`** —
   submits a genuinely underspecified obligation; only if the classifier
   actually lands on `AMBIGUOUS` does it attempt to advance the epoch
   counter toward `claim_arbitration_timeout`.

### Latest confirmed run

The prior contract's 7-test result applied only to the previous lifecycle at
`0x3034F21a81ce366a6ae1489744Aa89897c9D6E21`; do not use that address to
verify this fix. The corrected deployment is
[`0x3104Cb8AD2A8428714614D9C55707A17D1C6b90B`](https://genlayer-explorer.vercel.app).
The integration files target that address. The suite asserts that a consuming
classification pauses for owner approval and that a non-owner approval attempt
fails without changing reserved capacity.

## Why two things are documented rather than forced

LLM classification is nondeterministic by nature, so two things in this
suite are **attempted with real data and asserted on where the
guarantee is provable, but not forced to a specific outcome**:

1. **Which verdict a given obligation resolves to**, where the wording
   is deliberately informational or deliberately vague. The suite only
   hard-asserts a specific verdict where the obligation text is written
   to be unambiguous (the `CONSUMES` and overcommit tests use explicit
   "dedicated, exclusively reserved" language — both have asserted
   `CONSUMES` on every run so far). Where a test's whole point is to
   probe an edge case (a pure reporting request, a deliberately vague
   clause), it prints the actual verdict and adapts rather than
   asserting one exact enum value.
2. **`claim_arbitration_timeout`.** The contract's arbitration deadline
   is `submission_epoch + 50`, where an "epoch" is the contract's own
   monotonic tick counter (incremented once per qualifying write call),
   not wall-clock time — deliberately, so validators never disagree
   over real-world timestamps (see the contract-level design note at
   the top of [`promise_ledger.py`](../promise_ledger.py)). Waiting out
   50 ticks purely from the test's own calls would mean 50+ additional
   live transactions on top of everything else the suite already does.
   The test attempts this with a bounded number of additional real
   ticks (each one a genuine `submit_commitment` + `cancel_commitment`
   pair, not a placeholder) and documents rather than forces the result
   if the deadline isn't reached within that bound. Structural
   correctness (`arbitration_deadline_epoch > current_epoch` immediately
   after landing on `AMBIGUOUS`) is still asserted unconditionally on
   every run where the verdict lands `AMBIGUOUS`.

This mirrors the pattern used by this project's other live-tested
GenLayer contracts for the same underlying reason: an LLM verdict is not
something a test should be allowed to force, only to observe and react
to correctly.

## A bug this suite caught before it shipped

The address originally selected for this submission had `register_pool`
revert on **every** call: the deployed source built its per-pool
commitment index with `PoolIndex(commitment_ids=DynArray[str]())` inside
a `TreeMap[str, PoolIndex]`. GenVM rejects direct user instantiation of
a subscripted generic storage type constructed that way:

```
TypeError: this class can't be instantiated by user
```

Switching to the documented workaround for this case,
`gl.storage.inmem_allocate(DynArray[str])`, surfaced a second,
runner-specific issue on the pinned runner version:

```
TypeError: _GenericAlias.__init__() missing 1 required positional argument: 'args'
```

Rather than ship a primitive whose onboarding method reverts for every
caller, the per-pool reverse index was removed entirely. `promise_ledger.py`
no longer declares a `PoolIndex` dataclass or a `pool_index` storage
field at all — `_reserved_units_overlapping` (the deterministic capacity
check) and `list_commitments_for_pool` (a view method) both do a
filtered scan over the existing global `commitment_order` list instead,
matching on `pool_id`. This needs no nested generic storage type, is
simpler to audit, and is more than sufficient at the scale a single
capacity pool operates at.

The previous deployment above remains historical only. The corrected contract
is deployed on StudioNet at
**`0x3104Cb8AD2A8428714614D9C55707A17D1C6b90B`**. Its schema was retrieved
successfully and its non-owner lifecycle was exercised through pool
registration, consuming submission, and consensus classification. See
[`review.md`](../review.md) for the exact on-chain observations and the
remaining owner-gated coverage boundary.

## Re-deploying to a fresh address

If you want to run this suite against a brand-new deployment rather than
the reused one above:

```bash
genlayer account import --name pl-provider --keystore tests/.keys/provider.json \
  --source-password "pl-live-test-pass-2026" --password "pl-live-test-pass-2026"
genlayer account unlock --account pl-provider --password "pl-live-test-pass-2026"
genlayer account use pl-provider
genlayer deploy --contract promise_ledger.py --wallet keystore
```

Then update `CONTRACT_ADDRESS` in both files under `tests/integration/`
to the new address before re-running the suite.
