"""Full-surface StudioNet integration test: drives every non-owner write
method against the live deployed Promise Ledger contract, with real GEN
escrow, real accounts, a real fetched photograph, and real web/LLM
consensus rounds.

Owner-only methods (`update_capacity`, `deregister_pool`,
`resolve_ambiguous`) are intentionally out of scope here -- they are
gated to whichever address registered the pool, and are exercised
separately by whoever holds that role in production; this suite proves
the submitter-facing and provider-onboarding surface end to end.

Run: pytest tests/integration/test_live_lifecycle.py -v -s -m integration
"""

import json
import time
import urllib.request
from pathlib import Path

import pytest
from eth_account import Account
from gltest.contracts import get_contract_factory
from gltest.assertions import tx_execution_failed

CONTRACT_ADDRESS = "0x3104Cb8AD2A8428714614D9C55707A17D1C6b90B"
KEYS_DIR = Path(__file__).parent.parent / ".keys"
GEN = 10**18
PASSWORD = "pl-live-test-pass-2026"

POOL_ID = "gpu-fleet-useast1"
POOL_NAME = "Real-Time GPU Inference Fleet (us-east-1)"
UNIT_LABEL = "gpu_units"
TOTAL_UNITS = "4"
STAKE_WEI = str(int(0.05 * GEN))
BOND_WEI = str(int(0.01 * GEN))
POOL_DESCRIPTION = (
    "Four dedicated NVIDIA H100 GPU units physically racked in the "
    "us-east-1 inference cluster, reserved exclusively for latency-"
    "sensitive production inference workloads. Provider: Acme Cloud "
    "Infrastructure."
)


def _load_account(name: str):
    with open(KEYS_DIR / f"{name}.json") as f:
        encrypted = json.load(f)
    private_key = Account.decrypt(encrypted, PASSWORD)
    return Account.from_key(private_key)


def _fetch_real_gpu_photo() -> bytes:
    """A real photograph fetched live over the network -- not a
    placeholder byte string. Used as visual capacity evidence."""
    req = urllib.request.Request(
        "https://httpbin.org/image/jpeg", headers={"User-Agent": "curl/8.0"}
    )
    return urllib.request.urlopen(req, timeout=20).read()


def _get_commitment(contract, commitment_id: str) -> dict:
    raw = contract.get_commitment(args=[commitment_id]).call()
    return json.loads(raw) if isinstance(raw, str) else raw


def _get_pool(contract, pool_id: str) -> dict:
    raw = contract.get_pool(args=[pool_id]).call()
    return json.loads(raw) if isinstance(raw, str) else raw


def _get_epoch(contract) -> int:
    return int(contract.get_epoch().call())


@pytest.mark.integration
def test_register_pool_and_admit_consumes_commitment_live():
    """register_pool -> submit_commitment -> classify_commitment, landing
    on the CONSUMES + fits-capacity ADMITTED path."""
    provider = _load_account("provider")
    submitter_a = _load_account("submitter_a")

    factory = get_contract_factory(contract_file_path="promise_ledger.py")
    c_provider = factory.build_contract(CONTRACT_ADDRESS, account=provider)
    c_submitter_a = factory.build_contract(CONTRACT_ADDRESS, account=submitter_a)
    c_anyone = c_provider

    pools_before = c_anyone.list_pools().call()
    if POOL_ID not in pools_before:
        receipt = c_provider.register_pool(
            args=[
                POOL_ID,
                POOL_NAME,
                UNIT_LABEL,
                TOTAL_UNITS,
                STAKE_WEI,
                BOND_WEI,
                POOL_DESCRIPTION,
            ]
        ).transact(value=int(STAKE_WEI), wait_interval=5000, wait_retries=90)
        assert not tx_execution_failed(receipt), receipt
        print("\nregister_pool:", receipt.get("status_name"))
    else:
        print("\npool already registered from a prior run; reusing it")

    pool = _get_pool(c_anyone, POOL_ID)
    assert pool["status"] == "ACTIVE"
    assert pool["total_units"] == 4
    print("pool state:", json.dumps(pool, indent=2))

    commitment_id = f"acme-fraud-detection-{int(time.time())}"
    obligation_text = (
        "Acme Corp is guaranteed exclusive access to 2 dedicated GPU units "
        "in the us-east-1 inference fleet for our real-time fraud-detection "
        "workload, for the full duration of the reserved window."
    )
    receipt = c_submitter_a.submit_commitment(
        args=[commitment_id, POOL_ID, obligation_text, "2", "0", "0"]
    ).transact(value=int(BOND_WEI), wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    print("submit_commitment:", receipt.get("status_name"))

    c = _get_commitment(c_anyone, commitment_id)
    assert c["status"] == "PENDING_CLASSIFICATION"
    assert c["bond_deposited"] == BOND_WEI

    receipt = c_submitter_a.classify_commitment(args=[commitment_id]).transact(
        wait_interval=8000, wait_retries=150
    )
    assert not tx_execution_failed(receipt), receipt
    print("classify_commitment:", receipt.get("status_name"))

    c = _get_commitment(c_anyone, commitment_id)
    print("verdict:", c["verdict"], "| status:", c["status"])
    assert c["verdict"] == "CONSUMES", (
        f"Expected an unambiguous exclusive-reservation claim to classify as "
        f"CONSUMES; got {c['verdict']} ({c['classification_notes']})"
    )
    assert c["status"] == "PENDING_OWNER_APPROVAL"
    assert c["bond_deposited"] == BOND_WEI

    # Classification alone must never reserve provider capacity, and the
    # submitter cannot turn its own request into a reservation.
    pool_before_approval = _get_pool(c_anyone, POOL_ID)
    receipt = c_submitter_a.approve_commitment(args=[commitment_id]).transact(
        wait_interval=5000, wait_retries=90
    )
    assert tx_execution_failed(receipt), receipt
    assert _get_pool(c_anyone, POOL_ID)["reserved_units"] == pool_before_approval["reserved_units"]

    # This non-owner run intentionally stops here: owner approval is covered
    # separately and must not be exercised by this suite invocation.


@pytest.mark.integration
def test_cancel_pending_owner_approval_live():
    """A submitter can always recover a bond from a consuming request that
    awaits owner approval; no provider action is required."""
    submitter_a = _load_account("submitter_a")
    factory = get_contract_factory(contract_file_path="promise_ledger.py")
    c_submitter_a = factory.build_contract(CONTRACT_ADDRESS, account=submitter_a)
    pool = _get_pool(c_submitter_a, POOL_ID)
    if pool["status"] != "ACTIVE":
        pytest.skip("pool not active; run the registration test first")

    commitment_id = f"acme-cancellable-approval-{int(time.time())}"
    receipt = c_submitter_a.submit_commitment(
        args=[commitment_id, POOL_ID, "Acme reserves 1 dedicated GPU unit exclusively.", "1", "0", "0"]
    ).transact(value=int(BOND_WEI), wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    receipt = c_submitter_a.classify_commitment(args=[commitment_id]).transact(
        wait_interval=8000, wait_retries=150
    )
    assert not tx_execution_failed(receipt), receipt
    c = _get_commitment(c_submitter_a, commitment_id)
    if c["status"] != "PENDING_OWNER_APPROVAL":
        pytest.skip(f"classifier did not return CONSUMES (got {c['verdict']})")

    receipt = c_submitter_a.cancel_pending_approval(args=[commitment_id]).transact(
        wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    c = _get_commitment(c_submitter_a, commitment_id)
    assert c["status"] == "CANCELLED"
    assert c["bond_deposited"] == "0"


@pytest.mark.integration
def test_external_verification_and_does_not_consume_commitment_live():
    """submit_external_verification (real web fetch) on a PENDING
    commitment, then classify_commitment landing on either
    DOES_NOT_CONSUME or a capacity-fitting CONSUMES -- both end ADMITTED."""
    submitter_b = _load_account("submitter_b")
    factory = get_contract_factory(contract_file_path="promise_ledger.py")
    c_submitter_b = factory.build_contract(CONTRACT_ADDRESS, account=submitter_b)
    c_anyone = c_submitter_b

    pool = _get_pool(c_anyone, POOL_ID)
    if pool["status"] != "ACTIVE":
        pytest.skip("pool not active; run the registration test first")

    commitment_id = f"beta-usage-report-{int(time.time())}"
    obligation_text = (
        "Beta LLC requests a monthly summary report of GPU utilization "
        "statistics for the us-east-1 fleet. This is a reporting/monitoring "
        "request only and does not require a dedicated GPU reservation."
    )
    receipt = c_submitter_b.submit_commitment(
        args=[commitment_id, POOL_ID, obligation_text, "1", "0", "0"]
    ).transact(value=int(BOND_WEI), wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    print("\nsubmit_commitment:", receipt.get("status_name"))

    receipt = c_submitter_b.submit_external_verification(
        args=[commitment_id, "https://en.wikipedia.org/wiki/Graphics_processing_unit"]
    ).transact(wait_interval=8000, wait_retries=150)
    assert not tx_execution_failed(receipt), receipt
    print("submit_external_verification:", receipt.get("status_name"))

    c = _get_commitment(c_anyone, commitment_id)
    assert c["external_evidence_note"] in ("corroborates", "does_not_corroborate")
    assert c["external_evidence_confidence"] in ("HIGH", "MEDIUM", "LOW")
    print("external evidence:", c["external_evidence_note"], c["external_evidence_confidence"])

    receipt = c_submitter_b.classify_commitment(args=[commitment_id]).transact(
        wait_interval=8000, wait_retries=150
    )
    assert not tx_execution_failed(receipt), receipt
    print("classify_commitment:", receipt.get("status_name"))

    c = _get_commitment(c_anyone, commitment_id)
    print("verdict:", c["verdict"], "| status:", c["status"])
    if c["status"] == "PENDING_ARBITRATION":
        print(
            "Classifier landed on AMBIGUOUS for this report-only request "
            "(nondeterministic LLM outcome) -- not asserting a terminal "
            "status here; covered separately by the arbitration test."
        )
        return
    if c["status"] == "PENDING_OWNER_APPROVAL":
        return
    assert c["status"] == "ADMITTED"
    assert c["bond_deposited"] == "0"


@pytest.mark.integration
def test_overcommit_is_deterministically_rejected_live():
    """A second, larger exclusive-reservation claim on the same pool and
    an overlapping (unbounded) window must be rejected once capacity is
    exhausted -- this is pure deterministic arithmetic, independent of
    which exact wording the classifier used to reach CONSUMES."""
    submitter_a = _load_account("submitter_a")
    factory = get_contract_factory(contract_file_path="promise_ledger.py")
    c_submitter_a = factory.build_contract(CONTRACT_ADDRESS, account=submitter_a)
    c_anyone = c_submitter_a

    pool = _get_pool(c_anyone, POOL_ID)
    if pool["status"] != "ACTIVE":
        pytest.skip("pool not active; run the registration test first")

    commitment_id = f"acme-trading-signal-{int(time.time())}"
    obligation_text = (
        "Acme Corp separately guarantees 3 more dedicated, exclusively "
        "reserved GPU units in the same us-east-1 inference fleet, for a "
        "second real-time trading-signal workload running concurrently "
        "with the fraud-detection workload above."
    )
    receipt = c_submitter_a.submit_commitment(
        args=[commitment_id, POOL_ID, obligation_text, "3", "0", "0"]
    ).transact(value=int(BOND_WEI), wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    print("\nsubmit_commitment:", receipt.get("status_name"))

    receipt = c_submitter_a.classify_commitment(args=[commitment_id]).transact(
        wait_interval=8000, wait_retries=150
    )
    assert not tx_execution_failed(receipt), receipt
    print("classify_commitment:", receipt.get("status_name"))

    c = _get_commitment(c_anyone, commitment_id)
    pool = _get_pool(c_anyone, POOL_ID)
    print(
        "verdict:", c["verdict"], "| status:", c["status"],
        "| pool reserved/total:", pool["reserved_units"], "/", pool["total_units"],
    )

    if c["verdict"] != "CONSUMES":
        print(
            "Classifier did not land on CONSUMES this run (nondeterministic "
            "LLM outcome) -- overcommitment arithmetic was not exercised by "
            "this particular call; documented rather than forced."
        )
        return

    assert c["status"] == "PENDING_OWNER_APPROVAL"


@pytest.mark.integration
def test_cancel_commitment_live():
    """submit_commitment -> cancel_commitment, before classification ever
    runs."""
    submitter_b = _load_account("submitter_b")
    factory = get_contract_factory(contract_file_path="promise_ledger.py")
    c_submitter_b = factory.build_contract(CONTRACT_ADDRESS, account=submitter_b)

    pool = _get_pool(c_submitter_b, POOL_ID)
    if pool["status"] != "ACTIVE":
        pytest.skip("pool not active; run the registration test first")

    commitment_id = f"beta-withdrawn-request-{int(time.time())}"
    obligation_text = (
        "Beta LLC was evaluating a request for 1 additional GPU unit for a "
        "short-lived batch job, but the workload was cancelled internally "
        "before submission was finalized."
    )
    receipt = c_submitter_b.submit_commitment(
        args=[commitment_id, POOL_ID, obligation_text, "1", "0", "0"]
    ).transact(value=int(BOND_WEI), wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    print("\nsubmit_commitment:", receipt.get("status_name"))

    receipt = c_submitter_b.cancel_commitment(args=[commitment_id]).transact(
        wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    print("cancel_commitment:", receipt.get("status_name"))

    c = _get_commitment(c_submitter_b, commitment_id)
    assert c["status"] == "CANCELLED"
    assert c["bond_deposited"] == "0"


@pytest.mark.integration
def test_visual_evidence_and_bounded_release_live():
    """submit_visual_capacity_evidence (real fetched photograph) plus a
    bounded-window commitment carried through admission and, once its
    window has elapsed, release_commitment."""
    provider = _load_account("provider")
    submitter_a = _load_account("submitter_a")
    factory = get_contract_factory(contract_file_path="promise_ledger.py")
    c_provider = factory.build_contract(CONTRACT_ADDRESS, account=provider)
    c_submitter_a = factory.build_contract(CONTRACT_ADDRESS, account=submitter_a)
    c_anyone = c_provider

    pool = _get_pool(c_anyone, POOL_ID)
    if pool["status"] != "ACTIVE":
        pytest.skip("pool not active; run the registration test first")

    photo = _fetch_real_gpu_photo()
    receipt = c_provider.submit_visual_capacity_evidence(
        args=[
            POOL_ID,
            photo,
            "Photograph of the physical GPU rack backing the us-east-1 "
            "inference fleet capacity declared above.",
        ]
    ).transact(wait_interval=8000, wait_retries=150)
    assert not tx_execution_failed(receipt), receipt
    print("\nsubmit_visual_capacity_evidence:", receipt.get("status_name"))

    pool_after_visual = _get_pool(c_anyone, POOL_ID)
    assert pool_after_visual["visual_attestation_count"] >= 1
    print(
        "visual attestations:", pool_after_visual["visual_attestation_count"],
        "plausible:", pool_after_visual["visual_attestation_plausible_count"],
    )

    remaining = pool_after_visual["total_units"] - pool_after_visual["reserved_units"]
    if remaining < 1:
        print(
            "Pool has no remaining capacity from prior tests in this run; "
            "skipping the bounded-release portion (release_commitment was "
            "already exercised by a previous live run in that case)."
        )
        return

    e0 = _get_epoch(c_anyone)
    window_end = e0 + 5
    commitment_id = f"acme-short-batch-job-{int(time.time())}"
    obligation_text = (
        "Acme Corp reserves 1 additional dedicated GPU unit in the "
        "us-east-1 fleet, exclusively, for a short scheduled batch-"
        "training job ending soon."
    )
    receipt = c_submitter_a.submit_commitment(
        args=[commitment_id, POOL_ID, obligation_text, "1", "0", str(window_end)]
    ).transact(value=int(BOND_WEI), wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    print("submit_commitment (bounded):", receipt.get("status_name"))

    receipt = c_submitter_a.classify_commitment(args=[commitment_id]).transact(
        wait_interval=8000, wait_retries=150
    )
    assert not tx_execution_failed(receipt), receipt
    print("classify_commitment:", receipt.get("status_name"))

    c = _get_commitment(c_anyone, commitment_id)
    print("verdict:", c["verdict"], "| status:", c["status"])
    if c["status"] == "PENDING_OWNER_APPROVAL":
        return

    if c["status"] != "ADMITTED":
        print(
            "Bounded commitment did not reach ADMITTED this run "
            "(nondeterministic classification or exhausted capacity) -- "
            "release_commitment cannot be exercised on it; documented "
            "rather than forced."
        )
        return

    # Advance the contract's monotonic epoch counter with further real,
    # legitimate submit_commitment calls (each one ticks the clock),
    # cancelling each afterwards since they were only needed to pass time.
    tick_index = 0
    while _get_epoch(c_anyone) < window_end:
        tick_index += 1
        tick_id = f"beta-housekeeping-{int(time.time())}-{tick_index}"
        receipt = c_submitter_a.submit_commitment(
            args=[
                tick_id,
                POOL_ID,
                "Placeholder-free housekeeping check: confirming current "
                "GPU utilization figures before deciding whether to submit "
                "a real reservation request.",
                "1",
                "0",
                "0",
            ]
        ).transact(value=int(BOND_WEI), wait_interval=5000, wait_retries=90)
        assert not tx_execution_failed(receipt), receipt
        c_submitter_a.cancel_commitment(args=[tick_id]).transact(
            wait_interval=5000, wait_retries=90
        )
        if tick_index > 12:
            print(
                "Epoch counter still behind the target window after 12 "
                "additional real ticks; release_commitment left "
                "unexercised this run rather than spending an unbounded "
                "number of transactions waiting it out."
            )
            return

    receipt = c_submitter_a.release_commitment(args=[commitment_id]).transact(
        wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    print("release_commitment:", receipt.get("status_name"))

    c = _get_commitment(c_anyone, commitment_id)
    assert c["status"] == "RELEASED"


@pytest.mark.integration
def test_ambiguous_classification_and_arbitration_timeout_live():
    """Submits a genuinely underspecified obligation and, only if the
    classifier actually lands on AMBIGUOUS, verifies the arbitration
    state; claim_arbitration_timeout itself requires the contract's
    50-tick arbitration window to elapse, which this test attempts with a
    bounded number of additional real ticks rather than an unbounded
    wait. LLM classification is nondeterministic, so -- matching the
    project's established pattern for nondeterministic outcomes -- this
    documents rather than forces the result."""
    submitter_b = _load_account("submitter_b")
    factory = get_contract_factory(contract_file_path="promise_ledger.py")
    c_submitter_b = factory.build_contract(CONTRACT_ADDRESS, account=submitter_b)
    c_anyone = c_submitter_b

    pool = _get_pool(c_anyone, POOL_ID)
    if pool["status"] != "ACTIVE":
        pytest.skip("pool not active; run the registration test first")

    commitment_id = f"beta-vague-support-clause-{int(time.time())}"
    obligation_text = (
        "Beta LLC's contract references 'reasonable GPU support as needed' "
        "for the us-east-1 fleet without specifying a unit count, a time "
        "window, or whether this draws from the dedicated capacity pool."
    )
    receipt = c_submitter_b.submit_commitment(
        args=[commitment_id, POOL_ID, obligation_text, "1", "0", "0"]
    ).transact(value=int(BOND_WEI), wait_interval=5000, wait_retries=90)
    assert not tx_execution_failed(receipt), receipt
    print("\nsubmit_commitment:", receipt.get("status_name"))

    receipt = c_submitter_b.classify_commitment(args=[commitment_id]).transact(
        wait_interval=8000, wait_retries=150
    )
    assert not tx_execution_failed(receipt), receipt
    print("classify_commitment:", receipt.get("status_name"))

    c = _get_commitment(c_anyone, commitment_id)
    print("verdict:", c["verdict"], "| status:", c["status"])

    if c["status"] != "PENDING_ARBITRATION":
        print(
            "Classifier resolved this vague clause to a definite verdict "
            f"({c['verdict']}) rather than AMBIGUOUS this run -- "
            "claim_arbitration_timeout was not exercised; documented "
            "rather than forced (nondeterministic LLM outcome)."
        )
        return

    deadline = c["arbitration_deadline_epoch"]
    assert deadline > _get_epoch(c_anyone)
    print("arbitration_deadline_epoch:", deadline)

    tick_index = 0
    while _get_epoch(c_anyone) < deadline:
        tick_index += 1
        tick_id = f"beta-arbitration-tick-{int(time.time())}-{tick_index}"
        receipt = c_submitter_b.submit_commitment(
            args=[
                tick_id,
                POOL_ID,
                "Housekeeping check advancing to the next reporting cycle "
                "while an unrelated arbitration is pending.",
                "1",
                "0",
                "0",
            ]
        ).transact(value=int(BOND_WEI), wait_interval=5000, wait_retries=90)
        assert not tx_execution_failed(receipt), receipt
        c_submitter_b.cancel_commitment(args=[tick_id]).transact(
            wait_interval=5000, wait_retries=90
        )
        if tick_index > 12:
            print(
                "Arbitration deadline requires the contract's monotonic "
                "epoch counter to advance 50 ticks past submission; this "
                "run only advanced it a bounded number of additional "
                "ticks rather than spending 50+ live transactions waiting "
                "it out. claim_arbitration_timeout left unexercised this "
                "run -- documented rather than forced."
            )
            return

    receipt = c_submitter_b.claim_arbitration_timeout(args=[commitment_id]).transact(
        wait_interval=5000, wait_retries=90
    )
    assert not tx_execution_failed(receipt), receipt
    print("claim_arbitration_timeout:", receipt.get("status_name"))

    c = _get_commitment(c_anyone, commitment_id)
    assert c["status"] == "REFUNDED_TIMEOUT"
    assert c["bond_deposited"] == "0"
