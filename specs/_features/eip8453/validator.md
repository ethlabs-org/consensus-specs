# EIP-8453 -- Honest Validator

*Note*: This document is a work-in-progress for researchers and implementers.

<!-- mdformat-toc start --slug=github --no-anchors --maxlevel=6 --minlevel=2 -->

- [Introduction](#introduction)
- [Prerequisites](#prerequisites)
- [Helpers](#helpers)
  - [New `FinalitySigningRecord`](#new-finalitysigningrecord)
- [Validator assignments](#validator-assignments)
  - [Attestation committees](#attestation-committees)
  - [Head vote committees](#head-vote-committees)
  - [Lookahead](#lookahead)
- [Beacon chain responsibilities](#beacon-chain-responsibilities)
  - [Block and sidecar proposal](#block-and-sidecar-proposal)
    - [Preparing for a `BeaconBlock`](#preparing-for-a-beaconblock)
    - [Constructing the `BeaconBlockBody`](#constructing-the-beaconblockbody)
      - [Head votes](#head-votes)
      - [Attestations](#attestations)
      - [Attester slashings](#attester-slashings)
  - [Head voting](#head-voting)
    - [Head vote data](#head-vote-data)
    - [Construct head vote](#construct-head-vote)
    - [Broadcast head vote](#broadcast-head-vote)
  - [Attesting](#attesting)
    - [Attestation data](#attestation-data)
      - [Finality pair](#finality-pair)
      - [Height pair](#height-pair)
      - [Construct data](#construct-data)
      - [Record attestation](#record-attestation)
    - [Construct attestation](#construct-attestation)
    - [Broadcast attestation](#broadcast-attestation)
  - [Attestation aggregation](#attestation-aggregation)
    - [Aggregation selection](#aggregation-selection)
    - [Construct aggregate](#construct-aggregate)
      - [Head votes](#head-votes-1)
      - [FG attestations](#fg-attestations)
      - [Aggregation bits](#aggregation-bits)
    - [Broadcast aggregate](#broadcast-aggregate)

<!-- mdformat-toc end -->

## Introduction

This document represents the changes to be made in the code of an "honest
validator" to implement EIP-8453.

EIP-8453 replaces the combined LMD-GHOST and Casper FFG attestation with
separate head votes for the available chain (AC) and attestations for the
finality gadget (FG). Every validator sits on the attestation committee given by
its index, and an active validator casts its FG attestation in that committee's
slot in every round. In every slot, a head vote committee casts head votes.

## Prerequisites

This document is an extension of the
[Heze -- Honest Validator](../../heze/validator.md) guide. All behaviors and
definitions in that document, and documents it extends, carry over unless
explicitly noted or overridden.

All terminology, constants, functions, and protocol mechanics defined in
[EIP-8453 -- The Beacon Chain](./beacon-chain.md) are requisite for this
document and used throughout.

*TODO*: Specify the EIP-8453 fork-choice interfaces and duty deadlines
referenced below.

## Helpers

### New `FinalitySigningRecord`

The `FinalitySigningRecord` tracks a validator's FG signing history. A separate
record is maintained for each validator key, independently of whether its
attestations are included on chain.

```python
@dataclass
class FinalitySigningRecord:
    targets: dict[Height, Root]
    timeouts: dict[Height, set[Root]]
    finality_locks: dict[Height, Root]
    entries: dict[Height, set[Root]]
```

The fields are initialized empty for a validator with no prior EIP-8453 signing
history:

- `targets[h]`: the first target voted for at height `h`.
- `timeouts[h]`: the targets named by timeouts signed at height `h`.
- `finality_locks[h]`: the block named by a finality vote at height `h`.
- `entries[h]`: entries named by any height vote at height `h`.

*Note*: The record persists across restarts. Finality signing locks do not
expire and are distinct from the temporary AC locks maintained by fork choice.

## Validator assignments

### Attestation committees

To determine its attestation committee for `epoch`, a validator calls
`get_attestation_committee_assignment(state, epoch, validator_index)`.

```python
def get_attestation_committee_assignment(
    state: BeaconState, epoch: Epoch, validator_index: ValidatorIndex
) -> CommitteeIndex | None:
    """
    Return the index of the validator's attestation committee in ``epoch``, if
    it is active in ``epoch``.
    """
    # [Modified in EIP8453]
    # The committee index is fixed by the validator index
    if not is_active_validator(state.validators[validator_index], epoch):
        return None
    return CommitteeIndex(validator_index % ATTESTATION_COMMITTEES_PER_ROUND)
```

A validator's committee index is fixed by its validator index, and so is its
subnet. An active validator attests in each round, in the slot
`compute_attestation_committee_slot(round, committee_index)`, which moves by one
slot in every round. Round numbering is defined by `compute_round_at_slot` and
does not restart at the EIP-8453 fork.

*Note*: The committees of an epoch also contain validators that are eligible for
activation but not yet active, and validators that exit after that epoch. A
validator that is not active has no attestation duty.

### Head vote committees

To determine the slots in which it casts head votes, a validator calls
`get_head_vote_committee_assignments(state, epoch, validator_index)`.

```python
def get_head_vote_committee_assignments(
    state: BeaconState, epoch: Epoch, validator_index: ValidatorIndex
) -> Sequence[Slot]:
    """
    Return the slots of ``epoch`` in which the validator sits on the head vote
    committee.
    """
    assert epoch <= get_current_epoch(state) + 1

    slots: list[Slot] = []
    start_slot = compute_start_slot_at_epoch(epoch)
    for slot in range(start_slot, start_slot + SLOTS_PER_EPOCH):
        if validator_index in get_head_vote_committee(state, Slot(slot)):
            slots.append(Slot(slot))
    return slots
```

A validator can sit on the head vote committee in several slots of an epoch, or
in none. It casts one head vote per assigned slot, even if it holds several
seats in that slot's committee.

### Lookahead

Head vote assignments are computed from the chain state using
`get_head_vote_committee`. A validator should recompute them when the dependent
chain state changes. A validator's attestation committee index is fixed by its
validator index, and the chain state determines whether it is active.

*TODO*: Specify assignment lookahead and the dependent root in the EIP-8453
fork-choice and networking specifications.

## Beacon chain responsibilities

All validator responsibilities remain unchanged other than those noted below.

### Block and sidecar proposal

A validator is expected to propose a `SignedBeaconBlock` at the beginning of any
slot during which `is_proposer(state, validator_index)` returns `True`.

#### Preparing for a `BeaconBlock`

Let `parent` be the block returned by the AC proposal procedure, together with
its previous slot head-vote view.

*TODO*: Specify the EIP-8453 proposal helper, including full or empty execution
payload selection and inclusion-list checks.

#### Constructing the `BeaconBlockBody`

*Note*: In this section, `state` is the parent state processed through any empty
slots up to the proposal slot using `process_slots`, before applying the block.

##### Head votes

Set `block.body.head_votes` to aggregates from the AC proposal view, up to
`MAX_HEAD_VOTES`. Each aggregate must satisfy the verification conditions in
[`process_head_vote`](./beacon-chain.md#new-process_head_vote). Votes of
`block.slot - 1` come first. The remaining space may hold votes of earlier slots
in the previous and current epochs that add seats for the block of their slot,
since these weigh builder payments.

Set `block.body.head_vote_support` to one bit per included aggregate, in the
same order. Set an aggregate's bit if the proposer counted its votes as support
in its previous slot head-vote view. A counted vote must be of slot
`block.slot - 1`, for a block in the accepted tree at proposal time whose slot
is no later than the vote's slot. Otherwise, clear the bit.

*Note*: The voters of an aggregate share its data, so the proposer counts all of
their votes as support or none of them. A vote for an unknown block may be
included with its support bit cleared.

*TODO*: Specify aggregate selection when the proposal view exceeds
`MAX_HEAD_VOTES`, preserving the requirements of view merge.

##### Attestations

Set `block.body.attestations` to aggregates selected from the FG attestation
pool, up to `MAX_ATTESTATIONS_ELECTRA`. Each aggregate must satisfy the
verification conditions in
[`process_attestation`](./beacon-chain.md#modified-process_attestation),
including its committee encoding and signature. Only attestations for the
current target of `state` are valid.

A proposer should prefer aggregates that add target, progress or
pending-finality participation.

##### Attester slashings

Set `block.body.attester_slashings` to valid EIP-8453 attester slashing evidence
and `block.body.legacy_attester_slashings` to valid pre-EIP-8453 evidence. Each
list may contain up to `MAX_ATTESTER_SLASHINGS_ELECTRA` items.

The evidence must satisfy `process_attester_slashing` or
`process_legacy_attester_slashing`, respectively, against the state at its
position in block processing.

### Head voting

A validator on the head vote committee of `slot` creates, signs and broadcasts a
`HeadVoteMessage` for that slot.

Every node attempts `get_ac_vote_head(store)` after block, payload and head-vote
receipts and at `get_head_vote_due_ms()` into the slot, even if none of its
validators belongs to the head vote committee. A ready current-slot proposal
allows early preparation; otherwise preparation waits until the deadline.
Preparation caches one head per slot and unlocks using the frozen local view
merged with current proposals. When a new prepared head becomes available,
assigned validators immediately sign, process locally and broadcast their vote
once. Unselected validators do not sign, self-admit or emit a vote. Repeated
requests in the same slot return the prepared head without unlocking again. A
`None` result means no vote is permitted. Nodes must observe continuously from
the start of the previous slot after startup or restart before proposing,
preparing votes, confirming or publishing an FG source. Validator signing
records survive restart, including the record of head votes already signed; a
validator must never sign two different head votes for one slot. A missing
`get_ac_proposal` result means the proposal duty is skipped.

#### Head vote data

First, the validator constructs `head_vote_data`, a
[`HeadVoteData`](./beacon-chain.md#new-headvotedata) object:

- Let `head` be the head selected by the AC voting procedure for `slot`.
- Set `head_vote_data.slot = slot`.
- Set `head_vote_data.beacon_block_root = head.root`.
- Set `head_vote_data.payload_status = head.payload_status`. The prepared node
  uses `PAYLOAD_STATUS_PENDING` for a current-slot block, and
  `PAYLOAD_STATUS_FULL` or `PAYLOAD_STATUS_EMPTY` for an earlier block.

The AC voting procedure applies view merge, AC locks and FG restrictions using
its voting view.

#### Construct head vote

The validator creates `head_vote_message` as a
[`HeadVoteMessage`](./beacon-chain.md#new-headvotemessage):

- Set `head_vote_message.validator_index = validator_index`.
- Set `head_vote_message.data = head_vote_data`.
- Set `head_vote_message.signature` to
  `get_head_vote_signature(state, head_vote_data, privkey)`.

```python
def get_head_vote_signature(state: BeaconState, data: HeadVoteData, privkey: int) -> BLSSignature:
    domain = get_domain(state, DOMAIN_HEAD_VOTE, compute_epoch_at_slot(data.slot))
    signing_root = compute_signing_root(data, domain)
    return bls.Sign(privkey, signing_root)
```

The validator must record the selected root before signing and must not sign
another head for the same slot.

#### Broadcast head vote

The validator broadcasts `head_vote_message` on the global `head_vote`
[topic](./p2p-interface.md#new-head_vote) and processes it locally.

### Attesting

A validator attempts to create an FG attestation in the slot of its attestation
committee using the rules below. It sends no new attestation if there is no FG
source or if its signing history suppresses the vote.

*TODO*: Specify the FG attestation deadline and source observation for each
assigned slot and staggered committee schedule.

#### Attestation data

First, obtain `source` and `head` from the same AC-to-FG observation. The source
is the deepest block in the viable tree passing the fast test in the applicable
confirmation update. If no such block exists, the validator does not attest.

Let `source_state` and `head_state` be the states after processing `source` and
`head`, respectively:

- Let `target` be a copy of `source_state.target_checkpoint`.
- Let `justified = head_state.justified_checkpoint`.
- Let `finalized_height = head_state.finalized_checkpoint.height`.

If `target.root == Root()` because `source` advanced the height, set
`target.root = hash_tree_root(source)`.

*Note*: The height vote comes from the source state. The finality pair comes
from the head state.

##### Finality pair

The validator selects the finality pair using `get_finality_vote`:

```python
def get_finality_vote(
    record: FinalitySigningRecord,
    justified: HeightCheckpoint,
    finalized_height: Height,
) -> HeightCheckpoint:
    height, root = justified.height, justified.root
    if height <= finalized_height:
        return HeightCheckpoint.empty()
    if record.targets.get(height) not in (None, root):
        return HeightCheckpoint.empty()
    if not record.timeouts.get(height, set()) <= {root}:
        return HeightCheckpoint.empty()
    if record.finality_locks.get(height) not in (None, root):
        return HeightCheckpoint.empty()
    return justified.copy()
```

##### Height pair

The validator selects the height pair using `get_height_vote`:

```python
def get_height_vote(
    record: FinalitySigningRecord,
    target: HeightCheckpoint,
    finality: HeightCheckpoint,
) -> tuple[HeightCheckpoint, bool] | None:
    height, root = target.height, target.root
    if height == 0 or root == Root():
        return None

    lock = record.finality_locks.get(height)
    if finality != HeightCheckpoint.empty() and finality.height == height:
        lock = finality.root

    if lock is not None and lock != root:
        return None
    if height in record.timeouts:
        return target.copy(), True
    if record.targets.get(height) in (None, root):
        return target.copy(), False
    return target.copy(), True
```

*Note*: The candidate finality pair constrains the height vote before it is
removed, if necessary, by `get_finality_attestation_data` below.

##### Construct data

Set `attestation_data` to
`get_finality_attestation_data(record, target, justified, finalized_height)`. If
it returns `None`, the validator sends no new attestation.

*Note*: The duplicate-vote checks suppress the entire attestation, including any
new finality pair.

```python
def get_finality_attestation_data(
    record: FinalitySigningRecord,
    target: HeightCheckpoint,
    justified: HeightCheckpoint,
    finalized_height: Height,
) -> AttestationData | None:
    finality = get_finality_vote(record, justified, finalized_height)
    vote = get_height_vote(record, target, finality)
    if vote is None:
        return None
    target, timeout = vote

    entries = record.entries.get(target.height, set())
    if (not timeout and len(entries) > 0) or target.root in entries:
        return None

    if finality != HeightCheckpoint.empty() and target.height <= finality.height:
        finality = HeightCheckpoint.empty()

    return AttestationData(target=target, timeout=Boolean(timeout), finality=finality)
```

##### Record attestation

Before signing, the validator records the selected `attestation_data`:

```python
def record_finality_attestation(record: FinalitySigningRecord, data: AttestationData) -> None:
    height, root = data.target.height, data.target.root
    record.entries.setdefault(height, set()).add(root)
    if data.finality != HeightCheckpoint.empty():
        record.finality_locks[data.finality.height] = data.finality.root
    if data.timeout:
        record.timeouts.setdefault(height, set()).add(root)
    else:
        record.targets.setdefault(height, root)
```

Selecting and recording the data must be serialized per validator key. The
record and selected data must be persisted before signing. See
[Protection best practices](#protection-best-practices) for interrupted duties.

#### Construct attestation

The validator creates `attestation` as a
[`SingleAttestation`](./beacon-chain.md#modified-singleattestation) with the
following fields:

- Set `attestation.committee_index` to the assigned committee index.
- Set `attestation.attester_index = validator_index`.
- Set `attestation.data = attestation_data`.
- Set `attestation.signature` to
  `get_attestation_signature(source_state, attestation_data, epoch, privkey)`,
  where `epoch = get_attestation_epoch(source_state)`, the epoch of the target.

```python
def get_attestation_signature(
    state: BeaconState,
    attestation_data: AttestationData,
    # [New in EIP8453]
    epoch: Epoch,
    privkey: int,
) -> BLSSignature:
    # [Modified in EIP8453]
    domain = get_domain(state, DOMAIN_FINALITY_ATTESTER, epoch)
    signing_root = compute_signing_root(attestation_data, domain)
    return bls.Sign(privkey, signing_root)
```

The signature covers `attestation.data`, but not `attestation.committee_index`.

*Note*: `source_state.target_slot` is the slot of the target block, so every
validator signing for the same target uses the same epoch. The target is current
in the state of every chain that can include the attestation, and that state has
the same target slot.

#### Broadcast attestation

The validator broadcasts `attestation` on the `attestation_{subnet_id}`
[topic](./p2p-interface.md#new-attestation_subnet_id) and processes it locally,
where `subnet_id = compute_subnet_for_attestation(attestation.committee_index)`.

```python
def compute_subnet_for_attestation(
    # [Modified in EIP8453]
    # Removed `committees_per_slot`
    # [Modified in EIP8453]
    # Removed `slot`
    committee_index: CommitteeIndex,
) -> SubnetID:
    """
    Return the subnet of the attestation committee at ``committee_index``.
    """
    # [Modified in EIP8453]
    return SubnetID(committee_index % ATTESTATION_SUBNET_COUNT)
```

### Attestation aggregation

Validators aggregate head votes and FG attestations separately. The messages
passed to the helpers below must have valid signatures under the same domain and
valid committee assignments for `state`.

*Note*: The helpers aggregate individual messages. Aggregates with overlapping
signers cannot be combined by adding their signatures.

#### Aggregation selection

*TODO*: Specify aggregator selection for head votes and FG attestations. The
legacy aggregation helpers require adaptation to EIP-8453 committees.

#### Construct aggregate

##### Head votes

Construct `aggregate_vote` using `get_aggregate_head_vote` with head vote
messages for the same slot and block root. A validator holding several seats in
the committee is listed and signs once, and its vote counts for each of its
seats.

```python
def get_aggregate_head_vote(state: BeaconState, messages: Sequence[HeadVoteMessage]) -> HeadVote:
    """
    Aggregate head vote messages with identical data and distinct signers.
    """
    assert len(messages) > 0
    data = messages[0].data
    assert all(message.data == data for message in messages)
    signatures = {message.validator_index: message.signature for message in messages}
    assert len(signatures) == len(messages)

    for validator_index in signatures:
        assert validator_index in get_head_vote_committee(state, data.slot)

    voter_indices = sorted(signatures)
    return HeadVote(
        voter_indices=HeadVoteCommitteeIndices(data=voter_indices),
        data=data,
        signature=bls.Aggregate([signatures[index] for index in voter_indices]),
    )
```

##### FG attestations

Construct `aggregate_attestation` using `get_aggregate_attestation` with
individual attestations for the same data, where `state` is a state whose target
is the target of that data, such as the aggregator's source state. Attestations
cast in different rounds or epochs can be aggregated together.

```python
def get_aggregate_attestation(
    state: BeaconState, attestations: Sequence[SingleAttestation]
) -> Attestation:
    """
    Aggregate attestations with identical data and distinct signers.
    """
    assert len(attestations) > 0
    data = attestations[0].data
    assert all(a.data == data for a in attestations)
    participants = {a.attester_index for a in attestations}
    assert len(participants) == len(attestations)

    # [Modified in EIP8453]
    # Each signer's committee is fixed by its index
    committee_bits = AttestationCommitteeBits()
    for validator_index in participants:
        committee_bits[validator_index % ATTESTATION_COMMITTEES_PER_ROUND] = Boolean(True)

    epoch = get_attestation_epoch(state)
    aggregation_bits = get_attestation_aggregation_bits(state, epoch, committee_bits, participants)
    return Attestation(
        aggregation_bits=aggregation_bits,
        data=data,
        signature=bls.Aggregate([a.signature for a in attestations]),
        committee_bits=committee_bits,
    )
```

##### Aggregation bits

`get_attestation_aggregation_bits` encodes signers in committee-index order,
matching `get_aggregate_participants` in the beacon-chain specification. The
size bound applies to all members of the represented committees, not only the
signers. Split messages into separate aggregates when the bound would be
exceeded.

```python
def get_attestation_aggregation_bits(
    state: BeaconState,
    epoch: Epoch,
    committee_bits: AttestationCommitteeBits,
    participants: set[ValidatorIndex],
) -> AggregationBits:
    """
    Encode participants in the concatenation of the selected attestation
    committees.
    """
    aggregation_bits = AggregationBits()
    represented_participants: set[ValidatorIndex] = set()
    for committee_index in get_committee_indices(committee_bits):
        committee = get_attestation_committee(state, epoch, committee_index)
        assert len(aggregation_bits) + len(committee) <= MAX_VALIDATORS_PER_AGGREGATE
        for validator_index in committee:
            is_participant = validator_index in participants
            aggregation_bits.append(Boolean(is_participant))
            if is_participant:
                represented_participants.add(validator_index)
    assert represented_participants == participants
    return aggregation_bits
```

#### Broadcast aggregate

The validator broadcasts the aggregate for block inclusion.

*TODO*: Specify aggregate gossip messages, selection-proof wrappers and
broadcast deadlines.

*Note*: A block can include an FG aggregate only while its target is the current
target of the inclusion state. Its encoding stays valid for that whole time,
since the committees depend only on the epoch of the target.
