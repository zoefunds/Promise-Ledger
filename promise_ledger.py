# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

# ============================================================================
# PROMISE LEDGER
# ----------------------------------------------------------------------------
# A semantic capacity / overcommitment-prevention primitive for autonomous
# services and agents.
#
# Problem this solves:
#   A provider (an agent, a support desk, a compute fleet, a logistics
#   operator, an SLA vendor...) can accept multiple natural-language
#   commitments that are each individually reasonable ("a responder is
#   reserved exclusively for you") but that collectively exceed the
#   provider's real, finite capacity when their resource requirements are
#   combined. Promise Ledger prevents this by forcing every commitment
#   through:
#
#     1. A DETERMINISTIC capacity ledger (units, time windows, exclusivity)
#        that is never touched by an LLM. Numbers, totals, and overlap
#        arithmetic are pure Python integer math.
#     2. A CONSENSUS-VERIFIED semantic classification step, used ONLY to
#        map a free-text obligation onto an already-registered capacity
#        category and to decide whether fulfilling it actually consumes
#        that category's capacity. The model is never allowed to invent a
#        resource, invent a unit count, or decide whether overcommitment
#        is acceptable -- it may only answer a bounded 3-way question.
#     3. An ESCROW layer so that submitting a commitment for classification
#        has real economic weight (anti-spam / anti-griefing bond) and so
#        that funds can never be permanently stuck: every escrow path is
#        enumerated up front (admit / reject / bad-faith-forfeit / cancel /
#        timeout-recovery) and every one of them follows the same
#        zero-the-ledger-then-transfer ordering, so double payout is
#        structurally impossible.
#
# Why this won't return an UNDETERMINED consensus result:
#   - Every nondeterministic (LLM / web / vision) operation is wrapped in a
#     `gl.vm.run_nondet_unsafe(leader_fn, validator_fn)` pair where
#     `validator_fn` ALWAYS returns a strict `bool` -- never `None`, never a
#     raised exception that isn't a classified `gl.vm.UserError`.
#   - Every LLM answer is parsed through a defensive, key-aliasing parser
#     that collapses any malformed/verbose/off-schema response down to one
#     of a small closed set of enum values. There is no open-ended free
#     text compared verbatim between validators anywhere in the consensus
#     path, which is the #1 cause of spurious validator disagreement.
#   - All money fields are u256. All identifiers are strings. All time is a
#     contract-local monotonic epoch counter (incremented deterministically
#     by write calls), never wall-clock time, so validators can never
#     disagree because they observed different real-world timestamps.
#   - Comparative validators use bucketed/tolerant comparison (confidence
#     buckets, boolean verdicts, rounded unit estimates) instead of exact
#     string/float equality, so benign LLM phrasing drift cannot break
#     consensus.
#   - Every error path is classified with [EXPECTED] / [EXTERNAL] /
#     [TRANSIENT] / [LLM_ERROR] prefixes and validators react accordingly
#     (see `_handle_leader_error`), so failures resolve to a definite
#     agree/disagree instead of an ambiguous VM crash.
# ============================================================================

from genlayer import *
from dataclasses import dataclass
import typing
import json
import re

# ----------------------------------------------------------------------------
# Error classification prefixes
# ----------------------------------------------------------------------------
ERROR_EXPECTED = "[EXPECTED]"      # deterministic business-logic rejection
ERROR_EXTERNAL = "[EXTERNAL]"      # external API / web 4xx (deterministic)
ERROR_TRANSIENT = "[TRANSIENT]"    # network / 5xx (retry-worthy, non-fatal)
ERROR_LLM = "[LLM_ERROR]"          # LLM produced unusable output

# ----------------------------------------------------------------------------
# Bounded enums (always stored/compared as `str`, never as Python Enum)
# ----------------------------------------------------------------------------
POOL_STATUS_ACTIVE = "ACTIVE"
POOL_STATUS_DEREGISTERED = "DEREGISTERED"

COMMIT_STATUS_PENDING_CLASSIFICATION = "PENDING_CLASSIFICATION"
COMMIT_STATUS_PENDING_ARBITRATION = "PENDING_ARBITRATION"
COMMIT_STATUS_PENDING_OWNER_APPROVAL = "PENDING_OWNER_APPROVAL"
COMMIT_STATUS_ADMITTED = "ADMITTED"
COMMIT_STATUS_REJECTED_OVERCOMMIT = "REJECTED_OVERCOMMIT"
COMMIT_STATUS_REJECTED_BAD_FAITH = "REJECTED_BAD_FAITH"
COMMIT_STATUS_REJECTED_BY_OWNER = "REJECTED_BY_OWNER"
COMMIT_STATUS_REFUNDED_TIMEOUT = "REFUNDED_TIMEOUT"
COMMIT_STATUS_CANCELLED = "CANCELLED"
COMMIT_STATUS_RELEASED = "RELEASED"

VERDICT_CONSUMES = "CONSUMES"
VERDICT_DOES_NOT_CONSUME = "DOES_NOT_CONSUME"
VERDICT_AMBIGUOUS = "AMBIGUOUS"
VERDICT_UNSET = ""

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"

RULING_ADMIT = "ADMIT"
RULING_REJECT_BAD_FAITH = "REJECT_BAD_FAITH"

_ALLOWED_VERDICTS = (VERDICT_CONSUMES, VERDICT_DOES_NOT_CONSUME, VERDICT_AMBIGUOUS)
_ALLOWED_CONFIDENCE = (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW)
_ID_RE = re.compile(r"^[a-zA-Z0-9_\-\.:]{1,64}$")


# ============================================================================
# Storage dataclasses
# ============================================================================

@allow_storage
@dataclass
class CapacityPool:
    pool_id: str
    owner: Address
    name: str
    unit_label: str
    total_units: u256
    reserved_units: u256                 # sum of currently-admitted CONSUMES commitments
    status: str
    registration_stake_wei: u256
    stake_deposited: u256
    bond_wei_per_commitment: u256        # fixed, owner-set, deterministic bond price
    active_commitment_count: u256
    visual_attestation_count: u256
    visual_attestation_plausible_count: u256
    created_epoch: u256
    description: str


@allow_storage
@dataclass
class Commitment:
    commitment_id: str
    pool_id: str
    submitter: Address
    obligation_text: str
    requested_units: u256
    window_start_epoch: u256             # 0 == unbounded start
    window_end_epoch: u256               # 0 == unbounded end
    status: str
    verdict: str
    confidence: str
    matched_pool_confirmed: bool
    bond_wei: u256
    bond_deposited: u256
    submitted_epoch: u256
    arbitration_deadline_epoch: u256
    classification_notes: str
    external_evidence_note: str
    external_evidence_confidence: str


@allow_storage
@dataclass
class VisualAttestation:
    pool_id: str
    submitter: Address
    plausible: bool
    notes: str
    submitted_epoch: u256


# ============================================================================
# EVM transfer interface -- every GEN payout in this contract funnels through
# `_send_gen`, which is the single emission choke point.
# ============================================================================

@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


class PromiseLedger(gl.Contract):
    # ---- global config / admin -------------------------------------------
    admin: Address
    epoch_counter: u256

    # ---- pools --------------------------------------------------------
    pools: TreeMap[str, CapacityPool]
    pool_order: DynArray[str]

    # ---- commitments -------------------------------------------------
    commitments: TreeMap[str, Commitment]
    commitment_order: DynArray[str]

    # ---- visual evidence ------------------------------------------------
    visual_attestations: TreeMap[str, VisualAttestation]
    visual_attestation_order: DynArray[str]

    # ---- O(1) global stats -------------------------------------------
    stats: TreeMap[str, u256]

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def __init__(self):
        self.admin = gl.message.sender_address
        self.epoch_counter = u256(0)
        self.stats["total_pools"] = u256(0)
        self.stats["total_commitments"] = u256(0)
        self.stats["total_admitted"] = u256(0)
        self.stats["total_rejected_overcommit"] = u256(0)
        self.stats["total_rejected_bad_faith"] = u256(0)
        self.stats["total_rejected_by_owner"] = u256(0)
        self.stats["total_cancelled"] = u256(0)
        self.stats["total_timeout_refunds"] = u256(0)
        self.stats["total_ambiguous"] = u256(0)
        self.stats["total_bond_wei_escrowed"] = u256(0)
        self.stats["total_bond_wei_refunded"] = u256(0)
        self.stats["total_bond_wei_forfeited"] = u256(0)
        self.stats["total_stake_wei_escrowed"] = u256(0)
        self.stats["total_visual_attestations"] = u256(0)

    # ------------------------------------------------------------------
    # Internal helpers -- determinism, validation, escrow
    # ------------------------------------------------------------------

    def _tick(self) -> u256:
        """Contract-local monotonic logical clock. Never wall-clock time --
        this keeps every window/timeout comparison fully deterministic
        across leader and validators."""
        self.epoch_counter = self.epoch_counter + u256(1)
        return self.epoch_counter

    def _require_valid_id(self, value: str, field_name: str) -> None:
        if not value or not _ID_RE.match(value):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Invalid {field_name}: must match "
                f"[a-zA-Z0-9_\\-.:]{{1,64}}"
            )

    def _get_pool_or_raise(self, pool_id: str) -> CapacityPool:
        if pool_id not in self.pools:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Unknown pool_id: {pool_id}")
        return self.pools[pool_id]

    def _get_commitment_or_raise(self, commitment_id: str) -> Commitment:
        if commitment_id not in self.commitments:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Unknown commitment_id: {commitment_id}"
            )
        return self.commitments[commitment_id]

    def _require_pool_active(self, pool: CapacityPool) -> None:
        if pool.status != POOL_STATUS_ACTIVE:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Pool {pool.pool_id} is not active "
                f"(status={pool.status})"
            )

    def _require_sender(self, expected: Address, action: str) -> None:
        if gl.message.sender_address != expected:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only {expected} may {action}"
            )

    def _bump_stat(self, key: str, amount: u256) -> None:
        current = self.stats.get(key, u256(0))
        self.stats[key] = current + amount

    def _send_gen(self, to_address: Address, amount: u256) -> None:
        """The single emission point. No payout anywhere in this contract
        calls the EVM transfer interface directly -- everything routes
        through here so the entire escrow surface can be audited by
        grepping one function name."""
        if not to_address:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Missing recipient address")
        if amount <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Transfer amount must be positive")
        _Recipient(to_address).emit_transfer(value=amount)

    def _windows_overlap(
        self,
        a_start: u256,
        a_end: u256,
        b_start: u256,
        b_end: u256,
    ) -> bool:
        """0 means unbounded on that side. Pure integer interval-overlap
        math -- fully deterministic, no floats, no wall-clock."""
        a_start_i = int(a_start)
        a_end_i = int(a_end)
        b_start_i = int(b_start)
        b_end_i = int(b_end)

        effective_a_end = a_end_i if a_end_i > 0 else 2**256 - 1
        effective_b_end = b_end_i if b_end_i > 0 else 2**256 - 1

        return a_start_i <= effective_b_end and b_start_i <= effective_a_end

    def _reserved_units_overlapping(
        self,
        pool_id: str,
        window_start: u256,
        window_end: u256,
        exclude_commitment_id: str = "",
    ) -> u256:
        """Deterministic scan over the global commitment order, filtered to
        this pool, summing only ADMITTED + CONSUMES commitments whose
        window overlaps the candidate window. This is the arithmetic the
        LLM is never allowed to touch.

        (A per-pool reverse index would make this O(k) instead of O(n),
        but nesting a DynArray inside a dataclass stored as a TreeMap
        value hits a GenVM storage-generic instantiation limitation on
        the currently pinned runner; a linear scan over commitment_order
        is the robust, verified-working alternative and is more than
        sufficient at the scale a single capacity pool operates at.)"""
        total = u256(0)
        for cid in self.commitment_order:
            if cid == exclude_commitment_id:
                continue
            c = self.commitments[cid]
            if c.pool_id != pool_id:
                continue
            if c.status != COMMIT_STATUS_ADMITTED:
                continue
            if c.verdict != VERDICT_CONSUMES:
                continue
            if not self._windows_overlap(
                c.window_start_epoch, c.window_end_epoch, window_start, window_end
            ):
                continue
            total = total + c.requested_units
        return total

    def _parse_json_block(self, text: str) -> dict:
        """Defensive JSON extraction: strips wrapping prose, fixes trailing
        commas. LLMs regularly wrap JSON in markdown fences or commentary."""
        if not isinstance(text, str):
            raise gl.vm.UserError(f"{ERROR_LLM} Non-string LLM output: {type(text)}")
        first = text.find("{")
        last = text.rfind("}")
        if first == -1 or last == -1 or last < first:
            raise gl.vm.UserError(f"{ERROR_LLM} No JSON object found in LLM output")
        candidate = text[first : last + 1]
        candidate = re.sub(r",(?!\s*?[\{\[\"\'\w])", "", candidate)
        try:
            return json.loads(candidate)
        except (ValueError, TypeError) as exc:
            raise gl.vm.UserError(f"{ERROR_LLM} Malformed JSON: {exc}")

    def _coerce_verdict(self, raw: typing.Any) -> str:
        """Collapse any LLM phrasing into exactly one of three closed
        values. Never raises for unusual-but-recognizable phrasing --
        falls back to AMBIGUOUS, which is itself a safe, fully-handled
        terminal-ish state (goes to arbitration, never silently admits)."""
        if not isinstance(raw, str):
            return VERDICT_AMBIGUOUS
        normalized = raw.strip().upper().replace(" ", "_").replace("-", "_")
        if normalized in _ALLOWED_VERDICTS:
            return normalized
        if "NOT" in normalized and "CONSUME" in normalized:
            return VERDICT_DOES_NOT_CONSUME
        if "CONSUME" in normalized:
            return VERDICT_CONSUMES
        if "AMBIG" in normalized or "UNCLEAR" in normalized or "UNKNOWN" in normalized:
            return VERDICT_AMBIGUOUS
        return VERDICT_AMBIGUOUS

    def _coerce_confidence(self, raw: typing.Any) -> str:
        if not isinstance(raw, str):
            return CONFIDENCE_LOW
        normalized = raw.strip().upper()
        if normalized in _ALLOWED_CONFIDENCE:
            return normalized
        if normalized.startswith("H"):
            return CONFIDENCE_HIGH
        if normalized.startswith("M"):
            return CONFIDENCE_MEDIUM
        return CONFIDENCE_LOW

    def _coerce_bool(self, raw: typing.Any) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in ("true", "yes", "1", "matches", "match")
        if isinstance(raw, (int, float)):
            return raw != 0
        return False

    def _handle_leader_error(self, leaders_res, leader_fn) -> bool:
        """Canonical validator-side error reconciliation. Always returns a
        strict bool -- this is what keeps consensus from ever landing on
        an undetermined outcome when the leader path raised."""
        leader_msg = getattr(leaders_res, "message", "")
        try:
            leader_fn()
            # Leader errored but validator succeeded -- outcomes diverge.
            return False
        except gl.vm.UserError as e:
            validator_msg = getattr(e, "message", str(e))
            if validator_msg.startswith(ERROR_EXPECTED) or validator_msg.startswith(
                ERROR_EXTERNAL
            ):
                return validator_msg == leader_msg
            if validator_msg.startswith(ERROR_TRANSIENT) and str(
                leader_msg
            ).startswith(ERROR_TRANSIENT):
                return True
            # LLM error or unrecognized -- force disagreement, triggers rotation.
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # POOL LIFECYCLE
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def register_pool(
        self,
        pool_id: str,
        name: str,
        unit_label: str,
        total_units: str,
        registration_stake_wei: str,
        bond_wei_per_commitment: str,
        description: str,
    ) -> str:
        """Register a fixed, deterministic capacity model, e.g.:
        'one exclusive incident responder', 'ten concurrent support slots',
        'four GPU units', 'two delivery vehicles'. total_units, stakes and
        bonds are supplied by the provider -- the LLM never sets or edits
        these numbers anywhere in this contract."""
        self._require_valid_id(pool_id, "pool_id")
        if pool_id in self.pools:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} pool_id already registered")
        if not name or len(name) > 200:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} name must be 1-200 chars")
        if not unit_label or len(unit_label) > 64:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unit_label must be 1-64 chars")

        try:
            total_units_u = u256(int(total_units))
            stake_u = u256(int(registration_stake_wei))
            bond_u = u256(int(bond_wei_per_commitment))
        except (ValueError, TypeError):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} total_units/registration_stake_wei/"
                f"bond_wei_per_commitment must be non-negative integers"
            )

        if total_units_u <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} total_units must be > 0")

        if gl.message.value <= u256(0):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Pool registration requires a GEN stake"
            )
        if gl.message.value != stake_u:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Sent value must exactly equal "
                f"registration_stake_wei"
            )

        pool = CapacityPool(
            pool_id=pool_id,
            owner=gl.message.sender_address,
            name=name,
            unit_label=unit_label,
            total_units=total_units_u,
            reserved_units=u256(0),
            status=POOL_STATUS_ACTIVE,
            registration_stake_wei=stake_u,
            stake_deposited=gl.message.value,
            bond_wei_per_commitment=bond_u,
            active_commitment_count=u256(0),
            visual_attestation_count=u256(0),
            visual_attestation_plausible_count=u256(0),
            created_epoch=self._tick(),
            description=description[:1000] if description else "",
        )
        self.pools[pool_id] = pool
        self.pool_order.append(pool_id)

        self._bump_stat("total_pools", u256(1))
        self._bump_stat("total_stake_wei_escrowed", gl.message.value)
        return pool_id

    @gl.public.write
    def update_capacity(self, pool_id: str, new_total_units: str) -> None:
        """Owner-only, fully deterministic. A decrease is only allowed
        down to the level of currently reserved units, so an owner cannot
        retroactively create an overcommitment by shrinking the pool."""
        pool = self._get_pool_or_raise(pool_id)
        self._require_sender(pool.owner, "update this pool's capacity")
        self._require_pool_active(pool)

        try:
            new_total_u = u256(int(new_total_units))
        except (ValueError, TypeError):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} new_total_units must be an integer")

        if new_total_u <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} new_total_units must be > 0")
        if new_total_u < pool.reserved_units:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Cannot shrink capacity below currently "
                f"reserved units ({pool.reserved_units})"
            )

        pool.total_units = new_total_u
        self.pools[pool_id] = pool

    @gl.public.write
    def deregister_pool(self, pool_id: str) -> None:
        """Refund path #1: full stake refund, only when the pool has zero
        active commitments (so no in-flight promise can be orphaned)."""
        pool = self._get_pool_or_raise(pool_id)
        self._require_sender(pool.owner, "deregister this pool")
        self._require_pool_active(pool)

        if pool.active_commitment_count > u256(0):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Pool has {pool.active_commitment_count} "
                f"active commitments; resolve or let them release first"
            )

        refund = pool.stake_deposited
        pool.status = POOL_STATUS_DEREGISTERED
        pool.stake_deposited = u256(0)
        self.pools[pool_id] = pool

        if refund > u256(0):
            self._send_gen(pool.owner, refund)

    # ------------------------------------------------------------------
    # COMMITMENT SUBMISSION + ESCROW (custody-in)
    # ------------------------------------------------------------------

    @gl.public.write.payable
    def submit_commitment(
        self,
        commitment_id: str,
        pool_id: str,
        obligation_text: str,
        requested_units: str,
        window_start_epoch: str,
        window_end_epoch: str,
    ) -> str:
        """Caller declares the resource requirement NUMERICALLY
        (requested_units) -- this number is never touched by the model.
        The bond is priced deterministically off the pool's own
        bond_wei_per_commitment, set by the provider at registration."""
        self._require_valid_id(commitment_id, "commitment_id")
        if commitment_id in self.commitments:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} commitment_id already exists")

        pool = self._get_pool_or_raise(pool_id)
        self._require_pool_active(pool)

        if not obligation_text or len(obligation_text) > 4000:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} obligation_text must be 1-4000 chars"
            )

        try:
            requested_units_u = u256(int(requested_units))
            window_start_u = u256(int(window_start_epoch))
            window_end_u = u256(int(window_end_epoch))
        except (ValueError, TypeError):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} requested_units/window_start_epoch/"
                f"window_end_epoch must be non-negative integers"
            )

        if requested_units_u <= u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} requested_units must be > 0")
        if requested_units_u > pool.total_units:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} requested_units ({requested_units_u}) exceeds "
                f"pool total_units ({pool.total_units}); cannot possibly be admitted"
            )
        if (
            int(window_end_u) != 0
            and int(window_start_u) != 0
            and int(window_end_u) < int(window_start_u)
        ):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} window_end_epoch must be >= window_start_epoch"
            )

        required_bond = pool.bond_wei_per_commitment
        if required_bond > u256(0):
            if gl.message.value != required_bond:
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} Must lock exactly bond_wei_per_commitment "
                    f"({required_bond})"
                )
        else:
            if gl.message.value != u256(0):
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} This pool requires no bond; do not send GEN"
                )

        now = self._tick()
        commitment = Commitment(
            commitment_id=commitment_id,
            pool_id=pool_id,
            submitter=gl.message.sender_address,
            obligation_text=obligation_text,
            requested_units=requested_units_u,
            window_start_epoch=window_start_u,
            window_end_epoch=window_end_u,
            status=COMMIT_STATUS_PENDING_CLASSIFICATION,
            verdict=VERDICT_UNSET,
            confidence=CONFIDENCE_LOW,
            matched_pool_confirmed=False,
            bond_wei=required_bond,
            bond_deposited=gl.message.value,
            submitted_epoch=now,
            arbitration_deadline_epoch=u256(0),
            classification_notes="",
            external_evidence_note="",
            external_evidence_confidence="",
        )
        self.commitments[commitment_id] = commitment
        self.commitment_order.append(commitment_id)

        self._bump_stat("total_commitments", u256(1))
        if gl.message.value > u256(0):
            self._bump_stat("total_bond_wei_escrowed", gl.message.value)
        return commitment_id

    @gl.public.write
    def cancel_commitment(self, commitment_id: str) -> None:
        """Refund path #2: cancellation before semantic evaluation has
        run. Reward-only-style refund, mirroring the reference pattern's
        cancel_milestone: only available pre-commitment of the counterparty
        (here: pre-classification)."""
        c = self._get_commitment_or_raise(commitment_id)
        self._require_sender(c.submitter, "cancel this commitment")
        if c.status != COMMIT_STATUS_PENDING_CLASSIFICATION:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only PENDING_CLASSIFICATION commitments can "
                f"be cancelled (status={c.status})"
            )

        refund = c.bond_deposited
        c.status = COMMIT_STATUS_CANCELLED
        c.bond_deposited = u256(0)
        self.commitments[commitment_id] = c

        self._bump_stat("total_cancelled", u256(1))
        if refund > u256(0):
            self._bump_stat("total_bond_wei_refunded", refund)
            self._send_gen(c.submitter, refund)

    @gl.public.write
    def cancel_pending_approval(self, commitment_id: str) -> None:
        """Submitter escape hatch for a consuming request that is waiting
        for provider approval. This prevents a silent owner from trapping
        the submitter's bond in the approval queue."""
        c = self._get_commitment_or_raise(commitment_id)
        self._require_sender(c.submitter, "cancel this pending approval")
        if c.status != COMMIT_STATUS_PENDING_OWNER_APPROVAL:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only PENDING_OWNER_APPROVAL commitments can "
                f"be cancelled (status={c.status})"
            )

        refund = c.bond_deposited
        c.status = COMMIT_STATUS_CANCELLED
        c.bond_deposited = u256(0)
        self.commitments[commitment_id] = c
        self._bump_stat("total_cancelled", u256(1))
        if refund > u256(0):
            self._bump_stat("total_bond_wei_refunded", refund)
            self._send_gen(c.submitter, refund)

    # ------------------------------------------------------------------
    # SEMANTIC CLASSIFICATION -- the only place an LLM touches this contract's
    # decision path, and only over a bounded 3-way question.
    # ------------------------------------------------------------------

    @gl.public.write
    def classify_commitment(self, commitment_id: str) -> str:
        c = self._get_commitment_or_raise(commitment_id)
        if c.status != COMMIT_STATUS_PENDING_CLASSIFICATION:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Commitment is not PENDING_CLASSIFICATION "
                f"(status={c.status})"
            )
        pool = self._get_pool_or_raise(c.pool_id)
        self._require_pool_active(pool)

        obligation_text = c.obligation_text
        pool_name = pool.name
        unit_label = pool.unit_label
        total_units = int(pool.total_units)
        requested_units = int(c.requested_units)

        def leader_fn() -> dict:
            prompt = (
                "You are a strict semantic classifier for a capacity ledger. "
                "You must NOT invent new resource categories, you must NOT "
                "change any numbers, and you must NOT decide whether "
                "overcommitment is acceptable -- a separate deterministic "
                "system does that. Answer only the classification question "
                "below.\n\n"
                f"Registered capacity category: \"{pool_name}\" "
                f"(unit: \"{unit_label}\", total capacity: {total_units} "
                f"{unit_label}).\n\n"
                f"A party has declared this natural-language obligation:\n"
                f"\"\"\"{obligation_text}\"\"\"\n\n"
                f"They claim it requires {requested_units} {unit_label} from "
                f"the category above.\n\n"
                "Answer exactly these two questions as JSON:\n"
                "1. matched_pool_confirmed: does the obligation's resource "
                "genuinely belong to the registered category above (true), "
                "or is it describing an unrelated/different resource (false)?\n"
                "2. verdict: does fulfilling this obligation consume "
                "capacity from that category? Answer exactly one of: "
                "\"CONSUMES\", \"DOES_NOT_CONSUME\", \"AMBIGUOUS\". Use "
                "AMBIGUOUS only if the text genuinely does not give enough "
                "information to decide.\n"
                "3. confidence: one of \"HIGH\", \"MEDIUM\", \"LOW\".\n"
                "4. notes: one short sentence explaining the verdict.\n\n"
                "Respond as JSON: {\"matched_pool_confirmed\": true/false, "
                "\"verdict\": \"...\", \"confidence\": \"...\", "
                "\"notes\": \"...\"}"
            )
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            if isinstance(raw, str):
                parsed = self._parse_json_block(raw)
            elif isinstance(raw, dict):
                parsed = raw
            else:
                raise gl.vm.UserError(
                    f"{ERROR_LLM} Unexpected LLM response type: {type(raw)}"
                )

            verdict = self._coerce_verdict(parsed.get("verdict"))
            confidence = self._coerce_confidence(parsed.get("confidence"))
            matched = self._coerce_bool(parsed.get("matched_pool_confirmed"))
            notes = parsed.get("notes")
            if not isinstance(notes, str):
                notes = ""
            notes = notes[:500]

            # A category mismatch always forces AMBIGUOUS regardless of what
            # the model said about "verdict" -- this is the guardrail that
            # stops the model from silently reclassifying an obligation
            # into a category it does not belong to.
            if not matched:
                verdict = VERDICT_AMBIGUOUS

            return {
                "verdict": verdict,
                "confidence": confidence,
                "matched_pool_confirmed": matched,
                "notes": notes,
            }

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return self._handle_leader_error(leaders_res, leader_fn)

            leader_out = leaders_res.calldata
            if not isinstance(leader_out, dict):
                return False
            if leader_out.get("verdict") not in _ALLOWED_VERDICTS:
                return False

            validator_out = leader_fn()

            # Comparative check on the two fields that actually drive
            # money/capacity movement. Confidence and notes are informational
            # only and are deliberately excluded from the agreement check --
            # comparing free-text notes verbatim is exactly the kind of
            # exact-match trap that produces spurious disagreement.
            if leader_out.get("verdict") != validator_out.get("verdict"):
                return False
            if bool(leader_out.get("matched_pool_confirmed")) != bool(
                validator_out.get("matched_pool_confirmed")
            ):
                return False
            return True

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        verdict = result["verdict"]
        confidence = result["confidence"]
        matched = bool(result["matched_pool_confirmed"])
        notes = result["notes"]

        c.verdict = verdict
        c.confidence = confidence
        c.matched_pool_confirmed = matched
        c.classification_notes = notes

        if verdict == VERDICT_AMBIGUOUS:
            c.status = COMMIT_STATUS_PENDING_ARBITRATION
            c.arbitration_deadline_epoch = self._tick() + u256(50)
            self.commitments[commitment_id] = c
            self._bump_stat("total_ambiguous", u256(1))
            return COMMIT_STATUS_PENDING_ARBITRATION

        if verdict == VERDICT_DOES_NOT_CONSUME:
            # No capacity risk -- admit, and refund the bond immediately
            # since there was never any overcommitment exposure.
            refund = c.bond_deposited
            c.status = COMMIT_STATUS_ADMITTED
            c.bond_deposited = u256(0)
            self.commitments[commitment_id] = c
            self._bump_stat("total_admitted", u256(1))
            if refund > u256(0):
                self._bump_stat("total_bond_wei_refunded", refund)
                self._send_gen(c.submitter, refund)
            return COMMIT_STATUS_ADMITTED

        # A consuming request cannot reserve a provider's pool merely
        # because an arbitrary caller submitted text that classifies as
        # consuming. The owner must explicitly authorize it next.
        c.status = COMMIT_STATUS_PENDING_OWNER_APPROVAL
        self.commitments[commitment_id] = c
        return COMMIT_STATUS_PENDING_OWNER_APPROVAL

    @gl.public.write
    def approve_commitment(self, commitment_id: str) -> str:
        """Owner-only admission authorization for a consuming commitment.
        Capacity arithmetic is intentionally performed only after this
        authorization, so third parties cannot lock a provider's capacity
        or registration stake by submitting/classifying a request."""
        c = self._get_commitment_or_raise(commitment_id)
        pool = self._get_pool_or_raise(c.pool_id)
        self._require_sender(pool.owner, "approve commitments for this pool")
        self._require_pool_active(pool)
        if c.status != COMMIT_STATUS_PENDING_OWNER_APPROVAL:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Commitment is not PENDING_OWNER_APPROVAL "
                f"(status={c.status})"
            )
        if c.verdict != VERDICT_CONSUMES:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only CONSUMES commitments require approval"
            )
        return self._settle_consumes_verdict(commitment_id, c, pool)

    @gl.public.write
    def reject_pending_approval(self, commitment_id: str) -> None:
        """Owner-only refusal with a full bond refund. This gives every
        owner-approval request a definitive, non-custodial exit."""
        c = self._get_commitment_or_raise(commitment_id)
        pool = self._get_pool_or_raise(c.pool_id)
        self._require_sender(pool.owner, "reject commitments for this pool")
        if c.status != COMMIT_STATUS_PENDING_OWNER_APPROVAL:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Commitment is not PENDING_OWNER_APPROVAL "
                f"(status={c.status})"
            )

        refund = c.bond_deposited
        c.status = COMMIT_STATUS_REJECTED_BY_OWNER
        c.bond_deposited = u256(0)
        self.commitments[commitment_id] = c
        self._bump_stat("total_rejected_by_owner", u256(1))
        if refund > u256(0):
            self._bump_stat("total_bond_wei_refunded", refund)
            self._send_gen(c.submitter, refund)

    def _settle_consumes_verdict(
        self, commitment_id: str, c: Commitment, pool: CapacityPool
    ) -> str:
        """Pure deterministic math: no LLM output participates below this
        point. This is the function an auditor should read to verify the
        overcommitment guarantee end to end."""
        already_reserved = self._reserved_units_overlapping(
            pool.pool_id,
            c.window_start_epoch,
            c.window_end_epoch,
            exclude_commitment_id=commitment_id,
        )
        prospective_total = already_reserved + c.requested_units

        if prospective_total <= pool.total_units:
            c.status = COMMIT_STATUS_ADMITTED
            self.commitments[commitment_id] = c

            pool.reserved_units = pool.reserved_units + c.requested_units
            pool.active_commitment_count = pool.active_commitment_count + u256(1)
            self.pools[pool.pool_id] = pool

            self._bump_stat("total_admitted", u256(1))

            # Bond is refundable in full on a clean admit -- it was never at
            # risk of forfeiture, it only gated the classification request.
            refund = c.bond_deposited
            c2 = self.commitments[commitment_id]
            c2.bond_deposited = u256(0)
            self.commitments[commitment_id] = c2
            if refund > u256(0):
                self._bump_stat("total_bond_wei_refunded", refund)
                self._send_gen(c.submitter, refund)
            return COMMIT_STATUS_ADMITTED

        # Overcommitment: reject. Refund path #3 -- rejection due to a full
        # pool is not the submitter's fault, so the bond is still returned
        # in full; only a REJECT_BAD_FAITH ruling (see resolve_ambiguous)
        # forfeits the bond.
        c.status = COMMIT_STATUS_REJECTED_OVERCOMMIT
        refund = c.bond_deposited
        c.bond_deposited = u256(0)
        self.commitments[commitment_id] = c

        self._bump_stat("total_rejected_overcommit", u256(1))
        if refund > u256(0):
            self._bump_stat("total_bond_wei_refunded", refund)
            self._send_gen(c.submitter, refund)
        return COMMIT_STATUS_REJECTED_OVERCOMMIT

    # ------------------------------------------------------------------
    # ARBITRATION for AMBIGUOUS verdicts (owner ruling + timeout recovery)
    # ------------------------------------------------------------------

    @gl.public.write
    def resolve_ambiguous(self, commitment_id: str, ruling: str) -> str:
        """Pool owner breaks an AMBIGUOUS classification. This is a human
        (or owner-controlled agent) decision, not an LLM decision -- the
        contract only ever asked the model a bounded 3-way question, and
        here a deterministic authority resolves what the model could not."""
        c = self._get_commitment_or_raise(commitment_id)
        pool = self._get_pool_or_raise(c.pool_id)
        self._require_sender(pool.owner, "resolve ambiguous commitments")

        if c.status != COMMIT_STATUS_PENDING_ARBITRATION:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Commitment is not PENDING_ARBITRATION "
                f"(status={c.status})"
            )
        if ruling not in (RULING_ADMIT, RULING_REJECT_BAD_FAITH):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} ruling must be ADMIT or REJECT_BAD_FAITH"
            )

        if ruling == RULING_REJECT_BAD_FAITH:
            # Refund path #4: forfeiture. Zero-then-transfer, funds go to
            # the pool owner as compensation for a bad-faith submission.
            forfeited = c.bond_deposited
            c.status = COMMIT_STATUS_REJECTED_BAD_FAITH
            c.bond_deposited = u256(0)
            self.commitments[commitment_id] = c

            self._bump_stat("total_rejected_bad_faith", u256(1))
            if forfeited > u256(0):
                self._bump_stat("total_bond_wei_forfeited", forfeited)
                self._send_gen(pool.owner, forfeited)
            return COMMIT_STATUS_REJECTED_BAD_FAITH

        # RULING_ADMIT: treat as CONSUMES from here on and run the same
        # deterministic capacity check every other admission path uses.
        c.verdict = VERDICT_CONSUMES
        self.commitments[commitment_id] = c
        return self._settle_consumes_verdict(commitment_id, c, pool)

    @gl.public.write
    def claim_arbitration_timeout(self, commitment_id: str) -> None:
        """Refund path #5: timeout recovery. If the pool owner never rules
        on an AMBIGUOUS commitment, the submitter can reclaim their bond
        once the arbitration deadline has passed -- funds can never be
        locked forever waiting on a silent counterparty."""
        c = self._get_commitment_or_raise(commitment_id)
        self._require_sender(c.submitter, "claim this timeout refund")

        if c.status != COMMIT_STATUS_PENDING_ARBITRATION:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Commitment is not PENDING_ARBITRATION "
                f"(status={c.status})"
            )
        if int(self.epoch_counter) < int(c.arbitration_deadline_epoch):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Arbitration deadline has not passed yet"
            )

        refund = c.bond_deposited
        c.status = COMMIT_STATUS_REFUNDED_TIMEOUT
        c.bond_deposited = u256(0)
        self.commitments[commitment_id] = c

        self._bump_stat("total_timeout_refunds", u256(1))
        if refund > u256(0):
            self._bump_stat("total_bond_wei_refunded", refund)
            self._send_gen(c.submitter, refund)

    # ------------------------------------------------------------------
    # RELEASE -- freeing capacity once a commitment's window has elapsed
    # ------------------------------------------------------------------

    @gl.public.write
    def release_commitment(self, commitment_id: str) -> None:
        """Deterministic capacity release once a bounded window has ended.
        Anyone may call this (it only ever frees capacity, never moves
        money), which means expired reservations cannot be kept alive by
        an uncooperative submitter or owner."""
        c = self._get_commitment_or_raise(commitment_id)
        if c.status != COMMIT_STATUS_ADMITTED:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only ADMITTED commitments can be released "
                f"(status={c.status})"
            )
        if int(c.window_end_epoch) == 0:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Commitment has no bounded end window; "
                f"it cannot be auto-released"
            )
        if int(self.epoch_counter) < int(c.window_end_epoch):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Commitment window has not ended yet"
            )

        pool = self._get_pool_or_raise(c.pool_id)
        if c.verdict == VERDICT_CONSUMES:
            if pool.reserved_units >= c.requested_units:
                pool.reserved_units = pool.reserved_units - c.requested_units
            else:
                pool.reserved_units = u256(0)
            if pool.active_commitment_count > u256(0):
                pool.active_commitment_count = pool.active_commitment_count - u256(1)
            self.pools[c.pool_id] = pool

        c.status = COMMIT_STATUS_RELEASED
        self.commitments[commitment_id] = c

    @gl.public.write
    def cancel_admitted_commitment(self, commitment_id: str) -> None:
        """Authorized early release for an admitted commitment. Either the
        provider or the submitter may terminate it; this is the mandatory
        escape hatch for unbounded windows and also safely handles bounded
        reservations before their end epoch."""
        c = self._get_commitment_or_raise(commitment_id)
        pool = self._get_pool_or_raise(c.pool_id)
        sender = gl.message.sender_address
        if sender != c.submitter and sender != pool.owner:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only the submitter or pool owner may cancel "
                f"an admitted commitment"
            )
        if c.status != COMMIT_STATUS_ADMITTED:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only ADMITTED commitments can be cancelled "
                f"(status={c.status})"
            )

        if c.verdict == VERDICT_CONSUMES:
            if pool.reserved_units < c.requested_units or pool.active_commitment_count <= u256(0):
                raise gl.vm.UserError(
                    f"{ERROR_EXPECTED} Inconsistent pool reservation accounting"
                )
            pool.reserved_units = pool.reserved_units - c.requested_units
            pool.active_commitment_count = pool.active_commitment_count - u256(1)
            self.pools[c.pool_id] = pool

        c.status = COMMIT_STATUS_RELEASED
        self.commitments[commitment_id] = c

    # ------------------------------------------------------------------
    # VISUAL EVIDENCE (image interpretation) -- attestation only, never
    # capacity-mutating, exactly to honor "the model must not invent new
    # resources or change capacity numbers."
    # ------------------------------------------------------------------

    @gl.public.write
    def submit_visual_capacity_evidence(
        self, pool_id: str, image_data: bytes, claim_note: str
    ) -> bool:
        """Attach a photo/screenshot (e.g. a GPU rack, a fleet of delivery
        vehicles, a support floor headcount) as supporting evidence for a
        pool's declared capacity. This can only raise or lower a
        credibility counter -- it is structurally incapable of touching
        total_units, reserved_units, or any escrow field."""
        pool = self._get_pool_or_raise(pool_id)
        self._require_pool_active(pool)

        if not image_data:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} image_data must not be empty")
        if not claim_note or len(claim_note) > 500:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} claim_note must be 1-500 chars")

        pool_name = pool.name
        unit_label = pool.unit_label
        total_units = int(pool.total_units)

        def leader_fn() -> dict:
            prompt = (
                "You are verifying visual evidence for a declared capacity "
                "claim on an infrastructure/service ledger. Do not invent "
                "numbers or change the declared total -- only judge "
                "plausibility.\n\n"
                f"Declared capacity: {total_units} {unit_label} for category "
                f"\"{pool_name}\".\n"
                f"Submitter's claim about this image: \"{claim_note}\"\n\n"
                "Answer as JSON: {\"plausible\": true/false, "
                "\"notes\": \"one short sentence\"}"
            )
            raw = gl.nondet.exec_prompt(
                prompt, images=[image_data], response_format="json"
            )
            if isinstance(raw, str):
                parsed = self._parse_json_block(raw)
            elif isinstance(raw, dict):
                parsed = raw
            else:
                raise gl.vm.UserError(
                    f"{ERROR_LLM} Unexpected LLM response type: {type(raw)}"
                )
            plausible = self._coerce_bool(parsed.get("plausible"))
            notes = parsed.get("notes")
            if not isinstance(notes, str):
                notes = ""
            return {"plausible": plausible, "notes": notes[:300]}

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return self._handle_leader_error(leaders_res, leader_fn)
            leader_out = leaders_res.calldata
            if not isinstance(leader_out, dict) or "plausible" not in leader_out:
                return False
            validator_out = leader_fn()
            return bool(leader_out.get("plausible")) == bool(
                validator_out.get("plausible")
            )

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        plausible = bool(result["plausible"])

        attestation_id = f"{pool_id}-visual-{int(pool.visual_attestation_count) + 1}"
        self.visual_attestations[attestation_id] = VisualAttestation(
            pool_id=pool_id,
            submitter=gl.message.sender_address,
            plausible=plausible,
            notes=result["notes"],
            submitted_epoch=self._tick(),
        )
        self.visual_attestation_order.append(attestation_id)

        pool.visual_attestation_count = pool.visual_attestation_count + u256(1)
        if plausible:
            pool.visual_attestation_plausible_count = (
                pool.visual_attestation_plausible_count + u256(1)
            )
        self.pools[pool_id] = pool

        self._bump_stat("total_visual_attestations", u256(1))
        return plausible

    # ------------------------------------------------------------------
    # WEB FETCH -- external corroboration (e.g. an SLA/status/certification
    # page) attached as supplementary, non-authoritative evidence.
    # ------------------------------------------------------------------

    @gl.public.write
    def submit_external_verification(self, commitment_id: str, url: str) -> bool:
        """Fetches an external page and asks whether it corroborates the
        commitment's declared category (e.g. a public SLA page naming the
        same responder/service). This never re-triggers money movement or
        overwrites an already-settled verdict -- it only annotates the
        commitment record for auditors, and only while still pending."""
        c = self._get_commitment_or_raise(commitment_id)
        if c.status not in (
            COMMIT_STATUS_PENDING_CLASSIFICATION,
            COMMIT_STATUS_PENDING_ARBITRATION,
        ):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} External verification only allowed while "
                f"pending (status={c.status})"
            )
        if not url or not (url.startswith("https://") or url.startswith("http://")):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} url must be http(s)")

        pool = self._get_pool_or_raise(c.pool_id)
        pool_name = pool.name
        unit_label = pool.unit_label
        obligation_text = c.obligation_text

        def leader_fn() -> dict:
            response = gl.nondet.web.get(url)
            status = getattr(response, "status_code", None)
            if status is None:
                status = getattr(response, "status", None)
            if status is not None and 400 <= int(status) < 500:
                raise gl.vm.UserError(f"{ERROR_EXTERNAL} Page returned {status}")
            if status is not None and int(status) >= 500:
                raise gl.vm.UserError(f"{ERROR_TRANSIENT} Page unavailable ({status})")

            body = response.body.decode("utf-8", errors="ignore")
            snippet = body[:6000]

            prompt = (
                "Does the following page content corroborate that a service "
                f"category named \"{pool_name}\" (unit: \"{unit_label}\") is "
                f"real and matches this obligation: \"{obligation_text}\"? "
                "Only use what is written on the page; do not use outside "
                "knowledge. Answer as JSON: {\"corroborates\": true/false, "
                "\"confidence\": \"HIGH\"|\"MEDIUM\"|\"LOW\"}\n\n"
                f"PAGE CONTENT:\n{snippet}"
            )
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            if isinstance(raw, str):
                parsed = self._parse_json_block(raw)
            elif isinstance(raw, dict):
                parsed = raw
            else:
                raise gl.vm.UserError(
                    f"{ERROR_LLM} Unexpected LLM response type: {type(raw)}"
                )

            return {
                "corroborates": self._coerce_bool(parsed.get("corroborates")),
                "confidence": self._coerce_confidence(parsed.get("confidence")),
            }

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return self._handle_leader_error(leaders_res, leader_fn)
            leader_out = leaders_res.calldata
            if not isinstance(leader_out, dict) or "corroborates" not in leader_out:
                return False
            validator_out = leader_fn()
            return bool(leader_out.get("corroborates")) == bool(
                validator_out.get("corroborates")
            )

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        corroborates = bool(result["corroborates"])

        c.external_evidence_note = (
            "corroborates" if corroborates else "does_not_corroborate"
        )
        c.external_evidence_confidence = result["confidence"]
        self.commitments[commitment_id] = c
        return corroborates

    # ------------------------------------------------------------------
    # VIEWS
    # ------------------------------------------------------------------

    @gl.public.view
    def get_pool(self, pool_id: str) -> dict:
        pool = self._get_pool_or_raise(pool_id)
        return {
            "pool_id": pool.pool_id,
            "owner": str(pool.owner),
            "name": pool.name,
            "unit_label": pool.unit_label,
            "total_units": int(pool.total_units),
            "reserved_units": int(pool.reserved_units),
            "available_units": int(pool.total_units) - int(pool.reserved_units),
            "status": pool.status,
            "registration_stake_wei": str(pool.registration_stake_wei),
            "bond_wei_per_commitment": str(pool.bond_wei_per_commitment),
            "active_commitment_count": int(pool.active_commitment_count),
            "visual_attestation_count": int(pool.visual_attestation_count),
            "visual_attestation_plausible_count": int(
                pool.visual_attestation_plausible_count
            ),
            "created_epoch": int(pool.created_epoch),
            "description": pool.description,
        }

    @gl.public.view
    def get_commitment(self, commitment_id: str) -> dict:
        c = self._get_commitment_or_raise(commitment_id)
        return {
            "commitment_id": c.commitment_id,
            "pool_id": c.pool_id,
            "submitter": str(c.submitter),
            "obligation_text": c.obligation_text,
            "requested_units": int(c.requested_units),
            "window_start_epoch": int(c.window_start_epoch),
            "window_end_epoch": int(c.window_end_epoch),
            "status": c.status,
            "verdict": c.verdict,
            "confidence": c.confidence,
            "matched_pool_confirmed": c.matched_pool_confirmed,
            "bond_wei": str(c.bond_wei),
            "bond_deposited": str(c.bond_deposited),
            "submitted_epoch": int(c.submitted_epoch),
            "arbitration_deadline_epoch": int(c.arbitration_deadline_epoch),
            "classification_notes": c.classification_notes,
            "external_evidence_note": c.external_evidence_note,
            "external_evidence_confidence": c.external_evidence_confidence,
        }

    @gl.public.view
    def list_pools(self) -> DynArray[str]:
        return self.pool_order

    @gl.public.view
    def list_commitments_for_pool(self, pool_id: str) -> DynArray[str]:
        return [cid for cid in self.commitment_order if self.commitments[cid].pool_id == pool_id]

    @gl.public.view
    def list_all_commitments(self) -> DynArray[str]:
        return self.commitment_order

    @gl.public.view
    def preview_admission(
        self,
        pool_id: str,
        requested_units: str,
        window_start_epoch: str,
        window_end_epoch: str,
    ) -> dict:
        """Read-only, fully deterministic dry run: would this numeric
        request + window fit given currently admitted CONSUMES
        commitments? Does not perform semantic classification -- this is
        purely the arithmetic half, exposed for UIs/agents to pre-check
        before paying a bond."""
        pool = self._get_pool_or_raise(pool_id)
        try:
            requested_units_u = u256(int(requested_units))
            window_start_u = u256(int(window_start_epoch))
            window_end_u = u256(int(window_end_epoch))
        except (ValueError, TypeError):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} requested_units/window_start_epoch/"
                f"window_end_epoch must be non-negative integers"
            )

        already_reserved = self._reserved_units_overlapping(
            pool_id, window_start_u, window_end_u
        )
        prospective_total = already_reserved + requested_units_u
        fits = prospective_total <= pool.total_units
        return {
            "pool_id": pool_id,
            "total_units": int(pool.total_units),
            "already_reserved_in_window": int(already_reserved),
            "requested_units": int(requested_units_u),
            "prospective_total": int(prospective_total),
            "would_fit": fits,
        }

    @gl.public.view
    def get_stats(self) -> dict:
        keys = [
            "total_pools",
            "total_commitments",
            "total_admitted",
            "total_rejected_overcommit",
            "total_rejected_bad_faith",
            "total_rejected_by_owner",
            "total_cancelled",
            "total_timeout_refunds",
            "total_ambiguous",
            "total_bond_wei_escrowed",
            "total_bond_wei_refunded",
            "total_bond_wei_forfeited",
            "total_stake_wei_escrowed",
            "total_visual_attestations",
        ]
        return {k: int(self.stats.get(k, u256(0))) for k in keys}

    @gl.public.view
    def get_visual_attestation(self, attestation_id: str) -> dict:
        if attestation_id not in self.visual_attestations:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Unknown visual attestation id: {attestation_id}"
            )
        a = self.visual_attestations[attestation_id]
        return {
            "pool_id": a.pool_id,
            "submitter": str(a.submitter),
            "plausible": a.plausible,
            "notes": a.notes,
            "submitted_epoch": int(a.submitted_epoch),
        }

    @gl.public.view
    def get_epoch(self) -> int:
        return int(self.epoch_counter)
