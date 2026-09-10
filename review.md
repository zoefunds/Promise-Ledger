# Promise Ledger review remediation

## Review request

The team identified two coupled lifecycle failures:

1. Any caller could submit and classify a consuming obligation, causing it to
   become `ADMITTED` and reserve capacity from a provider's pool without the
   provider's authorization.
2. A consuming commitment with `window_end_epoch = 0` had no release path.
   Such an unbounded reservation could keep capacity reserved indefinitely and
   prevent pool deregistration, leaving the provider's registration stake
   locked.

## Corrected design

The corrected contract is deployed on StudioNet at
[`0x3104Cb8AD2A8428714614D9C55707A17D1C6b90B`](https://genlayer-explorer.vercel.app).
The prior deployment is not a verification target for this remediation.

### Owner approval separates classification from admission

`classify_commitment` still determines only whether an obligation consumes the
registered category. For a `CONSUMES` result it now stores the result and sets
the commitment to `PENDING_OWNER_APPROVAL`; it does not mutate either
`reserved_units` or `active_commitment_count`, and it does not refund the bond
yet.

Only the pool owner may call `approve_commitment`. This is the single path that
runs `_settle_consumes_verdict`, which performs the deterministic overlap and
capacity check before changing the commitment to `ADMITTED`. Consequently, a
caller can no longer lock a provider's capacity merely by submitting text and
obtaining a semantic classification.

The owner also has `reject_pending_approval`, which terminates the request and
refunds its bond. The submitter has `cancel_pending_approval`, so a silent or
unresponsive owner cannot trap the submitter's bond in the approval queue.

### Every admission can be released

`release_commitment` remains permissionless for an admitted commitment whose
bounded end epoch has passed. The new `cancel_admitted_commitment` is callable
by either the pool owner or the commitment submitter at any time, including for
an unbounded window. For a consuming commitment it verifies accounting is
consistent, decrements `reserved_units` and `active_commitment_count`, then
sets the commitment to `RELEASED`.

This provides a safe authorized escape route for all admissions. Once the last
consuming commitment is released, the pool owner can deregister the pool and
recover its registration stake through the existing `deregister_pool` path.

## Lifecycle after the fix

```text
PENDING_CLASSIFICATION
  ├─ submitter cancel ─────────────────────────────> CANCELLED
  ├─ DOES_NOT_CONSUME ─────────────────────────────> ADMITTED
  ├─ AMBIGUOUS ───────────────────────────────────> PENDING_ARBITRATION
  └─ CONSUMES ────────────────────────────────────> PENDING_OWNER_APPROVAL
       ├─ submitter cancel ───────────────────────> CANCELLED
       ├─ owner reject ───────────────────────────> REJECTED_BY_OWNER
       └─ owner approve + deterministic fit check ─> ADMITTED / REJECTED_OVERCOMMIT

ADMITTED
  ├─ bounded end reached: anyone releases ────────> RELEASED
  └─ any time: owner or submitter cancels ────────> RELEASED
```

All bond-refund and forfeiture paths preserve the contract's zero-before-
transfer ordering. `cancel_admitted_commitment` moves no bond because admission
already settles it; it only releases capacity.

## Files changed

- `promise_ledger.py`: approval state, owner approval/rejection, submitter
  cancellation of a pending approval, authorized admission cancellation, and
  the owner-rejection statistic in `get_stats`.
- `tests/integration/test_live_lifecycle.py`: updated address and lifecycle
  expectations; checks that an unauthorized approval cannot reserve capacity;
  adds pending-approval cancellation coverage.
- `tests/integration/test_connectivity.py`: targets the corrected deployment.
- `README.md` and `docs/TESTING.md`: updated method count, state model,
  deployment reference, and test claims.

## Verification performed

`genvm-lint check promise_ledger.py --json` passed: 24 public methods, 15
write methods, 9 view methods.

The corrected contract schema was retrieved from StudioNet and includes the
new `approve_commitment`, `reject_pending_approval`,
`cancel_pending_approval`, and `cancel_admitted_commitment` methods.

Live non-owner execution against the corrected deployment registered the test
pool and classified consuming commitments. The resulting on-chain commitments
were `PENDING_OWNER_APPROVAL`, while the pool retained `reserved_units = 0`.
This is direct evidence that classification by an arbitrary caller does not
reserve capacity. Successful owner-only methods were deliberately excluded
from that run.

Some long consensus-backed pytest calls did not return a final local runner
summary before the execution environment ended. This document therefore does
not claim a full green live suite. The updated tests remain the reproducible
coverage harness; run them with the provider account when owner-path coverage
is desired.
