# PQ -- Fork Choice Local Store

*Note*: This document is a work-in-progress for researchers and implementers.

<!-- mdformat-toc start --slug=github --no-anchors --maxlevel=6 --minlevel=2 -->

- [Introduction](#introduction)
- [Configs](#configs)
  - [Time parameters](#time-parameters)
- [Helpers](#helpers)
  - [Modified `Store`](#modified-store)
  - [Modified `get_forkchoice_store`](#modified-get_forkchoice_store)
  - [New `upgrade_store_to_pq`](#new-upgrade_store_to_pq)
  - [New `is_ancestor_root`](#new-is_ancestor_root)
  - [New `get_deepest_node`](#new-get_deepest_node)
  - [New `is_compatible`](#new-is_compatible)
  - [New `get_subtree`](#new-get_subtree)
  - [Modified `get_parent_payload_status`](#modified-get_parent_payload_status)
  - [Modified `is_parent_node_full`](#modified-is_parent_node_full)
  - [New `get_upgraded_state`](#new-get_upgraded_state)
  - [New `get_block_state`](#new-get_block_state)
  - [New `get_block_pre_state`](#new-get_block_pre_state)
  - [Store mutators](#store-mutators)
    - [New `record_block_receipt_time`](#new-record_block_receipt_time)
    - [New `record_payload_receipt_time`](#new-record_payload_receipt_time)
- [Handlers](#handlers)
  - [Modified `on_tick`](#modified-on_tick)
  - [Modified `on_block`](#modified-on_block)
  - [Modified `on_execution_payload_envelope`](#modified-on_execution_payload_envelope)
  - [Modified `notify_ptc_messages`](#modified-notify_ptc_messages)
  - [Modified `on_payload_attestation_message`](#modified-on_payload_attestation_message)
  - [New `on_head_vote_message`](#new-on_head_vote_message)

<!-- mdformat-toc end -->

## Introduction

The fork choice of Gloas is replaced by that of EIP-8453. The store follows the
finality gadget (FG) through the beacon state and runs the available chain (AC)
on head votes.

Blocks no longer carry payload attestations, so the votes of the payload
timeliness committee reach fork choice only from the wire. Gossip validation
checks the XMSS signature of a head vote message from the wire. Head vote
aggregates in a block carry no signature, and `process_stark` proves their
signatures; they count only for builder payments. Head vote declarations carry
no signature and are used only if the store holds the signed vote.

Joining and restart handling are left to clients, outside these handlers.

At the fork, a node converts its EIP-8483 store with `upgrade_store_to_pq`. The
store keeps the blocks and post-states it holds, and the post-states of blocks
before the fork stay EIP-8483 states. PQ fork choice reads every stored state
through `get_block_state`, which upgrades an EIP-8483 state with
`get_upgraded_state`. `on_block` applies the first block after the fork to the
upgraded parent state, and imports a block from before the fork that arrives
late with the EIP-8483 state transition.

- [Available chain](./available-chain.md): head voting, confirmation and locks.
- [Finality gadget](./finality-gadget.md): fork-choice viability and FG source
  selection.

## Configs

### Time parameters

| Name                 | Value          | Duration                  |
| -------------------- | -------------- | ------------------------- |
| `HEAD_VOTE_DUE_BPS`  | `Uint64(2500)` | 25% of `SLOT_DURATION_MS` |
| `SUPPORT_CUTOFF_BPS` | `Uint64(5000)` | 50% of `SLOT_DURATION_MS` |
| `VIEW_FREEZE_BPS`    | `Uint64(7500)` | 75% of `SLOT_DURATION_MS` |

## Helpers

### Modified `Store`

```python
@dataclass
class Store:
    time_ms: Uint64
    genesis_time_ms: Uint64
    # [Modified in PQ:EIP8453]
    justified_checkpoint: HeightCheckpoint
    finalized_checkpoint: HeightCheckpoint
    # [Modified in PQ:EIP8453]
    # Removed `unrealized_justified_checkpoint`
    # [Modified in PQ:EIP8453]
    # Removed `unrealized_finalized_checkpoint`
    proposer_boost_root: Root
    # [Modified in PQ:EIP8453]
    # Removed `equivocating_indices`
    # [Modified in PQ:EIP8453]
    # Blocks before the fork keep their EIP-8483 blocks and post-states
    blocks: dict[Root, BeaconBlock | eip8483.BeaconBlock]
    block_states: dict[Root, BeaconState | eip8483.BeaconState]
    # [Modified in PQ:EIP8453]
    # Removed `block_timeliness`
    # [Modified in PQ:EIP8453]
    # Removed `checkpoint_states`
    # [Modified in PQ:EIP8453]
    # Removed `latest_messages`
    # [Modified in PQ:EIP8453]
    # Removed `unrealized_justifications`
    # [Modified in PQ:EIP8453]
    # Payloads of blocks before the fork keep their EIP-8483 envelopes
    payloads: dict[Root, ExecutionPayloadEnvelope | eip8483.ExecutionPayloadEnvelope]
    payload_timeliness_vote: dict[Root, list[Boolean | None]]
    payload_data_availability_vote: dict[Root, list[Boolean | None]]
    # [New in PQ:EIP8453]
    max_height: Height
    block_receipt_times: dict[Root, Uint64]
    payload_receipt_times: dict[Root, Uint64]
    latest_confirmed: ForkChoiceNode
    saved_confirmations: dict[Slot, ForkChoiceNode]
    locks: set[ACLock]
    head_vote_messages: dict[Slot, list[HeadVoteMessage]]
    head_vote_message_receipt_times: dict[Root, Uint64]
    validated_head_votes: dict[Root, ACVote]
    ac_tally_root: Root | None = None
    prepared_head_vote: tuple[Slot, ForkChoiceNode] | None = None
    pending_head_votes: list[tuple[HeadVoteMessage, ACVote]] = field(default_factory=list)
```

### Modified `get_forkchoice_store`

*Note*: The caller trusts the anchor block as justified and finalized at its
post-state’s target height. The store's clock starts at the anchor state's slot.

```python
def get_forkchoice_store(anchor_state: BeaconState, anchor_block: BeaconBlock) -> Store:
    assert anchor_block.state_root == hash_tree_root(anchor_state)
    anchor_root = hash_tree_root(anchor_block)
    # [New in PQ:EIP8453]
    advanced_anchor_state = anchor_state.copy()
    process_slot(advanced_anchor_state)
    assert advanced_anchor_state.target_checkpoint.root == anchor_root
    # [Modified in PQ:EIP8453]
    anchor_height = anchor_state.target_checkpoint.height
    justified_checkpoint = HeightCheckpoint(height=anchor_height, root=anchor_root)
    finalized_checkpoint = HeightCheckpoint(height=anchor_height, root=anchor_root)
    genesis_time_ms = seconds_to_milliseconds(anchor_state.genesis_time)
    time_ms = compute_time_at_slot_ms(genesis_time_ms, anchor_state.slot)
    return Store(
        time_ms=time_ms,
        genesis_time_ms=genesis_time_ms,
        # [Modified in PQ:EIP8453]
        justified_checkpoint=justified_checkpoint,
        finalized_checkpoint=finalized_checkpoint,
        proposer_boost_root=Root(),
        blocks={anchor_root: anchor_block.copy()},
        block_states={anchor_root: anchor_state.copy()},
        payloads={},
        payload_timeliness_vote={anchor_root: [None] * PTC_SIZE},
        payload_data_availability_vote={anchor_root: [None] * PTC_SIZE},
        # [New in PQ:EIP8453]
        max_height=anchor_height,
        block_receipt_times={anchor_root: time_ms},
        payload_receipt_times={},
        latest_confirmed=ForkChoiceNode(
            root=finalized_checkpoint.root, payload_status=PAYLOAD_STATUS_PENDING
        ),
        saved_confirmations={},
        locks=set(),
        head_vote_messages={},
        head_vote_message_receipt_times={},
        validated_head_votes={},
        ac_tally_root=anchor_root,
    )
```

### New `upgrade_store_to_pq`

*Note*: The finality gadget starts at height 0 from the last finalized Casper
FFG checkpoint, as in `upgrade_to_pq`, so the store's justified and finalized
checkpoints are that block at height 0. Every block before the fork is the
target of height 1 on its own branch. Blocks before the fork count as received
at the start of their slot. The AC starts with no locks, confirmations or head
votes.

```python
def upgrade_store_to_pq(pre: eip8483.Store) -> Store:
    """
    Return the PQ fork choice store converted from the EIP-8483 store ``pre``
    at the fork.
    """
    finalized_checkpoint = HeightCheckpoint(height=Height(0), root=pre.finalized_checkpoint.root)
    return Store(
        time_ms=pre.time_ms,
        genesis_time_ms=pre.genesis_time_ms,
        justified_checkpoint=finalized_checkpoint,
        finalized_checkpoint=finalized_checkpoint,
        proposer_boost_root=Root(),
        blocks=dict(pre.blocks),
        block_states=dict(pre.block_states),
        payloads=dict(pre.payloads),
        payload_timeliness_vote=dict(pre.payload_timeliness_vote),
        payload_data_availability_vote=dict(pre.payload_data_availability_vote),
        # [Modified in PQ]
        # Removed `payload_inclusion_list_satisfaction`
        max_height=Height(1),
        block_receipt_times={
            root: compute_time_at_slot_ms(pre.genesis_time_ms, block.slot)
            for root, block in pre.blocks.items()
        },
        payload_receipt_times={
            root: compute_time_at_slot_ms(pre.genesis_time_ms, pre.blocks[root].slot)
            for root in pre.payloads
        },
        latest_confirmed=ForkChoiceNode(
            root=finalized_checkpoint.root, payload_status=PAYLOAD_STATUS_PENDING
        ),
        saved_confirmations={},
        locks=set(),
        head_vote_messages={},
        head_vote_message_receipt_times={},
        validated_head_votes={},
    )
```

### New `is_ancestor_root`

```python
def is_ancestor_root(store: Store, root: Root, ancestor: Root) -> bool:
    """
    Return ``True`` if ``ancestor`` is an ancestor of ``root``
    in the tree of beacon blocks ignoring payload status.
    """
    node = ForkChoiceNode(root=root, payload_status=PAYLOAD_STATUS_PENDING)
    ancestor_node = ForkChoiceNode(root=ancestor, payload_status=PAYLOAD_STATUS_PENDING)
    return is_ancestor(store, node, ancestor_node)
```

### New `get_deepest_node`

*Note*: This helper has no root tie-break. Use it to select a node only from a
chain or a compatible set. Callers comparing conflicting nodes must be
independent of which tied node is returned, as in `is_compatible`.

```python
def get_deepest_node(store: Store, nodes: set[ForkChoiceNode]) -> ForkChoiceNode:
    """
    Return the node in ``nodes`` with the highest slot and payload status.
    """
    assert len(nodes) > 0
    return max(
        nodes,
        key=lambda node: (
            store.blocks[node.root].slot,
            # Precedence of full/empty over pending
            0 if node.payload_status == PAYLOAD_STATUS_PENDING else 1,
        ),
    )
```

### New `is_compatible`

```python
def is_compatible(store: Store, node_1: ForkChoiceNode, node_2: ForkChoiceNode) -> bool:
    """
    Return ``True`` if ``node_1`` and ``node_2`` belong to the same chain.
    """
    if get_deepest_node(store, {node_1, node_2}) == node_1:
        return is_ancestor(store, node_1, node_2)
    else:
        return is_ancestor(store, node_2, node_1)
```

### New `get_subtree`

```python
def get_subtree(store: Store, subtree_root: ForkChoiceNode) -> Sequence[ForkChoiceNode]:
    """
    Return the nodes in a subtree rooted at ``subtree_root``, including ``subtree_root``.
    """
    subtree = [subtree_root]
    for child in get_node_children(store, subtree_root):
        subtree.extend(get_subtree(store, child))
    return subtree
```

### Modified `get_parent_payload_status`

```python
def get_parent_payload_status(
    store: Store,
    # [Modified in PQ:EIP8453]
    block: BeaconBlock | eip8483.BeaconBlock,
) -> PayloadStatus:
    parent = store.blocks[block.parent_root]
    parent_block_hash = block.body.signed_execution_payload_bid.message.parent_block_hash
    message_block_hash = parent.body.signed_execution_payload_bid.message.block_hash
    return PAYLOAD_STATUS_FULL if parent_block_hash == message_block_hash else PAYLOAD_STATUS_EMPTY
```

### Modified `is_parent_node_full`

```python
def is_parent_node_full(
    store: Store,
    # [Modified in PQ:EIP8453]
    block: BeaconBlock | eip8483.BeaconBlock,
) -> bool:
    return get_parent_payload_status(store, block) == PAYLOAD_STATUS_FULL
```

### New `get_upgraded_state`

*Note*: The upgrade replays the slots from the state's slot to the fork, up to
an epoch near the fork. Readers such as `is_fg_contest` upgrade every stored
block before the fork on each call, so implementations should cache the upgraded
state of each stored block.

```python
def get_upgraded_state(state: eip8483.BeaconState | BeaconState) -> BeaconState:
    """
    Return ``state``, advanced to the PQ fork and upgraded if it is an EIP-8483
    state.
    """
    if not isinstance(state, eip8483.BeaconState):
        return state
    pre = state.copy()
    fork_slot = compute_start_slot_at_epoch(Epoch(PQ_FORK_EPOCH))
    if pre.slot < fork_slot:
        eip8483.process_slots(pre, fork_slot)
    return upgrade_to_pq(pre)
```

### New `get_block_state`

*Note*: The returned state can be the stored state itself. Callers must not
mutate it; a caller that changes it works on a copy.

```python
def get_block_state(store: Store, root: Root) -> BeaconState:
    """
    Return the post-state of the block at ``root``, upgraded at the EIP-8453
    fork.
    """
    return get_upgraded_state(store.block_states[root])
```

### New `get_block_pre_state`

```python
def get_block_pre_state(store: Store, block: BeaconBlock) -> BeaconState:
    """
    Return the post-state of the parent of ``block``, upgraded at the EIP-8453
    fork and advanced to the slot of ``block``.
    """
    state = get_block_state(store, block.parent_root).copy()
    if state.slot < block.slot:
        process_slots(state, block.slot)
    return state
```

### Store mutators

#### New `record_block_receipt_time`

```python
def record_block_receipt_time(store: Store, root: Root) -> None:
    """
    Store block receipt time.
    """
    store.block_receipt_times[root] = store.time_ms
```

#### New `record_payload_receipt_time`

```python
def record_payload_receipt_time(store: Store, root: Root) -> None:
    """
    Store payload receipt time.
    """
    store.payload_receipt_times[root] = store.time_ms
```

## Handlers

### Modified `on_tick`

*Note*: Updates the store time, then prepares the head vote or runs confirmation
when scheduled at `time_ms`. Every node prepares the head vote, including nodes
without a validator on the committee. Callers must process this tick before
other events at the same timestamp. A delayed client timer dispatches crossed
duty timestamps only from its current wall-clock slot, in order, before
advancing to its wall time. It skips all duties in earlier slots, including when
catching up from an old anchor: it must not call this handler for those
historical duty timestamps. Within the current slot, the first physical tick at
or after a deadline dispatches that duty with its scheduled timestamp. Callers
must not dispatch the same scheduled timestamp more than once. Queued early
votes carry their authenticated data into the slot and are weighed at use.

```python
def on_tick(store: Store, time_ms: Uint64) -> None:
    # Lock expiry reads must use the time of this tick.
    store.time_ms = time_ms
    time_into_slot_ms = (time_ms - store.genesis_time_ms) % SLOT_DURATION_MS
    slot = compute_slot_at_time_ms(store.genesis_time_ms, time_ms)
    previous_slot = saturating_sub(slot, 1)
    store.head_vote_messages = {
        s: votes for s, votes in store.head_vote_messages.items() if s >= previous_slot
    }
    retained_roots = {
        hash_tree_root(vote) for votes in store.head_vote_messages.values() for vote in votes
    }
    store.head_vote_message_receipt_times = {
        root: time
        for root, time in store.head_vote_message_receipt_times.items()
        if root in retained_roots
    }
    store.validated_head_votes = {
        root: vote for root, vote in store.validated_head_votes.items() if root in retained_roots
    }
    store.saved_confirmations = {
        s: node
        for s, node in store.saved_confirmations.items()
        if s >= saturating_sub(slot, SLOW_LOCK_WINDOW)
    }

    pending = store.pending_head_votes
    store.pending_head_votes = []
    for message, vote in pending:
        if message.data.slot > slot:
            store.pending_head_votes.append((message, vote))
        elif message.data.slot >= previous_slot:
            record_head_vote_message(store, message, vote)
            get_ac_vote_head(store)

    if time_into_slot_ms == get_head_vote_due_ms():
        get_ac_vote_head(store)

    if slot > GENESIS_SLOT and time_into_slot_ms == get_support_cutoff_ms():
        update_confirmation(store, slot - 1)
```

### Modified `on_block`

*Note*: On importing a PQ block, update finality, record its receipt time and
re-evaluate head vote preparation. PQ blocks carry no payload attestations.

```python
def on_block(store: Store, signed_block: SignedBeaconBlock | eip8483.SignedBeaconBlock) -> None:
    """
    Run ``on_block`` upon receiving a new block.
    """
    block = signed_block.message
    block_root = hash_tree_root(block)

    # Return early if the block is already known
    if block_root in store.blocks:
        return

    # Parent block must be known
    assert block.parent_root in store.block_states

    # If this block builds on the parent's full payload, that payload must
    # have been verified by on_execution_payload_envelope
    if is_parent_node_full(store, block):
        assert is_payload_verified(store, block.parent_root)

    # Blocks cannot be in the future. If they are, their consideration must be delayed until they are in the past.
    current_slot = get_current_slot(store)
    assert current_slot >= block.slot

    # [Modified in PQ:EIP8453]
    # Run finalized check over the parent to avoid circular dependency.
    assert is_ancestor_root(store, block.parent_root, store.finalized_checkpoint.root)

    # [New in PQ:EIP8453]
    # A block from before the fork that arrives late is processed with the EIP-8483
    # state transition, and its EIP-8483 post-state is stored like those of the
    # blocks before the fork
    if compute_epoch_at_slot(block.slot) < PQ_FORK_EPOCH:
        assert isinstance(signed_block, eip8483.SignedBeaconBlock)
        pre_fork_state = store.block_states[block.parent_root].copy()
        assert isinstance(pre_fork_state, eip8483.BeaconState)
        eip8483.state_transition(pre_fork_state, signed_block, validate_result=True)
        store.blocks[block_root] = signed_block.message
        store.block_states[block_root] = pre_fork_state
        store.payload_timeliness_vote[block_root] = [None] * PTC_SIZE
        store.payload_data_availability_vote[block_root] = [None] * PTC_SIZE
        record_block_receipt_time(store, block_root)
        # Notify the store about the payload attestations in the block, read
        # with its EIP-8483 post-state
        notify_ptc_messages(store, pre_fork_state, signed_block.message.body.payload_attestations)
        return

    # A block at or after the fork is a PQ block
    assert isinstance(signed_block, SignedBeaconBlock)
    block = signed_block.message

    # [Modified in PQ:EIP8453]
    # Make a copy of the parent's state, upgraded at the PQ fork
    state = get_block_state(store, block.parent_root).copy()

    # Check the block is valid and compute the post-state
    state_transition(state, signed_block, validate_result=True)

    # Add new block to the store
    store.blocks[block_root] = block
    # Add new state for this block to the store
    store.block_states[block_root] = state
    tally_root = store.ac_tally_root
    if tally_root is None or (state.dynasty, block_root) > (
        get_block_state(store, tally_root).dynasty,
        tally_root,
    ):
        store.ac_tally_root = block_root
    # Add a new PTC voting for this block to the store
    store.payload_timeliness_vote[block_root] = [None] * PTC_SIZE
    store.payload_data_availability_vote[block_root] = [None] * PTC_SIZE

    # [Modified in PQ:EIP8453]
    update_finality(store, state)

    # [New in PQ:EIP8453]
    # Record block receipt time
    record_block_receipt_time(store, block_root)

    # [Modified in PQ]
    # Removed `notify_ptc_messages`
    get_ac_vote_head(store)
```

### Modified `on_execution_payload_envelope`

*Note*: This function is modified to record payload receipt time.

```python
def on_execution_payload_envelope(
    store: Store,
    # [Modified in PQ:EIP8453]
    signed_envelope: SignedExecutionPayloadEnvelope | eip8483.SignedExecutionPayloadEnvelope,
) -> None:
    """
    Run ``on_execution_payload_envelope`` upon receiving a new execution payload envelope.
    """
    envelope = signed_envelope.message
    # The corresponding beacon block root needs to be known
    assert envelope.beacon_block_root in store.block_states

    # Check if blob data is available
    # If not, this payload MAY be queued and subsequently considered when blob data becomes available
    assert is_data_available(envelope.beacon_block_root)

    state = store.block_states[envelope.beacon_block_root]

    # Verify the execution payload envelope
    # [Modified in PQ:EIP8453]
    # The payload of a block before the fork is verified with the EIP-8483 rules
    if isinstance(state, eip8483.BeaconState):
        assert isinstance(signed_envelope, eip8483.SignedExecutionPayloadEnvelope)
        eip8483.verify_execution_payload_envelope(state, signed_envelope, eip8483.EXECUTION_ENGINE)
    else:
        assert isinstance(signed_envelope, SignedExecutionPayloadEnvelope)
        verify_execution_payload_envelope(state, signed_envelope, EXECUTION_ENGINE)

    # [New in PQ:EIP8453]
    # Record payload receipt time
    record_payload_receipt_time(store, envelope.beacon_block_root)

    # Add execution payload envelope to the store
    store.payloads[envelope.beacon_block_root] = envelope
    get_ac_vote_head(store)
```

### Modified `notify_ptc_messages`

*Note*: PQ blocks carry no payload attestations. Only a block before the fork
that arrives after the store conversion passes its PTC votes to the store, read
with its EIP-8483 post-state.

```python
def notify_ptc_messages(
    store: Store,
    # [Modified in PQ:EIP8453]
    state: eip8483.BeaconState,
    payload_attestations: Sequence[eip8483.PayloadAttestation],
) -> None:
    """
    Extracts a list of ``PayloadAttestationMessage`` from ``payload_attestations`` and updates the store with them
    These Payload attestations are assumed to be in the beacon block hence signature verification is not needed
    """
    if state.slot == 0:
        return
    for payload_attestation in payload_attestations:
        # [Modified in PQ:EIP8453]
        # A block before the fork has the EIP-8483 PTC
        indexed_payload_attestation = eip8483.get_indexed_payload_attestation(
            state, payload_attestation
        )
        for idx in indexed_payload_attestation.attesting_indices:
            on_payload_attestation_message(
                store,
                PayloadAttestationMessage(
                    validator_index=idx,
                    data=payload_attestation.data,
                    # [Modified in PQ]
                    signature=XMSSSignature(),
                ),
                is_from_block=True,
            )
```

### Modified `on_payload_attestation_message`

*Note*: PQ blocks carry no payload attestations, so a message from a block is a
PTC vote of a block before the fork, passed on by `notify_ptc_messages`. A
message from the wire is for the current slot, so after the store conversion it
is for a PQ block, and its signature is checked with `xmss_verify`, since
`IndexedPayloadAttestation` is deprecated.

```python
def on_payload_attestation_message(
    store: Store,
    ptc_message: PayloadAttestationMessage,
    # [Modified in PQ:EIP8453]
    is_from_block: bool = False,
) -> None:
    """
    Run ``on_payload_attestation_message`` upon receiving a new payload attestation message
    from either within a block before the fork or directly on the wire.
    """
    data = ptc_message.data

    # PTC attestation must be for a known block. If block is unknown, delay consideration until the block is found
    assert data.beacon_block_root in store.block_states
    state = store.block_states[data.beacon_block_root]

    # PTC votes can only change the vote for their assigned beacon block, return early otherwise
    if data.slot != state.slot:
        return

    # Get all positions of the attester in the PTC
    ptc_indices = []
    # [Modified in PQ:EIP8453]
    # A block before the fork has the EIP-8483 PTC
    if isinstance(state, eip8483.BeaconState):
        ptc = eip8483.get_ptc(state, data.slot)
    else:
        ptc = get_ptc(state, data.slot)
    for ptc_index, validator_index in enumerate(ptc):
        if validator_index == ptc_message.validator_index:
            ptc_indices.append(ptc_index)

    # Check that the attester is from the PTC
    assert len(ptc_indices) > 0

    # Verify the signature and check that its for the current slot if it is coming from the wire
    if not is_from_block:
        # [Modified in PQ]
        # Check that the attestation is for the current slot
        assert data.slot == get_current_slot(store)
        # A message from the wire is for the current slot, so for a block after the
        # store conversion
        assert isinstance(state, BeaconState)
        # Verify the signature
        domain = get_domain(state, DOMAIN_PTC_ATTESTER, compute_epoch_at_slot(data.slot))
        signing_root = compute_signing_root(data, domain)
        leaf_index = compute_xmss_leaf_index(data.slot, LEAF_OFFSET_PTC_ATTESTER)
        pubkey = state.validators[ptc_message.validator_index].pubkey
        assert xmss_verify(pubkey, leaf_index, signing_root, ptc_message.signature)

    # Update the votes for the block
    payload_timeliness_vote = store.payload_timeliness_vote[data.beacon_block_root]
    payload_data_availability_vote = store.payload_data_availability_vote[data.beacon_block_root]
    for ptc_index in ptc_indices:
        payload_timeliness_vote[ptc_index] = data.payload_present
        payload_data_availability_vote[ptc_index] = data.blob_data_available
```

### New `on_head_vote_message`

*Note*: Validates and stores a head vote on receipt. Its head must descend from
the finalized checkpoint at admission. Tallies resolve support using the current
frame and compute weight from the current tally state.

```python
def on_head_vote_message(store: Store, message: HeadVoteMessage) -> None:
    current_slot = get_current_slot(store)
    previous_slot = saturating_sub(current_slot, 1)
    data = message.data
    if (
        data.beacon_block_root not in store.blocks
        or data.beacon_block_root not in store.block_states
    ):
        return
    finalized_root = store.finalized_checkpoint.root
    if finalized_root != Root() and not is_ancestor_root(
        store, data.beacon_block_root, finalized_root
    ):
        return
    block_slot = store.blocks[data.beacon_block_root].slot
    assert block_slot <= data.slot
    if block_slot == data.slot:
        assert data.payload_status == PAYLOAD_STATUS_PENDING
    else:
        assert data.payload_status in (PAYLOAD_STATUS_EMPTY, PAYLOAD_STATUS_FULL)

    early = data.slot == current_slot + 1
    if early:
        slot_start = compute_time_at_slot_ms(store.genesis_time_ms, message.data.slot)
        if store.time_ms + MAXIMUM_GOSSIP_CLOCK_DISPARITY < slot_start:
            return
    elif data.slot not in [previous_slot, current_slot]:
        return

    state = get_block_state(store, data.beacon_block_root)
    assert message.validator_index < len(state.validators)
    pubkey = state.validators[message.validator_index].pubkey
    if early:
        pool = store.pending_head_votes
    else:
        pool = [
            (prior, store.validated_head_votes[hash_tree_root(prior)])
            for prior in store.head_vote_messages.get(data.slot, [])
        ]
    previous = [
        prior.data for prior, vote in pool if vote.pubkey == pubkey and vote.slot == data.slot
    ]
    if data in previous or len(previous) >= 2:
        return
    assert is_valid_head_vote_opening(state, message)
    vote = get_ac_vote(state, message)
    if not has_ac_admission_ticket(get_ac_tally_state(store), vote):
        return
    assert is_valid_head_vote_signature(state, message)
    if early:
        store.pending_head_votes.append((message, vote))
        return

    # Record the message
    record_head_vote_message(store, message, vote)
    get_ac_vote_head(store)
```
