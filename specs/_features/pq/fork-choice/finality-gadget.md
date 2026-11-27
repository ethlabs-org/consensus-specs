# PQ -- Finality Gadget

*Note*: This document is a work-in-progress for researchers and implementers.

<!-- mdformat-toc start --slug=github --no-anchors --maxlevel=6 --minlevel=2 -->

- [Introduction](#introduction)
- [Helpers](#helpers)
  - [New `FGFrame`](#new-fgframe)
  - [New `get_fg_frame`](#new-get_fg_frame)
  - [New `get_viable_subtree`](#new-get_viable_subtree)
  - [New `is_fg_viable_block`](#new-is_fg_viable_block)
  - [New `has_viable_descendant`](#new-has_viable_descendant)
- [Store updates](#store-updates)
  - [New `update_finality`](#new-update_finality)
- [Internal API](#internal-api)
  - [New `get_ac_frame`](#new-get_ac_frame)

<!-- mdformat-toc end -->

## Introduction

The finality gadget supplies the viable frame for the
[available chain](./available-chain.md) and reads
[AC locks](./available-chain.md#new-get_live_locks) for the FG source. Both use
the shared [fork-choice store](./local-store.md#modified-store).

*Provisional*: The frame is rooted at the finalized block and uses the P-U
viability rule from the
[verified-consensus adoption series](https://github.com/ethlabs-org/verified-consensus/commit/528d89635a0a0af87ec6c33d074409118e51edbb).
That series compiles without errors or `sorry` in `Solution` and passed the fast
tier and Codex and Astra reviews. This spec may be adjusted when the series
lands on `main`.

## Helpers

### New `FGFrame`

*Note*: Available Chain uses the Finality Gadget frame to constrain its decision
walks.

```python
@dataclass
class FGFrame:
    finalized_node: ForkChoiceNode
    tree: Sequence[ForkChoiceNode]
    viable_heads: set[ForkChoiceNode]
```

### New `get_fg_frame`

```python
def get_fg_frame(store: Store) -> FGFrame:
    finalized_node = ForkChoiceNode(
        root=store.finalized_checkpoint.root,
        payload_status=PAYLOAD_STATUS_PENDING,
    )
    # Share the finalized subtree across all readers of this frame.
    tree = get_subtree(store, finalized_node)
    return FGFrame(
        finalized_node=finalized_node,
        tree=tree,
        viable_heads={node for node in tree if is_fg_viable_block(store, node.root)},
    )
```

### New `get_viable_subtree`

```python
def get_viable_subtree(
    store: Store, fg_frame: FGFrame, tree: Sequence[ForkChoiceNode]
) -> Sequence[ForkChoiceNode]:
    """
    Return the nodes in ``tree`` with a viable head in that same domain.
    """
    viable_heads_in_tree = set(tree) & fg_frame.viable_heads
    return [
        node
        for node in tree
        if any(is_ancestor(store, viable_head, node) for viable_head in viable_heads_in_tree)
    ]
```

### New `is_fg_viable_block`

```python
def is_fg_viable_block(store: Store, block_root: Root) -> bool:
    """
    Return ``True`` if the block passes the FG height and justification test.
    """
    height = get_block_state(store, block_root).target_checkpoint.height
    if height >= store.max_height:
        return True
    return height + 1 == store.max_height and (
        store.justified_checkpoint.height < height
        or is_ancestor_root(store, block_root, store.justified_checkpoint.root)
    )
```

### New `has_viable_descendant`

```python
def has_viable_descendant(store: Store, block_root: Root) -> bool:
    """
    Return ``True`` if the block or any of its descendants passes the FG viability check.
    """
    return any(
        is_fg_viable_block(store, root)
        for root in store.blocks
        if is_ancestor_root(store, root, block_root)
    )
```

## Store updates

### New `update_finality`

*Notes*: First, update the store's maximum known height using the state's target
height.

Replace the store's justified checkpoint with the state's justified checkpoint
when both conditions hold:

- Its `(height, root)` tuple is lexicographically greater than the store's
  justified tuple.
- Its block descends from or equals the store's finalized block.

Then replace the store's finalized checkpoint with the state's finalized
checkpoint when all conditions hold:

- Its height is greater than the store's finalized height.
- Its block descends from or equals the store's finalized block.
- Its block is an ancestor of or equal to the store's justified block, including
  any justification update made above.
- Its block or one of its descendants passes the FG viability check.

```python
def update_finality(store: Store, state: BeaconState) -> None:
    """
    Update finality gadget information in store
    """
    # Update max_height
    store.max_height = max(store.max_height, state.target_checkpoint.height)

    # Update justification
    store_justification = (store.justified_checkpoint.height, store.justified_checkpoint.root)
    state_justification = (state.justified_checkpoint.height, state.justified_checkpoint.root)
    if state_justification > store_justification and is_ancestor_root(
        store, state.justified_checkpoint.root, store.finalized_checkpoint.root
    ):
        store.justified_checkpoint = state.justified_checkpoint

    # Update finality
    if (
        state.finalized_checkpoint.height > store.finalized_checkpoint.height
        and is_ancestor_root(
            store, state.finalized_checkpoint.root, store.finalized_checkpoint.root
        )
        and is_ancestor_root(
            store, store.justified_checkpoint.root, state.finalized_checkpoint.root
        )
        and has_viable_descendant(store, state.finalized_checkpoint.root)
    ):
        store.finalized_checkpoint = state.finalized_checkpoint
```

## Internal API

### New `get_ac_frame`

*Notes*: At or after the support cutoff, returns the FG confirmation node. It is
the deepest viable effective live fast-lock node whose vote slot is at least the
current slot minus two. Otherwise it is the deepest viable non-genesis ancestor
of the highest effective live lock (including that lock itself) accepted locally
by the start of the slot `DEEP_FG_SOURCE_SLOTS` ago. Time before joining,
restart, or fork activation does not count. With no such node, return `None`.

The AC frame is used by validators at scheduled FG duties. Each duty reads the
currently eligible source; publication is deduplicated by each validator's
persistent signing record rather than a node-level source watermark. A later
duty may therefore read a source equal to or ancestral to an earlier source.

```python
def get_ac_frame(store: Store) -> ForkChoiceNode | None:
    assert get_time_into_slot_ms(store) >= get_support_cutoff_ms()

    current_slot = get_current_slot(store)
    fg_frame = get_fg_frame(store)
    viable_subtree = get_viable_subtree(store, fg_frame, fg_frame.tree)
    live_locks = get_live_locks(store, fg_frame)
    candidates = {
        lock.node
        for lock in live_locks
        if lock.fast and current_slot <= lock.vote_slot + 2 and lock.node in viable_subtree
    }
    if candidates:
        return get_deepest_node(store, candidates)

    anchor = get_highest_live_lock_node(store, live_locks)
    if anchor is None or current_slot < DEEP_FG_SOURCE_SLOTS:
        return None
    cutoff = compute_time_at_slot_ms(store.genesis_time_ms, current_slot - DEEP_FG_SOURCE_SLOTS)
    if store.deep_since_ms > cutoff:
        return None
    deep = {
        node
        for node in viable_subtree
        if store.blocks[node.root].slot > GENESIS_SLOT
        and is_ancestor(store, anchor, node)
        and get_block_and_payload_receipt_time(store, node) <= cutoff
    }
    return get_deepest_node(store, deep) if deep else None
```
