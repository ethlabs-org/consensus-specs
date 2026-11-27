# PQ -- Available Chain

*Note*: This document is a work-in-progress for researchers and implementers.

<!-- mdformat-toc start --slug=github --no-anchors --maxlevel=6 --minlevel=2 -->

- [Introduction](#introduction)
- [Constants](#constants)
  - [Time](#time)
- [Configs](#configs)
  - [Misc](#misc)
- [Helpers](#helpers)
  - [New `ACLock`](#new-aclock)
  - [New `get_head_vote_due_ms`](#new-get_head_vote_due_ms)
  - [New `get_support_cutoff_ms`](#new-get_support_cutoff_ms)
  - [New `get_view_freeze_ms`](#new-get_view_freeze_ms)
  - [Head votes and locks](#head-votes-and-locks)
    - [New `ACVote`](#new-acvote)
    - [New `get_ac_vote`](#new-get_ac_vote)
    - [New `get_ac_tally_state`](#new-get_ac_tally_state)
    - [New `get_ac_vote_validator_index`](#new-get_ac_vote_validator_index)
    - [New `compute_ac_vote_weight`](#new-compute_ac_vote_weight)
    - [New `has_ac_admission_ticket`](#new-has_ac_admission_ticket)
    - [New `sum_head_vote_weights`](#new-sum_head_vote_weights)
    - [New `get_block_and_payload_receipt_time`](#new-get_block_and_payload_receipt_time)
    - [New `get_vote_resolution_time`](#new-get_vote_resolution_time)
    - [New `get_head_vote_messages_by_cutoff`](#new-get_head_vote_messages_by_cutoff)
    - [New `get_declared_votes_and_readiness`](#new-get_declared_votes_and_readiness)
    - [New `is_usable_proposer_support`](#new-is_usable_proposer_support)
    - [New `get_votes_declared_in_proposals`](#new-get_votes_declared_in_proposals)
    - [New `compute_merged_view`](#new-compute_merged_view)
    - [New `get_head_votes_by_cutoff`](#new-get_head_votes_by_cutoff)
    - [New `get_equivocating_pubkeys`](#new-get_equivocating_pubkeys)
    - [New `get_viable_ancestor`](#new-get_viable_ancestor)
    - [New `get_live_locks`](#new-get_live_locks)
    - [New `conflicts_with_lock`](#new-conflicts_with_lock)
    - [New `compute_fast_lock_expiry`](#new-compute_fast_lock_expiry)
    - [New `compute_ghost_head`](#new-compute_ghost_head)
    - [New `compute_head_walk_score`](#new-compute_head_walk_score)
    - [New `is_eligible_for_head_walk`](#new-is_eligible_for_head_walk)
    - [New `get_head_walk_anchor`](#new-get_head_walk_anchor)
    - [New `get_head_in_tree`](#new-get_head_in_tree)
    - [New `get_voter_subtree`](#new-get_voter_subtree)
    - [New `compute_confirmation_score`](#new-compute_confirmation_score)
    - [New `get_confirmed_head`](#new-get_confirmed_head)
    - [New `compute_confirmed_lock`](#new-compute_confirmed_lock)
    - [New `compute_fast_lock`](#new-compute_fast_lock)
    - [New `compute_slow_lock`](#new-compute_slow_lock)
  - [Store mutators](#store-mutators)
    - [New `add_locks`](#new-add_locks)
    - [New `update_locks_and_confirmation`](#new-update_locks_and_confirmation)
    - [New `record_head_vote_message`](#new-record_head_vote_message)
  - [`on_tick` helpers](#on_tick-helpers)
    - [New `update_confirmation`](#new-update_confirmation)
    - [New `unlock`](#new-unlock)
  - [`on_head_vote_message` helpers](#on_head_vote_message-helpers)
    - [New `is_valid_head_vote_opening`](#new-is_valid_head_vote_opening)
    - [New `is_valid_head_vote_signature`](#new-is_valid_head_vote_signature)
- [Internal API](#internal-api)
  - [New `get_ac_proposal`](#new-get_ac_proposal)
  - [New `is_ac_vote_ready`](#new-is_ac_vote_ready)
  - [New `get_ac_vote_head`](#new-get_ac_vote_head)
  - [New `get_confirmed_execution_block_hash`](#new-get_confirmed_execution_block_hash)
- [External API](#external-api)
  - [New `get_confirmed_node`](#new-get_confirmed_node)
  - [Modified `get_head`](#modified-get_head)

<!-- mdformat-toc end -->

## Introduction

The available chain uses the shared
[fork-choice store](./local-store.md#modified-store) and the
[FG frame](./finality-gadget.md#new-get_fg_frame). Its fast locks supply the
[FG source](./finality-gadget.md#new-get_ac_frame).

## Constants

### Time

| Name               | Value        |
| ------------------ | ------------ |
| `INFINITE_TIME_MS` | `UINT64_MAX` |

## Configs

Slot timing uses the
[fork-choice time parameters](./local-store.md#time-parameters).

### Misc

| Name                  | Value        | Description                                              |
| --------------------- | ------------ | -------------------------------------------------------- |
| `SLOW_LOCK_WINDOW`    | `Slot(2**5)` | Slow-confirmation window and max lock duration, in slots |
| `FAST_LOCK_EXTENSION` | `Slot(2)`    | Slots added to the latest live fast expiry               |

Both `SLOW_LOCK_WINDOW` and `FAST_LOCK_EXTENSION` must be at least two.

## Helpers

### New `ACLock`

*Note*: A lock records its node, vote slot, whether it is fast, and its last
live slot. It expires at the start of `expiry_slot + 1`. Moving the lock to an
ancestor preserves its other fields. Fast and slow locks remain separate, even
when they lock the same node or have the same expiry.

```python
@dataclass(eq=True, frozen=True)
class ACLock:
    node: ForkChoiceNode
    vote_slot: Slot
    fast: bool
    expiry_slot: Slot
```

### New `get_head_vote_due_ms`

```python
def get_head_vote_due_ms() -> Uint64:
    return get_slot_component_duration_ms(HEAD_VOTE_DUE_BPS)
```

### New `get_support_cutoff_ms`

```python
def get_support_cutoff_ms() -> Uint64:
    return get_slot_component_duration_ms(SUPPORT_CUTOFF_BPS)
```

### New `get_view_freeze_ms`

```python
def get_view_freeze_ms() -> Uint64:
    return get_slot_component_duration_ms(VIEW_FREEZE_BPS)
```

### Head votes and locks

#### New `ACVote`

*Note*: Represents an individual AC vote in a form suitable for set operations,
including deduplication and merging votes from messages and blocks.

```python
@dataclass(eq=True, frozen=True)
class ACVote:
    slot: Slot
    pubkey: XMSSPubkey
    supported_node: ForkChoiceNode
    vrf_root: Root
    secret: Bytes32
```

#### New `get_ac_vote`

```python
def get_ac_vote(state: BeaconState, message: HeadVoteMessage) -> ACVote:
    data = message.data
    validator = state.validators[message.validator_index]
    supported_node = ForkChoiceNode(
        root=data.beacon_block_root,
        payload_status=data.payload_status,
    )
    return ACVote(
        slot=data.slot,
        pubkey=validator.pubkey,
        supported_node=supported_node,
        vrf_root=validator.vrf_root,
        secret=message.proof[0],
    )
```

#### New `get_ac_tally_state`

Use the accepted PQ post-state with the highest dynasty, breaking ties by block
root. This high-water mark includes accepted blocks that later fail the FG
height filter; the filter still controls which heads receive support. Before the
first PQ block, use the upgraded finalized state for dynasty zero. The state is
not pinned to a slot. Importing a block can change the next tally, without
revisiting locks or votes already prepared at their scheduled times.

```python
def get_ac_tally_state(store: Store) -> BeaconState:
    root = store.ac_tally_root
    return get_block_state(store, store.finalized_checkpoint.root if root is None else root)
```

#### New `get_ac_vote_validator_index`

The voted head authenticated the key and opening; the tally state's registry
supplies the index for the lottery when both the key and commitment match.

```python
def get_ac_vote_validator_index(state: BeaconState, vote: ACVote) -> ValidatorIndex | None:
    for index, validator in enumerate(state.validators):
        if validator.pubkey == vote.pubkey:
            return ValidatorIndex(index) if validator.vrf_root == vote.vrf_root else None
    return None
```

#### New `compute_ac_vote_weight`

The tally uses its current dynasty, even for a vote from the preceding slot.
With the same snapshot history, adjacent dynasties replace the draws for
`HEAD_VOTE_SEATS_PER_DYNASTY` seat indices. Each version of each lane has one
ticket in expectation.

```python
def compute_ac_vote_weight(state: BeaconState, vote: ACVote) -> Uint64:
    index = get_ac_vote_validator_index(state, vote)
    if index is None:
        return Uint64(0)
    return compute_ac_ticket_weight(
        state.ac_snapshots, state.dynasty, vote.slot, index, vote.secret
    )
```

#### New `has_ac_admission_ticket`

Check the local tally state's current and previous lotteries for the signed
slot. A previous-only winner can propagate without contributing to a current
tally.

```python
def has_ac_admission_ticket(state: BeaconState, vote: ACVote) -> bool:
    index = get_ac_vote_validator_index(state, vote)
    if index is None:
        return False
    return compute_ac_ticket_weight(
        state.ac_snapshots, state.dynasty, vote.slot, index, vote.secret
    ) > 0 or (
        state.dynasty > 0
        and compute_ac_ticket_weight(
            state.ac_snapshots, state.dynasty - 1, vote.slot, index, vote.secret
        )
        > 0
    )
```

#### New `sum_head_vote_weights`

Each vote supports its signed head. When an equivocation rule counts a voter
once in a tally, use the maximum weight of that voter's applicable votes.

```python
def sum_head_vote_weights(
    store: Store, votes: Sequence[ACVote] | set[ACVote], slot: Slot
) -> Uint64:
    state = get_ac_tally_state(store)
    weights: dict[XMSSPubkey, Uint64] = {}
    for vote in votes:
        if vote.slot == slot:
            weights[vote.pubkey] = max(
                weights.get(vote.pubkey, Uint64(0)), compute_ac_vote_weight(state, vote)
            )
    return Uint64(sum(weights.values()))
```

#### New `get_block_and_payload_receipt_time`

*Note*: An *empty* or *pending* node uses the beacon block’s receipt time. A
*full* node uses the later of the beacon block and payload receipt times.

```python
def get_block_and_payload_receipt_time(store: Store, node: ForkChoiceNode) -> Uint64:
    """
    Return ``node`` receipt time.
    """
    if node.payload_status == PAYLOAD_STATUS_FULL:
        return max(store.block_receipt_times[node.root], store.payload_receipt_times[node.root])
    else:
        return store.block_receipt_times[node.root]
```

#### New `get_vote_resolution_time`

*Note*: For unresolved votes `INFINITE_TIME_MS` is returned.

```python
def get_vote_resolution_time(
    store: Store, supported_node: ForkChoiceNode, vote_receipt_time: Uint64
) -> Uint64:
    """
    Return the resolution time of the vote.
    """
    block_root = supported_node.root
    payload_status = supported_node.payload_status

    # Votes must be for a known block
    if block_root not in store.blocks:
        return INFINITE_TIME_MS

    # on_head_vote_message admits only heads at or before the vote slot, with
    # PENDING exactly for the vote's own-slot block.

    # Full votes require a verified payload
    if payload_status == PAYLOAD_STATUS_FULL and not is_payload_verified(store, block_root):
        return INFINITE_TIME_MS

    # Votes must support blocks belonging to the FG frame
    if not is_ancestor_root(store, supported_node.root, store.finalized_checkpoint.root):
        return INFINITE_TIME_MS

    return max(vote_receipt_time, get_block_and_payload_receipt_time(store, supported_node))
```

#### New `get_head_vote_messages_by_cutoff`

*Note*: Builds an AC view from P2P messages using locally observed receipt and
resolution times.

```python
def get_head_vote_messages_by_cutoff(
    store: Store, vote_slot: Slot, receipt_threshold_ms: Uint64, resolution_threshold_ms: Uint64
) -> tuple[list[HeadVoteMessage], list[int]]:
    """
    Return messages for ``vote_slot`` received before ``receipt_threshold_ms``,
    and indices of those votes that are resolved before ``resolution_threshold_ms``.
    """
    vote_messages: list[HeadVoteMessage] = []
    support_indices: list[int] = []

    for message in store.head_vote_messages.get(vote_slot, []):
        data = message.data
        message_root = hash_tree_root(message)
        message_receipt_ms = store.head_vote_message_receipt_times[message_root]
        # Include messages received before the receipt threshold
        if message_receipt_ms < receipt_threshold_ms:
            vote_messages.append(message)
            supported_node = ForkChoiceNode(
                root=data.beacon_block_root, payload_status=data.payload_status
            )
            # Record the indices of included messages whose votes resolve before the support threshold
            if (
                get_vote_resolution_time(store, supported_node, message_receipt_ms)
                < resolution_threshold_ms
            ):
                support_indices.append(len(vote_messages) - 1)

    return vote_messages, support_indices
```

#### New `get_declared_votes_and_readiness`

*Note*: Readiness checks for an exact authenticated vote or two distinct votes
by the declared validator, regardless of their current ticket counts. The two
votes enter the view as equivocation evidence without proposer support.

```python
def get_declared_votes_and_readiness(
    store: Store, tree: Sequence[ForkChoiceNode], slot: Slot
) -> tuple[set[ACVote], set[ACVote], bool]:
    votes: set[ACVote] = set()
    support: set[ACVote] = set()
    all_held = True
    ready_proposal = False
    if slot == GENESIS_SLOT:
        return votes, support, False
    vote_slot = Slot(slot - 1)
    signed: dict[tuple[ValidatorIndex, Root], ACVote] = {}
    by_pubkey: dict[XMSSPubkey, set[ACVote]] = {}
    for message in store.head_vote_messages.get(vote_slot, []):
        vote = store.validated_head_votes[hash_tree_root(message)]
        signed[(message.validator_index, hash_tree_root(message.data))] = vote
        if vote.pubkey not in by_pubkey:
            by_pubkey[vote.pubkey] = set()
        by_pubkey[vote.pubkey].add(vote)
    for root in {node.root for node in tree}:
        block = store.blocks[root]
        if block.slot != slot:
            continue
        block = cast("BeaconBlock", block)
        ready = True
        for declaration, marked in zip(
            block.body.head_vote_declarations, block.body.head_vote_support_bits, strict=True
        ):
            index = declaration.validator_index
            data = HeadVoteData(
                slot=vote_slot,
                beacon_block_root=declaration.beacon_block_root,
                payload_status=declaration.payload_status,
            )
            vote = signed.get((index, hash_tree_root(data)))
            if vote is None:
                evidence: set[ACVote] = set()
                if data.beacon_block_root in store.block_states:
                    state = get_block_state(store, data.beacon_block_root)
                    if index < len(state.validators):
                        evidence = by_pubkey.get(state.validators[index].pubkey, set())
                all_held &= len(evidence) == 2
                if len(evidence) == 2:
                    votes.update(evidence)
                continue
            votes.add(vote)
            if marked:
                if is_usable_proposer_support(store, tree, data):
                    support.add(vote)
                else:
                    ready = False
        ready_proposal |= ready
    return votes, support, all_held and ready_proposal
```

#### New `is_usable_proposer_support`

```python
def is_usable_proposer_support(
    store: Store, tree: Sequence[ForkChoiceNode], data: HeadVoteData
) -> bool:
    node = ForkChoiceNode(root=data.beacon_block_root, payload_status=data.payload_status)
    return (
        node in tree
        and store.blocks[node.root].slot <= data.slot
        and (
            (store.blocks[node.root].slot == data.slot)
            == (data.payload_status == PAYLOAD_STATUS_PENDING)
        )
    )
```

#### New `get_votes_declared_in_proposals`

*Note*: Builds an AC proposer view. Supporting votes must reference locally
known blocks. *Full* block support additionally requires a verified payload.

```python
def get_votes_declared_in_proposals(
    store: Store, tree: Sequence[ForkChoiceNode], block_slot: Slot
) -> tuple[set[ACVote], set[ACVote]]:
    """
    Return the held declarations and usable support of the slot's proposals.
    """

    votes, support, _ = get_declared_votes_and_readiness(store, tree, block_slot)
    return votes, support
```

#### New `compute_merged_view`

*Note*: Merges views from P2P messages and blocks into sets of single AC votes,
deduplicating votes and combining their support.

```python
def compute_merged_view(
    store: Store,
    votes_from_messages: tuple[list[HeadVoteMessage], list[int]],
    votes_from_blocks: tuple[set[ACVote], set[ACVote]],
) -> tuple[set[ACVote], set[ACVote]]:
    """
    Return the merged vote set and its supporting subset.
    """
    votes: set[ACVote] = set()
    support_votes: set[ACVote] = set()

    # Process p2p message votes
    for index, message_vote in enumerate(votes_from_messages[0]):
        vote = store.validated_head_votes[hash_tree_root(message_vote)]
        votes.add(vote)
        if index in votes_from_messages[1]:
            support_votes.add(vote)

    return votes | votes_from_blocks[0], support_votes | votes_from_blocks[1]
```

#### New `get_head_votes_by_cutoff`

```python
def get_head_votes_by_cutoff(
    store: Store, slot: Slot, resolution_threshold_ms: Uint64
) -> tuple[set[ACVote], set[ACVote]]:
    """
    Return all signed pool votes for ``slot`` and the subset resolved before the
    resolution threshold.
    """
    votes_from_messages = get_head_vote_messages_by_cutoff(
        store, slot, INFINITE_TIME_MS, resolution_threshold_ms
    )
    return compute_merged_view(store, votes_from_messages, (set(), set()))
```

#### New `get_equivocating_pubkeys`

```python
def get_equivocating_pubkeys(votes: set[ACVote]) -> set[XMSSPubkey]:
    """
    Return public keys of validators with multiple distinct votes.
    """
    seen: set[XMSSPubkey] = set()
    equivocating: set[XMSSPubkey] = set()
    for vote in votes:
        if vote.pubkey in seen:
            equivocating.add(vote.pubkey)
        seen.add(vote.pubkey)
    return equivocating
```

#### New `get_viable_ancestor`

```python
def get_viable_ancestor(
    store: Store,
    fg_frame: FGFrame,
    tree: Sequence[ForkChoiceNode],
    viable_tree: Sequence[ForkChoiceNode],
    node: ForkChoiceNode,
) -> ForkChoiceNode:
    """
    Return the deepest ancestor that is viable or an ancestor of the forkchoice root.
    """
    return get_deepest_node(
        store,
        {
            ancestor
            for ancestor in tree
            if is_ancestor(store, node, ancestor)
            and (ancestor in viable_tree or is_ancestor(store, fg_frame.forkchoice_root, ancestor))
        },
    )
```

#### New `get_live_locks`

*Note*: Every lock read uses its viable ancestor. Lock updates store this
ancestor so that a later change to the FG frame cannot restore the old node.

```python
def get_live_locks(
    store: Store, fg_frame: FGFrame, tree: Sequence[ForkChoiceNode], time_ms: Uint64
) -> set[ACLock]:
    """
    Return unexpired locks in the frame, each moved to its viable ancestor.
    """
    viable_tree = get_viable_subtree(store, fg_frame, tree)
    return {
        ACLock(
            node=get_viable_ancestor(store, fg_frame, tree, viable_tree, lock.node),
            vote_slot=lock.vote_slot,
            fast=lock.fast,
            expiry_slot=lock.expiry_slot,
        )
        for lock in store.locks
        if lock.node in tree
        and time_ms < compute_time_at_slot_ms(store.genesis_time_ms, lock.expiry_slot + 1)
    }
```

#### New `conflicts_with_lock`

```python
def conflicts_with_lock(store: Store, live_locks: set[ACLock], node: ForkChoiceNode) -> bool:
    return any(not is_compatible(store, lock.node, node) for lock in live_locks)
```

#### New `compute_fast_lock_expiry`

*Note*: Only live fast locks from slots before `slot` extend the expiry. Their
nodes need not be on the new lock's branch. A lock moved to the finalized node
still contributes. Locks from the same or a newer slot do not contribute.

```python
def compute_fast_lock_expiry(live_locks: set[ACLock], slot: Slot) -> Slot:
    expiries = {lock.expiry_slot for lock in live_locks if lock.fast and lock.vote_slot < slot}
    if len(expiries) == 0:
        return slot + 2
    return min(slot + SLOW_LOCK_WINDOW, max(slot + 2, max(expiries) + FAST_LOCK_EXTENSION))
```

#### New `compute_ghost_head`

```python
def compute_ghost_head(
    store: Store,
    anchor: ForkChoiceNode,
    tree: Sequence[ForkChoiceNode],
    get_score: Callable[[ForkChoiceNode], Uint64],
    is_eligible: Callable[[ForkChoiceNode], bool],
) -> ForkChoiceNode:
    """
    Return the head reached by a GHOST walk from ``anchor``,
    using the supplied scoring and eligibility functions.
    """
    head = anchor
    while True:
        eligible_children = [
            child
            for child in get_node_children(store, head)
            if child in tree and is_eligible(child)
        ]
        if len(eligible_children) == 0:
            return head
        head = max(
            eligible_children,
            key=lambda child: (
                get_score(child),
                child.root,
                get_payload_status_tiebreaker(store, child),
            ),
        )
```

#### New `compute_head_walk_score`

*Note*: Equivocating validators contribute their support to every node. The
node’s supporting score counts the committee seats of non-equivocating
validators whose support vote is for the node or one of its descendants.

```python
def compute_head_walk_score(
    store: Store,
    votes: set[ACVote],
    support_votes: set[ACVote],
    slot: Slot,
    node: ForkChoiceNode,
) -> Uint64:
    """
    Return the sum of the equivocating score and the node's supporting score.
    """
    equivocating_pubkeys = get_equivocating_pubkeys(votes)
    scoring_votes = {vote for vote in votes if vote.pubkey in equivocating_pubkeys}
    scoring_votes |= {
        vote for vote in support_votes if is_ancestor(store, vote.supported_node, node)
    }
    return sum_head_vote_weights(store, scoring_votes, slot)
```

#### New `is_eligible_for_head_walk`

*Note*: A node is eligible for the head walk if it is a full or empty node from
the previous slot, its parent block is not yet a viable head, its block is from
the current slot, or its head-walk score exceeds half the participating tickets.

```python
def is_eligible_for_head_walk(
    store: Store,
    fg_frame: FGFrame,
    votes: set[ACVote],
    support_votes: set[ACVote],
    slot: Slot,
    node: ForkChoiceNode,
) -> bool:
    """
    Return ``True`` if the head walk may advance into ``node``.
    """
    block = store.blocks[node.root]

    # Full or empty nodes from the previous slot are always eligible
    if is_previous_slot_payload_decision(store, node):
        return True

    parent = ForkChoiceNode(root=block.parent_root, payload_status=PAYLOAD_STATUS_PENDING)
    if parent not in fg_frame.viable_heads:
        return True

    if block.slot == get_current_slot(store):
        return True

    voting_seats_count = sum_head_vote_weights(store, votes, slot)
    score = compute_head_walk_score(store, votes, support_votes, slot, node)

    # Require a strict majority of participating tickets
    return 2 * score > voting_seats_count
```

#### New `get_head_walk_anchor`

*Note*: Use the full FG frame even when the caller selects votes and blocks from
a restricted view.

```python
def get_head_walk_anchor(
    store: Store, fg_frame: FGFrame, finalized_subtree: Sequence[ForkChoiceNode]
) -> ForkChoiceNode:
    """
    Return the deepest live lock node or the forkchoice root.
    """
    return get_deepest_node(
        store,
        {lock.node for lock in get_live_locks(store, fg_frame, finalized_subtree, store.time_ms)}
        | {fg_frame.forkchoice_root},
    )
```

#### New `get_head_in_tree`

*Note*: Walks the viable subtree of tree from the head-walk anchor, using the
head-walk scoring and eligibility rules. The anchor is selected from the full FG
frame independently of tree.

```python
def get_head_in_tree(
    store: Store,
    fg_frame: FGFrame,
    finalized_subtree: Sequence[ForkChoiceNode],
    tree: Sequence[ForkChoiceNode],
    votes: set[ACVote],
    support_votes: set[ACVote],
    slot: Slot,
) -> ForkChoiceNode:
    """
    Return the head of the tree.
    """
    anchor = get_head_walk_anchor(store, fg_frame, finalized_subtree)
    viable_subtree = get_viable_subtree(store, fg_frame, tree)
    return compute_ghost_head(
        store=store,
        anchor=anchor,
        tree=viable_subtree,
        get_score=lambda node: compute_head_walk_score(store, votes, support_votes, slot, node),
        is_eligible=lambda node: is_eligible_for_head_walk(
            store, fg_frame, votes, support_votes, slot, node
        ),
    )
```

#### New `get_voter_subtree`

*Note*: Return a subtree of nodes received before the previous slot's
view-freeze deadline, together with ancestors of proposals from the voting slot,
including the proposals themselves.

```python
def get_voter_subtree(
    store: Store, tree: Sequence[ForkChoiceNode], slot: Slot
) -> Sequence[ForkChoiceNode]:
    """
    Return the subtree available to the voter in ``slot``.
    """
    view_freeze_ms = compute_time_at_slot_ms(store.genesis_time_ms, slot - 1) + get_view_freeze_ms()
    slot_proposals = {node for node in tree if store.blocks[node.root].slot == slot}
    return [
        node
        for node in tree
        if get_block_and_payload_receipt_time(store, node) < view_freeze_ms
        or any(is_ancestor(store, proposal, node) for proposal in slot_proposals)
    ]
```

#### New `compute_confirmation_score`

*Note*: The confirmation score counts the committee seats of non-equivocating
validators whose votes are in support votes set and support the node or one of
its descendants. Equivocating validators contribute no support.

```python
def compute_confirmation_score(
    store: Store,
    votes: set[ACVote],
    support_votes: set[ACVote],
    slot: Slot,
    node: ForkChoiceNode,
) -> Uint64:
    """
    Return confirmation score of the ``node``.
    """
    equivocating_pubkeys = get_equivocating_pubkeys(votes)
    return sum_head_vote_weights(
        store,
        [
            vote
            for vote in support_votes
            if vote.pubkey not in equivocating_pubkeys
            and is_ancestor(store, vote.supported_node, node)
        ],
        slot,
    )
```

#### New `get_confirmed_head`

*Note*: Runs a confirmation walk using the confirmation score, requiring
eligible nodes to score more than half the participating tickets. Returns `None`
if the resulting head is ineligible, including when the walk stops at the
finalized node.

```python
def get_confirmed_head(
    store: Store,
    fg_frame: FGFrame,
    tree: Sequence[ForkChoiceNode],
    votes: set[ACVote],
    support_votes: set[ACVote],
    slot: Slot,
) -> ForkChoiceNode | None:
    """
    Return the confirmation candidate, or ``None`` if no node has sufficient support.
    """
    voting_seats_count = sum_head_vote_weights(store, votes, slot)

    def is_eligible(node: ForkChoiceNode) -> bool:
        return (
            2 * compute_confirmation_score(store, votes, support_votes, slot, node)
            > voting_seats_count
        )

    head = compute_ghost_head(
        store=store,
        anchor=fg_frame.finalized_node,
        tree=tree,
        get_score=lambda node: compute_confirmation_score(store, votes, support_votes, slot, node),
        is_eligible=is_eligible,
    )

    if is_eligible(head):
        return head
    else:
        return None
```

#### New `compute_confirmed_lock`

*Note*: Computes the confirmed path and the lock as the deepest node in the
confirmed path. Candidate nodes are selected from the FG frame, excluding
genesis and nodes that conflict with a live lock.

```python
def compute_confirmed_lock(
    store: Store,
    tree: Sequence[ForkChoiceNode],
    live_locks: set[ACLock],
    confirmed: ForkChoiceNode,
    test_predicate: Callable[[ForkChoiceNode], bool],
) -> ForkChoiceNode | None:
    """
    Return the deepest selected path node, or ``None``.
    """
    path = {
        node
        for node in tree
        if is_ancestor(store, confirmed, node)
        and store.blocks[node.root].slot > GENESIS_SLOT
        and not conflicts_with_lock(store, live_locks, node)
        and test_predicate(node)
    }

    return get_deepest_node(store, path) if len(path) > 0 else None
```

#### New `compute_fast_lock`

*Note*: Selects non-genesis nodes on the path to `confirmed` within the FG frame
whose confirmation support reaches the fast-lock ticket threshold. The deepest
selected node is the fast lock.

```python
def compute_fast_lock(
    store: Store,
    tree: Sequence[ForkChoiceNode],
    live_locks: set[ACLock],
    votes: set[ACVote],
    support_votes: set[ACVote],
    slot: Slot,
    confirmed: ForkChoiceNode,
) -> ForkChoiceNode | None:
    """
    Return the fast lock, or ``None``.
    """
    return compute_confirmed_lock(
        store=store,
        tree=tree,
        live_locks=live_locks,
        confirmed=confirmed,
        test_predicate=lambda node: (
            compute_confirmation_score(store, votes, support_votes, slot, node)
            >= HEAD_VOTE_FAST_LOCK_THRESHOLD
        ),
    )
```

#### New `compute_slow_lock`

*Note*: The slow lock requires:

- Saved confirmations in the FG frame for each of the previous
  `SLOW_LOCK_WINDOW - 1` slots.
- A non-genesis node in the FG frame that is an ancestor of or equal to both the
  current candidate and every saved confirmation in that window.

The deepest such node is the slow lock.

```python
def compute_slow_lock(
    store: Store,
    tree: Sequence[ForkChoiceNode],
    live_locks: set[ACLock],
    slot: Slot,
    confirmed: ForkChoiceNode,
) -> ForkChoiceNode | None:
    """
    Return the slow lock, or ``None`` if the window or common ancestor is missing.
    """
    if slot + 1 < SLOW_LOCK_WINDOW:
        return None

    previous_slots = range(slot + 1 - SLOW_LOCK_WINDOW, slot)
    if not all(
        Slot(j) in store.saved_confirmations and store.saved_confirmations[Slot(j)] in tree
        for j in previous_slots
    ):
        return None

    slow_lock = compute_confirmed_lock(
        store=store,
        tree=tree,
        live_locks=live_locks,
        confirmed=confirmed,
        test_predicate=lambda node: all(
            is_ancestor(store, store.saved_confirmations[Slot(j)], node) for j in previous_slots
        ),
    )
    return slow_lock
```

### Store mutators

#### New `add_locks`

```python
def add_locks(
    store: Store, fg_frame: FGFrame, tree: Sequence[ForkChoiceNode], locks: set[ACLock]
) -> None:
    """
    Add locks, remove expired or out-of-frame locks, and store their viable ancestors.
    """
    store.locks |= locks
    store.locks = get_live_locks(store, fg_frame, tree, store.time_ms)
```

#### New `update_locks_and_confirmation`

```python
def update_locks_and_confirmation(
    store: Store,
    slot: Slot,
    fg_frame: FGFrame,
    tree: Sequence[ForkChoiceNode],
    live_locks: set[ACLock],
    confirmed: ForkChoiceNode,
    fast_lock: ForkChoiceNode | None,
    slow_lock: ForkChoiceNode | None,
) -> None:
    """
    Record the candidate and separate fast and slow locks, even when confirmation is vetoed.
    """
    store.saved_confirmations[slot] = confirmed
    vetoed = conflicts_with_lock(store, live_locks, confirmed)

    locks = set()
    if fast_lock is not None:
        locks.add(
            ACLock(
                node=fast_lock,
                vote_slot=slot,
                fast=True,
                expiry_slot=compute_fast_lock_expiry(live_locks, slot),
            )
        )
    if slow_lock is not None:
        locks.add(
            ACLock(node=slow_lock, vote_slot=slot, fast=False, expiry_slot=slot + SLOW_LOCK_WINDOW)
        )

    # A veto stops the confirmation record, not locks on compatible ancestors.
    if not vetoed and not (
        store.latest_confirmed in tree and is_ancestor(store, store.latest_confirmed, confirmed)
    ):
        store.latest_confirmed = confirmed

    # Store viable ancestors even when neither test creates a new lock.
    add_locks(store, fg_frame, tree, locks)
```

#### New `record_head_vote_message`

```python
def record_head_vote_message(store: Store, message: HeadVoteMessage, vote: ACVote) -> None:
    """
    Store the vote with its receipt time, authenticated key and opening.
    """
    slot_pool = store.head_vote_messages.get(message.data.slot, [])
    slot_pool.append(message)
    store.head_vote_messages[message.data.slot] = slot_pool

    # Add receipt time
    message_root = hash_tree_root(message)
    store.head_vote_message_receipt_times[message_root] = store.time_ms
    store.validated_head_votes[message_root] = vote
```

### `on_tick` helpers

#### New `update_confirmation`

*Notes*: This function:

- Computes and records a confirmation candidate using all locally known votes
  for the slot and support resolved before the cutoff.
- Updates the confirmed head only when no live lock conflicts with the
  candidate.
- Adds separate fast and slow locks on qualifying ancestors that conflict with
  no live lock, even when the candidate itself is vetoed.

```python
def update_confirmation(store: Store, slot: Slot) -> None:
    fg_frame = get_fg_frame(store)
    tree = get_subtree(store, fg_frame.finalized_node)
    support_cutoff_ms = (
        compute_time_at_slot_ms(store.genesis_time_ms, slot) + get_support_cutoff_ms()
    )
    votes, support_votes = get_head_votes_by_cutoff(store, slot, support_cutoff_ms)
    confirmed = get_confirmed_head(store, fg_frame, tree, votes, support_votes, slot)

    # Return if there is no confirmation
    if confirmed is None:
        store.saved_confirmations.pop(slot, None)
        return

    live_locks = get_live_locks(store, fg_frame, tree, store.time_ms)
    fast_lock = compute_fast_lock(
        store=store,
        tree=tree,
        live_locks=live_locks,
        votes=votes,
        support_votes=support_votes,
        slot=slot,
        confirmed=confirmed,
    )
    slow_lock = compute_slow_lock(store, tree, live_locks, slot, confirmed)

    # Write updates to store
    update_locks_and_confirmation(
        store,
        slot,
        fg_frame,
        tree,
        live_locks,
        confirmed,
        fast_lock,
        slow_lock,
    )
```

#### New `unlock`

*Note*: Equivocators count as opposition to every node. Other validators count
when their resolved vote supports a conflicting node. Count each validator's
maximum applicable ticket weight. Only locks from before the vote slot can move.

```python
def unlock(
    store: Store,
    fg_frame: FGFrame,
    tree: Sequence[ForkChoiceNode],
    slot: Slot,
    votes: set[ACVote],
    support_votes: set[ACVote],
) -> None:
    """
    Move older locks using the supplied vote and support view.
    """
    equivocating_pubkeys = get_equivocating_pubkeys(votes)

    def reaches_unlock_threshold(node: ForkChoiceNode) -> bool:
        opposing_votes = {vote for vote in votes if vote.pubkey in equivocating_pubkeys}
        opposing_votes |= {
            vote
            for vote in support_votes
            if vote.slot == slot
            and vote.supported_node in tree
            and not is_compatible(store, vote.supported_node, node)
        }
        return sum_head_vote_weights(store, opposing_votes, slot) >= HEAD_VOTE_UNLOCK_THRESHOLD

    locks = set()
    for lock in get_live_locks(store, fg_frame, tree, store.time_ms):
        node = lock.node
        if lock.vote_slot < slot:
            fallback_path = {
                ancestor
                for ancestor in tree
                if is_ancestor(store, node, ancestor) and not reaches_unlock_threshold(ancestor)
            }
            node = (
                get_deepest_node(store, fallback_path)
                if len(fallback_path) > 0
                else fg_frame.finalized_node
            )
        locks.add(
            ACLock(
                node=node, vote_slot=lock.vote_slot, fast=lock.fast, expiry_slot=lock.expiry_slot
            )
        )
    store.locks = locks
```

### `on_head_vote_message` helpers

#### New `is_valid_head_vote_opening`

```python
def is_valid_head_vote_opening(state: BeaconState, message: HeadVoteMessage) -> bool:
    if not is_head_vote_payload_status_valid(state, message.data):
        return False
    if compute_epoch_at_slot(message.data.slot) < PQ_FORK_EPOCH:
        return False
    if message.validator_index >= len(state.validators):
        return False
    return is_valid_vrf_proof(state, message.validator_index, message.data.slot, message.proof)
```

#### New `is_valid_head_vote_signature`

```python
def is_valid_head_vote_signature(state: BeaconState, message: HeadVoteMessage) -> bool:
    domain = get_domain(state, DOMAIN_HEAD_VOTE, compute_epoch_at_slot(message.data.slot))
    signing_root = compute_signing_root(message.data, domain)
    leaf_index = compute_xmss_leaf_index(message.data.slot, LEAF_OFFSET_HEAD_VOTE)
    pubkey = state.validators[message.validator_index].pubkey
    return xmss_verify(pubkey, leaf_index, signing_root, message.signature)
```

## Internal API

### New `get_ac_proposal`

*Note*: At the start of a slot, selects the proposal head using received and
resolved P2P votes from the previous slot. Returns the head, declarations and
support bits. The receipt and resolution thresholds are infinite, so whether a
vote is support depends only on its data and the store, not on when the vote or
its block was received. If the declaration cap is exceeded, votes by pubkeys
with tickets in the current tally state, including their equivocation evidence,
take priority. Within each priority group, declarations keep pool order.

```python
def get_ac_proposal(
    store: Store,
) -> tuple[ForkChoiceNode, HeadVoteDeclarations, HeadVoteSupportBits]:
    assert get_time_into_slot_ms(store) == Uint64(0)

    current_slot = get_current_slot(store)
    assert current_slot > GENESIS_SLOT

    fg_frame = get_fg_frame(store)
    tree = get_subtree(store, fg_frame.finalized_node)
    vote_messages, support_indices = get_head_vote_messages_by_cutoff(
        store,
        current_slot - 1,
        receipt_threshold_ms=INFINITE_TIME_MS,
        resolution_threshold_ms=INFINITE_TIME_MS,
    )

    # Build the view from P2P messages only
    votes, support_votes = compute_merged_view(
        store, (vote_messages, support_indices), (set(), set())
    )
    unlock(store, fg_frame, tree, current_slot - 1, votes, support_votes)
    head = get_head_in_tree(
        store=store,
        fg_frame=fg_frame,
        finalized_subtree=tree,
        tree=tree,
        votes=votes,
        support_votes=support_votes,
        slot=current_slot - 1,
    )

    # Convert a pending head to empty for proposal construction
    if head.payload_status == PAYLOAD_STATUS_PENDING:
        head = ForkChoiceNode(root=head.root, payload_status=PAYLOAD_STATUS_EMPTY)

    positions = list(range(len(vote_messages)))
    limit = 4 * HEAD_VOTE_COMMITTEE_SIZE
    if len(positions) > limit:
        tally_state = get_ac_tally_state(store)
        pool_votes = [
            store.validated_head_votes[hash_tree_root(message)] for message in vote_messages
        ]
        weighted_pubkeys = {
            vote.pubkey for vote in votes if compute_ac_vote_weight(tally_state, vote) > 0
        }
        positions.sort(key=lambda position: pool_votes[position].pubkey not in weighted_pubkeys)

    declarations = HeadVoteDeclarations()
    bits = HeadVoteSupportBits()
    for position in positions[:limit]:
        message = vote_messages[position]
        declarations.append(
            HeadVoteDeclaration(
                validator_index=message.validator_index,
                beacon_block_root=message.data.beacon_block_root,
                payload_status=message.data.payload_status,
            )
        )
        bits.append(Boolean(position in support_indices))
    return head, declarations, bits
```

### New `is_ac_vote_ready`

*Note*: Declarations across current-slot proposals must be held by an exact vote
or equivocation evidence. At least one proposal must have usable heads for its
marked exact votes. A timely vote held by the proposer can fall outside the
receiver's admission window. In that case the receiver can wait until the
head-vote deadline even if it received the declared message.

```python
def is_ac_vote_ready(store: Store, tree: Sequence[ForkChoiceNode]) -> bool:
    """
    Return the current slot's readiness computed by
    ``get_declared_votes_and_readiness``.
    """
    _, _, ready = get_declared_votes_and_readiness(store, tree, get_current_slot(store))
    return ready
```

### New `get_ac_vote_head`

*Note*: Prepares the head as soon as [`is_ac_vote_ready`](#new-is_ac_vote_ready)
holds, or at the head-vote deadline without requiring readiness. Returns `None`
before readiness or after a missed deadline. The prepared result remains stable
for the slot. Merges the frozen local P2P view with views from current slot
proposals, then walks the voter's viable subtree, falling back to the full FG
tree if that subtree is empty.

```python
def get_ac_vote_head(store: Store) -> ForkChoiceNode | None:
    current_slot = get_current_slot(store)
    if store.prepared_head_vote is not None and store.prepared_head_vote[0] == current_slot:
        return store.prepared_head_vote[1]
    elapsed = get_time_into_slot_ms(store)
    if elapsed > get_head_vote_due_ms():
        return None
    fg_frame = get_fg_frame(store)
    tree = get_subtree(store, fg_frame.finalized_node)
    if elapsed < get_head_vote_due_ms() and not is_ac_vote_ready(store, tree):
        return None

    if current_slot == GENESIS_SLOT:
        store.prepared_head_vote = (current_slot, fg_frame.finalized_node)
        return fg_frame.finalized_node
    view_freeze_ms = (
        compute_time_at_slot_ms(store.genesis_time_ms, current_slot - 1) + get_view_freeze_ms()
    )
    # Local view
    local_view = get_head_vote_messages_by_cutoff(
        store,
        current_slot - 1,
        receipt_threshold_ms=view_freeze_ms,
        resolution_threshold_ms=view_freeze_ms,
    )
    # Proposer view
    proposer_view = get_votes_declared_in_proposals(store, tree, current_slot)
    # Merge views
    votes, support_votes = compute_merged_view(store, local_view, proposer_view)
    unlock(store, fg_frame, tree, current_slot - 1, votes, support_votes)

    # Run over voter viable subtree
    voter_subtree = get_voter_subtree(store, tree, current_slot)
    if len(get_viable_subtree(store, fg_frame, voter_subtree)) == 0:
        voter_subtree = tree
    head = get_head_in_tree(
        store=store,
        fg_frame=fg_frame,
        finalized_subtree=tree,
        tree=voter_subtree,
        votes=votes,
        support_votes=support_votes,
        slot=current_slot - 1,
    )

    # Current-slot votes name the block without deciding its payload.
    if store.blocks[head.root].slot == current_slot:
        head = ForkChoiceNode(root=head.root, payload_status=PAYLOAD_STATUS_PENDING)
    elif head.payload_status == PAYLOAD_STATUS_PENDING:
        head = ForkChoiceNode(root=head.root, payload_status=PAYLOAD_STATUS_EMPTY)
    store.prepared_head_vote = (current_slot, head)
    return head
```

### New `get_confirmed_execution_block_hash`

*Note*: Returns the confirmed execution block hash for the Engine API. In the
case of an empty slot a payload from a past slot can still be confirmed.

```python
def get_confirmed_execution_block_hash(store: Store) -> Hash32:
    confirmed_node = get_confirmed_node(store)
    confirmed_block = store.blocks[confirmed_node.root]
    bid = confirmed_block.body.signed_execution_payload_bid.message
    if confirmed_node.payload_status == PAYLOAD_STATUS_FULL:
        return bid.block_hash
    else:
        return bid.parent_block_hash
```

## External API

### New `get_confirmed_node`

*Note*: Returns the deepest common ancestor of the current head and the latest
confirmed node, using the finalized node when the latter is outside the FG
frame. Ancestry includes payload status, so the result can be pending.

```python
def get_confirmed_node(store: Store) -> ForkChoiceNode:
    fg_frame = get_fg_frame(store)
    tree = get_subtree(store, fg_frame.finalized_node)
    confirmed = (
        store.latest_confirmed if store.latest_confirmed in tree else fg_frame.finalized_node
    )
    head = get_head(store)
    return get_deepest_node(
        store,
        {
            node
            for node in tree
            if is_ancestor(store, confirmed, node) and is_ancestor(store, head, node)
        },
    )
```

### Modified `get_head`

*Note*: This function is modified to use the new fork-choice algorithm. It uses
current slot votes at or after the support cutoff, and previous slot votes
otherwise.

```python
def get_head(store: Store) -> ForkChoiceNode:
    current_slot = get_current_slot(store)
    if current_slot == GENESIS_SLOT:
        return ForkChoiceNode(
            root=store.finalized_checkpoint.root,
            payload_status=PAYLOAD_STATUS_EMPTY,
        )

    if get_time_into_slot_ms(store) >= get_support_cutoff_ms():
        slot = current_slot
    else:
        slot = current_slot - 1

    fg_frame = get_fg_frame(store)
    tree = get_subtree(store, fg_frame.finalized_node)
    votes, support_votes = get_head_votes_by_cutoff(store, slot, INFINITE_TIME_MS)
    head = get_head_in_tree(
        store=store,
        fg_frame=fg_frame,
        finalized_subtree=tree,
        tree=tree,
        votes=votes,
        support_votes=support_votes,
        slot=slot,
    )

    # Never return pending
    if head.payload_status == PAYLOAD_STATUS_PENDING:
        return ForkChoiceNode(root=head.root, payload_status=PAYLOAD_STATUS_EMPTY)
    else:
        return head
```
