"""Locks and confirmed tips on a small fork with real payload-status nodes."""

from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace

import pytest

from eth_consensus_specs.test import context
from eth_consensus_specs.test.helpers.keys import get_vrf_proof, get_vrf_root

pytestmark = pytest.mark.usefixtures("fake_xmss")


@pytest.fixture
def spec(preset, monkeypatch):
    spec = import_module(f"eth_consensus_specs.pq.{context.DEFAULT_TEST_PRESET}")
    monkeypatch.setattr(spec, "config", spec.config._replace(PQ_FORK_EPOCH=0))
    return spec


def at_slot(spec, slot):
    return spec.compute_time_at_slot_ms(spec.Uint64(0), spec.Slot(slot))


def finalized_subtree(spec, store):
    return spec.get_fg_frame(store).tree


@pytest.fixture
def tree(spec, monkeypatch):
    # Lock tests can specify exact ticket counts; authenticated votes use the sampler.
    compute_weight = spec.compute_ac_opening_weight

    def compute_opening_weight(state, pubkey, slot, vrf_root, secret):
        if vrf_root == spec.Root():
            return spec.bytes_to_uint64(secret[:8])
        return compute_weight(state, pubkey, slot, vrf_root, secret)

    monkeypatch.setattr(spec, "compute_ac_opening_weight", compute_opening_weight)
    # F -- P -- A -- A1
    #       \-- B -- B1
    blocks = {}
    states = {}
    nodes = {}
    for index, (name, parent, slot) in enumerate(
        [
            ("F", None, 0),
            ("P", "F", 1),
            ("A", "P", 2),
            ("B", "P", 3),
            ("A1", "A", 4),
            ("B1", "B", 5),
        ],
        start=1,
    ):
        root = spec.Root(index.to_bytes(32, "little"))
        block = spec.BeaconBlock(
            slot=slot, parent_root=nodes[parent].root if parent else spec.Root()
        )
        # Children build on EMPTY. Payloads are absent unless a test adds one.
        block.body.signed_execution_payload_bid.message.block_hash = root
        blocks[root] = block
        states[root] = spec.BeaconState(slot=11)
        nodes[name] = spec.ForkChoiceNode(root=root, payload_status=spec.PAYLOAD_STATUS_EMPTY)
    finalized = spec.HeightCheckpoint(height=0, root=nodes["F"].root)
    store = spec.Store(
        time_ms=at_slot(spec, 11) + spec.get_support_cutoff_ms(),
        genesis_time_ms=spec.Uint64(0),
        justified_checkpoint=finalized,
        finalized_checkpoint=finalized,
        proposer_boost_root=spec.Root(),
        blocks=blocks,
        block_states=states,
        payloads={},
        payload_timeliness_vote={},
        payload_data_availability_vote={},
        max_height=spec.Height(0),
        deep_since_ms=spec.Uint64(0),
        block_receipt_times=dict.fromkeys(blocks, spec.Uint64(0)),
        payload_receipt_times={},
        latest_confirmed=spec.ForkChoiceNode(
            root=nodes["F"].root, payload_status=spec.PAYLOAD_STATUS_PENDING
        ),
        saved_confirmations={},
        locks=set(),
        head_vote_messages={},
        head_vote_message_receipt_times={},
        validated_head_votes={},
    )
    return store, nodes


def lock(spec, node, slot=9, fast=True, expiry=13):
    return spec.ACLock(
        node=node, vote_slot=spec.Slot(slot), fast=fast, expiry_slot=spec.Slot(expiry)
    )


def vote(spec, store, node, validator, slot=10, received=None, weight=1):
    message = spec.HeadVoteMessage(
        validator_index=validator,
        data=spec.HeadVoteData(
            slot=slot, beacon_block_root=node.root, payload_status=node.payload_status
        ),
    )
    if slot not in store.head_vote_messages:
        store.head_vote_messages[slot] = []
    store.head_vote_messages[slot].append(message)
    root = spec.hash_tree_root(message)
    store.head_vote_message_receipt_times[root] = (
        at_slot(spec, slot) if received is None else received
    )
    store.validated_head_votes[root] = spec.ACVote(
        slot=message.data.slot,
        pubkey=spec.XMSSPubkey(validator.to_bytes(32, "little")),
        supported_node=node,
        ticket_openings=frozenset({(spec.Root(), spec.Bytes32(weight.to_bytes(32, "little")))}),
    )


def add_elected_validator(spec, state):
    state.validators.append(spec.Validator(pubkey=spec.xmss.SkToPk(1), vrf_root=get_vrf_root(0)))
    state.ac_snapshots[0] = spec.ACSnapshot(weights=spec.ACWeights(data=[1]))


def test_fast_expiry_grows_and_is_capped(spec, tree):
    store, nodes = tree
    expiries = []
    for slot in range(10, 43):
        store.time_ms = at_slot(spec, slot + 1) + spec.get_support_cutoff_ms()
        vote(spec, store, nodes["A"], 0, slot=slot, weight=spec.HEAD_VOTE_FAST_LOCK_THRESHOLD)
        spec.update_confirmation(store, spec.Slot(slot))
        current = [item for item in store.locks if item.vote_slot == slot and item.fast]
        assert len(current) == 1
        expiries.append(int(current[0].expiry_slot))
    assert expiries == list(range(12, 73, 2)) + [73, 74]


@pytest.mark.parametrize("expiry", [10, 11, 41])
def test_fast_expiry_matches_previous_rule_at_cutoff(spec, tree, expiry):
    store, nodes = tree
    slot = spec.Slot(10)
    store.time_ms = at_slot(spec, slot + 1) - 1
    store.locks = {lock(spec, nodes["A"], expiry=expiry)}
    vote(spec, store, nodes["A"], 0, slot=slot, weight=spec.HEAD_VOTE_FAST_LOCK_THRESHOLD)

    # At the cutoff, a lock expiring in the vote slot has already expired.
    previous_expiry = slot + 2
    if expiry > slot:
        previous_expiry = min(
            slot + spec.SLOW_LOCK_WINDOW, max(slot + 2, expiry + spec.FAST_LOCK_EXTENSION)
        )
    spec.on_tick(store, at_slot(spec, slot + 1) + spec.get_support_cutoff_ms())
    assert {item for item in store.locks if item.vote_slot == slot} == {
        lock(spec, nodes["A"], slot=slot, expiry=previous_expiry)
    }


def test_fast_expiry_paper_example_and_gap(spec, tree, monkeypatch):
    store, nodes = tree
    monkeypatch.setattr(spec, "SLOW_LOCK_WINDOW", spec.Slot(6))
    expiries = []
    for slot in [10, 11, 12, 13, 14, 16]:
        store.time_ms = at_slot(spec, slot + 1) + spec.get_support_cutoff_ms()
        vote(spec, store, nodes["A"], 0, slot=slot, weight=spec.HEAD_VOTE_FAST_LOCK_THRESHOLD)
        spec.update_confirmation(store, spec.Slot(slot))
        current = [item for item in store.locks if item.vote_slot == slot and item.fast]
        assert len(current) == 1
        expiries.append(int(current[0].expiry_slot))
    assert expiries == [12, 14, 16, 18, 20, 22]


@pytest.mark.parametrize("kind", ["slow", "expired", "outside_frame", "unverified_full"])
def test_fast_expiry_ignores_ineligible_locks(spec, tree, kind):
    store, nodes = tree
    existing = lock(spec, nodes["A"])
    if kind == "slow":
        existing = replace(existing, fast=False)
    elif kind == "expired":
        existing = replace(existing, expiry_slot=spec.Slot(10))
    elif kind == "unverified_full":
        existing = replace(
            existing, node=replace(nodes["A"], payload_status=spec.PAYLOAD_STATUS_FULL)
        )
    else:
        existing = replace(existing, node=replace(nodes["A"], root=spec.Root(b"\xff" * 32)))
    store.locks = {existing}
    assert (
        spec.compute_fast_lock_expiry(
            spec.get_live_locks(store, spec.get_fg_frame(store)),
            spec.Slot(10),
        )
        == 12
    )


def test_fast_expiry_counts_lock_moved_to_finalized(spec, tree):
    store, _ = tree
    frame = spec.get_fg_frame(store)
    store.locks = {lock(spec, frame.finalized_node, expiry=12)}
    assert (
        spec.compute_fast_lock_expiry(
            spec.get_live_locks(store, frame),
            spec.Slot(10),
        )
        == 14
    )


def test_fast_and_slow_locks_with_same_node_and_expiry(spec, tree, monkeypatch):
    monkeypatch.setattr(spec, "SLOW_LOCK_WINDOW", spec.Slot(4))
    store, nodes = tree
    store.saved_confirmations = dict.fromkeys(range(7, 10), nodes["A"])
    store.locks = {lock(spec, nodes["A"], expiry=12)}
    vote(spec, store, nodes["A"], 0, weight=spec.HEAD_VOTE_FAST_LOCK_THRESHOLD)
    spec.update_confirmation(store, spec.Slot(10))
    new = {item for item in store.locks if item.vote_slot == 10}
    assert new == {
        lock(spec, nodes["A"], slot=10, fast=True, expiry=14),
        lock(spec, nodes["A"], slot=10, fast=False, expiry=14),
    }
    assert store.latest_confirmed == nodes["A"]


def test_fast_and_slow_locks_can_have_different_nodes(spec, tree, monkeypatch):
    monkeypatch.setattr(spec, "SLOW_LOCK_WINDOW", spec.Slot(4))
    store, nodes = tree
    store.saved_confirmations = dict.fromkeys(range(7, 10), nodes["P"])
    vote(spec, store, nodes["A"], 0, weight=spec.HEAD_VOTE_FAST_LOCK_THRESHOLD)
    spec.update_confirmation(store, spec.Slot(10))
    assert store.locks == {
        lock(spec, nodes["A"], slot=10, fast=True, expiry=12),
        lock(spec, nodes["P"], slot=10, fast=False, expiry=14),
    }


def test_veto_records_candidate_and_locks_shared_ancestor(spec, tree, monkeypatch):
    monkeypatch.setattr(spec, "SLOW_LOCK_WINDOW", spec.Slot(4))
    store, nodes = tree
    store.latest_confirmed = nodes["A"]
    existing = lock(spec, nodes["A"])
    store.locks = {existing}
    store.saved_confirmations = dict.fromkeys(range(7, 10), nodes["B"])
    vote(spec, store, nodes["B"], 0, weight=spec.HEAD_VOTE_FAST_LOCK_THRESHOLD)
    spec.update_confirmation(store, spec.Slot(10))
    assert store.saved_confirmations[10] == nodes["B"]
    assert store.latest_confirmed == nodes["A"]
    assert store.locks == {
        existing,
        lock(spec, nodes["P"], slot=10, fast=True, expiry=14),
        lock(spec, nodes["P"], slot=10, fast=False, expiry=14),
    }


@pytest.mark.parametrize("missing", ["absent", "removed"])
def test_missing_candidate_breaks_slow_streak(spec, tree, missing):
    store, nodes = tree
    store.saved_confirmations = dict.fromkeys(range(10, 41), nodes["A"])
    assert (
        spec.compute_slow_lock(
            store, finalized_subtree(spec, store), None, spec.Slot(41), nodes["A"]
        )
        == nodes["A"]
    )
    if missing == "absent":
        del store.saved_confirmations[10]
    else:
        store.saved_confirmations[10] = replace(nodes["A"], root=spec.Root(b"\xff" * 32))
    assert (
        spec.compute_slow_lock(
            store, finalized_subtree(spec, store), None, spec.Slot(41), nodes["A"]
        )
        is None
    )


def test_no_candidate_clears_saved_slot(spec, tree):
    store, nodes = tree
    store.saved_confirmations[10] = nodes["A"]
    spec.update_confirmation(store, spec.Slot(10))
    assert 10 not in store.saved_confirmations


def test_expiry_at_slot_start(spec, tree):
    store, nodes = tree
    store.locks = {lock(spec, nodes["A"], expiry=12)}
    frame = spec.get_fg_frame(store)
    boundary = at_slot(spec, 13)
    store.time_ms = boundary - 1
    assert len(spec.get_live_locks(store, frame)) == 1
    store.time_ms = boundary
    assert not spec.get_live_locks(store, frame)
    store.time_ms = boundary + 1
    assert not spec.get_live_locks(store, frame)


def test_live_locks_move_to_viable_ancestor_and_stay_there(spec, tree):
    store, nodes = tree
    original = lock(spec, nodes["A"])
    store.locks = {original}
    full_frame = spec.get_fg_frame(store)
    frame = replace(full_frame, viable_heads={nodes["B"]})
    moved = replace(original, node=nodes["P"])
    assert spec.get_live_locks(store, frame) == {moved}
    assert spec.get_head_walk_anchor(store, frame) == nodes["P"]
    assert not spec.conflicts_with_lock(store, moved.node, nodes["B"])
    # Even an empty lock update must store the moved node.
    spec.add_locks(store, frame, set())
    assert spec.get_live_locks(store, full_frame) == {moved}


@pytest.mark.parametrize("justified", ["F", "A"])
def test_fg_viability_at_frontier(spec, tree, justified):
    store, nodes = tree
    store.max_height = spec.Height(2)
    store.justified_checkpoint = spec.HeightCheckpoint(
        height=int(justified == "A"), root=nodes[justified].root
    )
    for name, height in [("A", 1), ("B", 1), ("A1", 2), ("B1", 2)]:
        store.block_states[nodes[name].root].target_checkpoint.height = spec.Height(height)

    frame = spec.get_fg_frame(store)
    assert spec.get_head_walk_anchor(store, frame) == frame.finalized_node
    expected_heads = {nodes[name].root for name in ("A", "A1", "B1")}
    if justified == "F":
        expected_heads.add(nodes["B"].root)
    assert {node.root for node in frame.viable_heads} == expected_heads

    # A restricted walk cannot use a frontier block outside its domain to make B viable.
    restricted = [node for node in frame.tree if node.root != nodes["B1"].root]
    viable = spec.get_viable_subtree(store, frame, restricted)
    assert nodes["P"] in viable
    assert (nodes["B"] in viable) == (justified == "F")

    # With only A's branch at the frontier, finality's guard uses the same rule.
    store.block_states[nodes["B1"].root].target_checkpoint.height = spec.Height(1)
    assert spec.has_viable_descendant(store, nodes["B"].root) == (justified == "F")
    assert spec.get_head_walk_anchor(store, spec.get_fg_frame(store)) == frame.finalized_node


@pytest.mark.parametrize("branch", ["A", "B"])
def test_highest_live_lock_matches_chain_veto_and_anchor(spec, tree, branch):
    store, nodes = tree
    pending = replace(nodes[branch], payload_status=spec.PAYLOAD_STATUS_PENDING)
    store.locks = {lock(spec, nodes["P"], vote_slot, fast=False) for vote_slot in (7, 8)} | {
        lock(spec, pending, 9),
        lock(spec, nodes[branch], 10),
    }
    frame = spec.get_fg_frame(store)
    live_locks = spec.get_live_locks(store, frame)
    highest = spec.get_highest_live_lock_node(store, live_locks)
    assert highest == nodes[branch]
    for node in frame.tree:
        old_veto = any(not spec.is_compatible(store, item.node, node) for item in live_locks)
        assert spec.conflicts_with_lock(store, highest, node) == old_veto

    assert spec.get_head_walk_anchor(store, frame) == highest


def test_unlock_at_threshold_moves_to_common_ancestor_preserving_fields(spec, tree):
    store, nodes = tree
    original = lock(spec, nodes["A"], fast=False)
    store.locks = {original}
    for validator in range(spec.HEAD_VOTE_UNLOCK_THRESHOLD):
        vote(spec, store, nodes["B"], validator)
    spec.unlock(
        store,
        spec.get_fg_frame(store),
        spec.Slot(10),
        *spec.get_head_votes_by_cutoff(store, spec.Slot(10), spec.INFINITE_TIME_MS),
    )
    assert store.locks == {replace(original, node=nodes["P"])}


def test_unlock_below_threshold_keeps_lock(spec, tree):
    store, nodes = tree
    original = lock(spec, nodes["A"])
    store.locks = {original}
    for validator in range(spec.HEAD_VOTE_UNLOCK_THRESHOLD - 1):
        vote(spec, store, nodes["B"], validator)
    spec.unlock(
        store,
        spec.get_fg_frame(store),
        spec.Slot(10),
        *spec.get_head_votes_by_cutoff(store, spec.Slot(10), spec.INFINITE_TIME_MS),
    )
    assert store.locks == {original}


def test_unlock_equivocators_count_without_resolved_votes(spec, tree):
    store, nodes = tree
    original = lock(spec, nodes["A"])
    store.locks = {original}
    unknown_a = replace(nodes["A"], root=spec.Root(b"\xfe" * 32))
    unknown_b = replace(nodes["B"], root=spec.Root(b"\xff" * 32))
    for validator in range(spec.HEAD_VOTE_UNLOCK_THRESHOLD):
        vote(spec, store, unknown_a, validator)
        vote(spec, store, unknown_b, validator)
    spec.unlock(
        store,
        spec.get_fg_frame(store),
        spec.Slot(10),
        *spec.get_head_votes_by_cutoff(store, spec.Slot(10), spec.INFINITE_TIME_MS),
    )
    # Equivocating weight at the threshold opposes every ancestor, including F.
    assert store.locks == {replace(original, node=spec.get_fg_frame(store).finalized_node)}


def test_unlock_equivocator_not_counted_twice(spec, tree):
    store, nodes = tree
    original = lock(spec, nodes["A"])
    store.locks = {original}
    # Counting the equivocator twice would reach the unlock threshold.
    vote(spec, store, nodes["A"], 0)
    vote(spec, store, nodes["B"], 0)
    for validator in range(1, spec.HEAD_VOTE_UNLOCK_THRESHOLD - 1):
        vote(spec, store, nodes["B"], validator)
    spec.unlock(
        store,
        spec.get_fg_frame(store),
        spec.Slot(10),
        *spec.get_head_votes_by_cutoff(store, spec.Slot(10), spec.INFINITE_TIME_MS),
    )
    assert store.locks == {original}


def test_unlock_counts_duplicate_committee_seats(spec, tree):
    store, nodes = tree
    original = lock(spec, nodes["A"])
    store.locks = {original}
    vote(spec, store, nodes["B"], 0, weight=spec.HEAD_VOTE_UNLOCK_THRESHOLD)
    spec.unlock(
        store,
        spec.get_fg_frame(store),
        spec.Slot(10),
        *spec.get_head_votes_by_cutoff(store, spec.Slot(10), spec.INFINITE_TIME_MS),
    )
    assert store.locks == {replace(original, node=nodes["P"])}


def test_unlock_full_empty_conflict_moves_to_pending(spec, tree):
    store, nodes = tree
    original = lock(spec, nodes["A"])
    store.locks = {original}
    store.payloads[nodes["A"].root] = spec.ExecutionPayloadEnvelope()
    store.payload_receipt_times[nodes["A"].root] = spec.Uint64(0)
    full = replace(nodes["A"], payload_status=spec.PAYLOAD_STATUS_FULL)
    vote(spec, store, full, 0, weight=spec.HEAD_VOTE_UNLOCK_THRESHOLD)
    spec.unlock(
        store,
        spec.get_fg_frame(store),
        spec.Slot(10),
        *spec.get_head_votes_by_cutoff(store, spec.Slot(10), spec.INFINITE_TIME_MS),
    )
    pending = replace(nodes["A"], payload_status=spec.PAYLOAD_STATUS_PENDING)
    assert store.locks == {replace(original, node=pending)}


def test_unlock_removes_expired_locks(spec, tree):
    store, nodes = tree
    store.locks = {lock(spec, nodes["A"], expiry=10)}
    spec.unlock(
        store,
        spec.get_fg_frame(store),
        spec.Slot(10),
        *spec.get_head_votes_by_cutoff(store, spec.Slot(10), spec.INFINITE_TIME_MS),
    )
    assert not store.locks


def test_confirmation_tick_uses_current_time_for_lock_expiry(spec, tree):
    store, nodes = tree
    store.latest_confirmed = nodes["A"]
    store.locks = {lock(spec, nodes["A"], slot=8, expiry=10)}
    store.time_ms = at_slot(spec, 11) - 1
    vote(spec, store, nodes["B"], 0, weight=spec.HEAD_VOTE_FAST_LOCK_THRESHOLD)
    tick_time = at_slot(spec, 11) + spec.get_support_cutoff_ms()
    spec.on_tick(store, tick_time)
    assert store.time_ms == tick_time
    assert store.latest_confirmed == nodes["B"]
    assert store.locks == {lock(spec, nodes["B"], slot=10, expiry=12)}


def test_equivocators_do_not_supply_fast_lock_support(spec, tree):
    store, nodes = tree
    vote(spec, store, nodes["A"], 0)
    vote(spec, store, nodes["B"], 0)
    for validator in range(1, spec.HEAD_VOTE_FAST_LOCK_THRESHOLD):
        vote(spec, store, nodes["A"], validator)
    spec.update_confirmation(store, spec.Slot(10))
    # The clean votes suffice for relative confirmation but not a fast lock.
    assert store.latest_confirmed == nodes["A"]
    assert not store.locks


@pytest.mark.parametrize(("clean_weight", "expected"), [(5, None), (6, "A1"), (0, "finalized")])
def test_confirmed_head_matches_majority_walk(spec, tree, clean_weight, expected):
    store, nodes = tree
    if expected == "finalized":
        store.finalized_checkpoint = spec.HeightCheckpoint(height=0, root=nodes["A1"].root)
        store.justified_checkpoint = store.finalized_checkpoint.copy()
    finalized = spec.get_fg_frame(store).finalized_node
    subtree = finalized_subtree(spec, store)
    if expected == "finalized":
        slot = store.blocks[finalized.root].slot
        vote(spec, store, finalized, 0, slot=slot)
    else:
        slot = spec.Slot(10)
        vote(spec, store, nodes["A1"], 0, weight=clean_weight)
        vote(spec, store, nodes["A1"], 1, weight=2)
        vote(spec, store, nodes["B1"], 1, weight=4)
        vote(spec, store, nodes["B1"], 2, weight=1)
    votes, support = spec.get_head_votes_by_cutoff(store, slot, spec.INFINITE_TIME_MS)
    support = {v for v in support if v.supported_node != nodes["B1"]}
    equivocators = spec.get_equivocating_pubkeys(votes)
    total = spec.sum_head_vote_weights(store, votes, slot)

    def clean_support(node):
        return {v for v in support if spec.is_ancestor(store, v.supported_node, node)}

    def score(node):
        return spec.sum_head_vote_weights(
            store, {v for v in clean_support(node) if v.pubkey not in equivocators}, slot
        )

    def has_majority(node):
        return 2 * score(node) > total

    old_head = spec.compute_ghost_head(store, finalized, subtree, score, has_majority)
    old_head = old_head if has_majority(old_head) else None
    expected_head = finalized if expected == "finalized" else nodes.get(expected)
    assert (
        spec.get_confirmed_head(store, subtree, votes, support, slot) == old_head == expected_head
    )

    equivocations = {v for v in votes if v.pubkey in equivocators}
    for node in subtree:
        old_score = spec.sum_head_vote_weights(store, equivocations | clean_support(node), slot)
        assert spec.compute_head_walk_score(store, votes, support, slot, node) == old_score


@pytest.mark.parametrize(
    ("confirmed_name", "head_name", "expected_name"),
    [("A1", "A", "A"), ("A", "A1", "A"), ("A1", "B1", "P")],
)
def test_confirmed_tip_common_ancestor(spec, tree, confirmed_name, head_name, expected_name):
    store, nodes = tree
    store.latest_confirmed = nodes[confirmed_name]
    vote(spec, store, nodes[head_name], 0, slot=11)
    assert spec.get_head(store) == nodes[head_name]
    assert spec.get_confirmed_node(store) == nodes[expected_name]
    assert store.latest_confirmed == nodes[confirmed_name]


@pytest.mark.parametrize(
    ("confirmed_status", "head_status"),
    [("FULL", "EMPTY"), ("EMPTY", "FULL"), ("PENDING", "FULL")],
)
def test_confirmed_tip_payload_status(spec, tree, confirmed_status, head_status):
    store, nodes = tree
    store.payloads[nodes["A"].root] = spec.ExecutionPayloadEnvelope()
    store.payload_receipt_times[nodes["A"].root] = spec.Uint64(0)
    store.latest_confirmed = replace(
        nodes["A"], payload_status=getattr(spec, f"PAYLOAD_STATUS_{confirmed_status}")
    )
    head = replace(nodes["A"], payload_status=getattr(spec, f"PAYLOAD_STATUS_{head_status}"))
    vote(spec, store, head, 0, slot=11)
    assert spec.get_head(store) == head
    assert spec.get_confirmed_node(store) == replace(
        nodes["A"], payload_status=spec.PAYLOAD_STATUS_PENDING
    )


def test_confirmed_tip_outside_frame(spec, tree):
    store, nodes = tree
    store.latest_confirmed = replace(nodes["A"], root=spec.Root(b"\xff" * 32))
    vote(spec, store, nodes["B1"], 0, slot=11)
    assert spec.get_head(store) == nodes["B1"]
    assert spec.get_confirmed_node(store) == spec.get_fg_frame(store).finalized_node


@pytest.mark.parametrize("receipt_offset", [-1, 0, 1])
@pytest.mark.parametrize("with_payload", [False, True])
def test_ac_vote_head_viable_tree_fallback(spec, tree, receipt_offset, with_payload):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11) + spec.get_head_vote_due_ms()
    freeze = at_slot(spec, 10) + spec.get_view_freeze_ms()
    store.max_height = spec.Height(2)
    # Only the two leaves pass the height floor.
    for name in ["A1", "B1"]:
        store.block_states[nodes[name].root].target_checkpoint.height = spec.Height(2)
    store.block_receipt_times[nodes["A1"].root] = freeze + receipt_offset
    store.block_receipt_times[nodes["B1"].root] = freeze
    if with_payload:
        for name in ["A1", "B1"]:
            store.payloads[nodes[name].root] = spec.ExecutionPayloadEnvelope()
            store.payload_receipt_times[nodes[name].root] = spec.Uint64(0)

    frame = spec.get_fg_frame(store)
    restricted = spec.get_voter_subtree(
        store, spec.get_subtree(store, frame.finalized_node), spec.Slot(11)
    )
    # The restricted tree is nonempty even when none of its nodes is viable.
    assert restricted
    assert bool(spec.get_viable_subtree(store, frame, restricted)) == (receipt_offset < 0)
    expected = nodes["A1"] if receipt_offset < 0 else nodes["B1"]
    if with_payload:
        expected = replace(expected, payload_status=spec.PAYLOAD_STATUS_FULL)
    assert spec.get_ac_vote_head(store) == expected


def test_ac_vote_head_keeps_payload_restriction(spec, tree):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11) + spec.get_head_vote_due_ms()
    freeze = at_slot(spec, 10) + spec.get_view_freeze_ms()
    store.max_height = spec.Height(2)
    store.block_states[nodes["A1"].root].target_checkpoint.height = spec.Height(2)
    store.payloads[nodes["A1"].root] = spec.ExecutionPayloadEnvelope()
    store.payload_receipt_times[nodes["A1"].root] = freeze
    full = replace(nodes["A1"], payload_status=spec.PAYLOAD_STATUS_FULL)
    frame = spec.get_fg_frame(store)
    restricted = spec.get_voter_subtree(
        store, spec.get_subtree(store, frame.finalized_node), spec.Slot(11)
    )
    assert full in frame.viable_heads
    assert full not in restricted
    assert nodes["A1"] in spec.get_viable_subtree(store, frame, restricted)
    assert spec.get_ac_vote_head(store) == nodes["A1"]


@pytest.mark.parametrize("late", [False, True])
def test_proposal_unlock_does_not_change_vote_locks(spec, tree, late):
    store, nodes = tree
    original = lock(spec, nodes["A"])
    store.locks = {original}
    received = at_slot(spec, 10) + spec.get_view_freeze_ms() if late else None
    for validator in range(spec.HEAD_VOTE_UNLOCK_THRESHOLD):
        vote(spec, store, nodes["B"], validator, received=received)
    store.time_ms = at_slot(spec, 11)
    non_proposer = replace(store)

    head, _, _ = spec.get_ac_proposal(store)
    assert head == nodes["B"]
    assert store.locks == non_proposer.locks == {original}

    for voter in (store, non_proposer):
        spec.on_tick(voter, voter.time_ms + spec.get_head_vote_due_ms())
        assert spec.get_ac_vote_head(voter) == nodes["A" if late else "B"]
        assert voter.locks == {original if late else replace(original, node=nodes["P"])}


def test_proposal_declaration_cap_prioritizes_weighted_voters(spec, tree):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11)
    limit = 4 * spec.HEAD_VOTE_COMMITTEE_SIZE
    unresolved = spec.ForkChoiceNode(root=nodes["B"].root, payload_status=spec.PAYLOAD_STATUS_FULL)
    for index in range(limit):
        vote(spec, store, nodes["A"], index, weight=0)
    vote(spec, store, unresolved, limit, weight=0)
    vote(spec, store, nodes["B1"], limit, weight=5)
    vote(spec, store, nodes["B1"], limit + 1, weight=3)

    head, declarations, bits = spec.get_ac_proposal(store)

    assert head == nodes["B1"]
    assert len(declarations) == len(bits) == limit
    assert [item.validator_index for item in declarations] == [
        limit,
        limit,
        limit + 1,
        *range(limit - 3),
    ]
    assert [item.beacon_block_root for item in declarations[:2]] == [unresolved.root, head.root]
    assert not bits[0]
    assert all(bits[1:])


@pytest.mark.parametrize("at_deadline", [False, True])
def test_voter_unlock_uses_proposer_support_once(spec, tree, at_deadline):
    store, nodes = tree
    original = lock(spec, nodes["A"])
    store.locks = {original}
    proposal = store.blocks[nodes["B1"].root]
    proposal.slot = spec.Slot(11)
    indices = list(range(spec.HEAD_VOTE_UNLOCK_THRESHOLD))
    propose_votes(spec, store, nodes, [(nodes["B"], indices, True)])
    store.time_ms = at_slot(spec, 11)
    if at_deadline:
        # The last seat holds only B. Counting its unheld declaration for A
        # would mark it as equivocating; dropping the whole proposal would
        # also lose the support needed to reach the unlock threshold.
        proposal.body.head_vote_declarations.append(
            spec.HeadVoteDeclaration(
                validator_index=indices[-1],
                beacon_block_root=nodes["A"].root,
                payload_status=nodes["A"].payload_status,
            )
        )
        proposal.body.head_vote_support_bits.append(True)
        store.time_ms += spec.get_head_vote_due_ms() - 1
        assert spec.get_ac_vote_head(store) is None
        store.time_ms += 1
        cast, support, _ = spec.get_declared_votes_and_readiness(
            store, finalized_subtree(spec, store), spec.Slot(11)
        )
        assert cast == support
        assert {v.supported_node for v in cast} == {nodes["B"]}
    head = spec.get_ac_vote_head(store)
    assert head == replace(nodes["B1"], payload_status=spec.PAYLOAD_STATUS_PENDING)
    assert store.locks == {replace(original, node=nodes["P"])}
    # Subsequent evidence cannot change this slot's prepared vote or unlock again.
    proposal.body.head_vote_declarations[0].beacon_block_root = nodes["A"].root
    assert spec.get_ac_vote_head(store) == head
    assert store.locks == {replace(original, node=nodes["P"])}


def propose_votes(spec, store, nodes, votes):
    proposal = store.blocks[nodes["B1"].root]
    proposal.slot = spec.Slot(11)
    for node, voters, supported in votes:
        for voter in voters:
            proposal.body.head_vote_declarations.append(
                spec.HeadVoteDeclaration(
                    validator_index=voter,
                    beacon_block_root=node.root,
                    payload_status=node.payload_status,
                )
            )
            proposal.body.head_vote_support_bits.append(supported)
            vote(spec, store, node, voter, received=at_slot(spec, 11))


def merged_voters(spec, view):
    votes, support = view[:2]
    return {int.from_bytes(v.pubkey, "little") for v in votes}, {
        int.from_bytes(v.pubkey, "little") for v in support
    }


def test_proposer_support_needs_known_verified_head(spec, tree):
    store, nodes = tree
    unknown = spec.ForkChoiceNode(
        root=spec.Root(b"\x99" * 32), payload_status=spec.PAYLOAD_STATUS_EMPTY
    )
    full = replace(nodes["A"], payload_status=spec.PAYLOAD_STATUS_FULL)
    propose_votes(
        spec,
        store,
        nodes,
        [(unknown, [0, 1], True), (full, [2, 3], True), (nodes["B"], [4, 5], True)],
    )
    view = spec.get_declared_votes_and_readiness(
        store, finalized_subtree(spec, store), spec.Slot(11)
    )
    assert merged_voters(spec, view) == ({0, 1, 2, 3, 4, 5}, {4, 5})
    # Verifying the payload lets the proposer's support count for the full vote
    store.payloads[nodes["A"].root] = spec.ExecutionPayloadEnvelope()
    view = spec.get_declared_votes_and_readiness(
        store, finalized_subtree(spec, store), spec.Slot(11)
    )
    assert merged_voters(spec, view) == ({0, 1, 2, 3, 4, 5}, {2, 3, 4, 5})


def test_declarations_keep_individual_support_bits(spec, tree):
    store, nodes = tree
    propose_votes(
        spec, store, nodes, [(nodes["B"], [0, 1, 2], True), (nodes["B"], [3, 4, 5], False)]
    )
    view = spec.get_declared_votes_and_readiness(
        store, finalized_subtree(spec, store), spec.Slot(11)
    )
    assert merged_voters(spec, view) == ({0, 1, 2, 3, 4, 5}, {0, 1, 2})


def test_pair_backed_declaration_matches_proposer_support(spec, tree, monkeypatch):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11)
    propose_votes(spec, store, nodes, [(nodes["B"], [0], True)])
    store.block_states[nodes["B"].root].validators.append(spec.Validator())
    subtree = finalized_subtree(spec, store)
    proposer_view = spec.get_declared_votes_and_readiness(store, subtree, spec.Slot(11))
    store.head_vote_messages.clear()
    store.head_vote_message_receipt_times.clear()
    store.validated_head_votes.clear()
    freeze = at_slot(spec, 10) + spec.get_view_freeze_ms()
    vote(spec, store, nodes["P"], 0, received=freeze, weight=3)
    assert spec.get_ac_vote_head(store) is None
    vote(spec, store, nodes["A"], 0, received=freeze, weight=2)
    messages = list(store.head_vote_messages[10])
    commitments = [spec.Root(bytes([i]) * 32) for i in (1, 2, 3)]
    for message, commitment, weight in zip(messages, commitments[:2], (5, 2), strict=True):
        root = spec.hash_tree_root(message)
        store.validated_head_votes[root] = replace(
            store.validated_head_votes[root],
            ticket_openings=frozenset({(commitment, spec.Bytes32(weight.to_bytes(32, "little")))}),
        )
    tally = spec.get_ac_tally_state(store)
    tally.validators.append(spec.Validator(vrf_root=commitments[0]))
    monkeypatch.setattr(
        spec,
        "compute_ac_ticket_weight",
        lambda snapshots, dynasty, slot, index, secret: spec.bytes_to_uint64(secret[:8]),
    )
    receipts = dict(store.head_vote_message_receipt_times)
    validated = dict(store.validated_head_votes)
    assert spec.get_ac_vote_head(store) is not None
    votes, support, ready = spec.get_declared_votes_and_readiness(store, subtree, spec.Slot(11))
    assert ready
    local_view = spec.get_head_vote_messages_by_cutoff(store, spec.Slot(10), freeze, freeze)
    merged, support = spec.compute_merged_view(store, local_view, (votes, support))
    assert (merged, support) == proposer_view[:2]
    assert not spec.get_equivocating_pubkeys(merged)
    assert {v.supported_node for v in support} == {nodes["B"]}
    for commitment, expected in zip(commitments, (5, 2, 0), strict=True):
        tally.validators[0].vrf_root = commitment
        assert (
            spec.compute_head_walk_score(store, merged, support, spec.Slot(10), nodes["B"])
            == expected
        )
        assert spec.compute_head_walk_score(store, merged, support, spec.Slot(10), nodes["A"]) == 0
    assert store.head_vote_messages == {10: messages}
    assert store.head_vote_message_receipt_times == receipts
    assert store.validated_head_votes == validated
    assert not store.pending_head_votes
    _, declarations, bits = spec.get_ac_proposal(store)
    assert [d.beacon_block_root for d in declarations] == [nodes["P"].root, nodes["A"].root]
    assert [bool(bit) for bit in bits] == [True, True]


def test_pair_backed_marked_support_waits_for_payload(spec, tree):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11)
    full = replace(nodes["B"], payload_status=spec.PAYLOAD_STATUS_FULL)
    propose_votes(spec, store, nodes, [(full, [0], True)])
    store.block_states[nodes["B"].root].validators.append(spec.Validator())
    store.head_vote_messages.clear()
    store.head_vote_message_receipt_times.clear()
    store.validated_head_votes.clear()
    vote(spec, store, nodes["P"], 0)
    vote(spec, store, nodes["A"], 0)
    votes, support, ready = spec.get_declared_votes_and_readiness(
        store, finalized_subtree(spec, store), spec.Slot(11)
    )
    assert {v.supported_node for v in votes} == {full}
    assert not support
    assert not ready
    assert spec.get_ac_vote_head(store) is None

    store.payloads[full.root] = spec.ExecutionPayloadEnvelope()
    store.payload_receipt_times[full.root] = store.time_ms
    votes, support, ready = spec.get_declared_votes_and_readiness(
        store, finalized_subtree(spec, store), spec.Slot(11)
    )
    assert {v.supported_node for v in support} == {full}
    assert ready


@pytest.mark.parametrize("head_slot", [10, 12])
def test_pair_backed_marked_support_checks_declared_head_slot(spec, tree, head_slot):
    store, nodes = tree
    head = nodes["A1"]
    store.blocks[head.root].slot = spec.Slot(head_slot)
    store.block_states[head.root].validators.append(spec.Validator())
    proposal = store.blocks[nodes["B1"].root]
    proposal.slot = spec.Slot(11)
    proposal.body.head_vote_declarations.append(
        spec.HeadVoteDeclaration(
            validator_index=0, beacon_block_root=head.root, payload_status=head.payload_status
        )
    )
    proposal.body.head_vote_support_bits.append(True)
    vote(spec, store, nodes["P"], 0)
    vote(spec, store, nodes["A"], 0)

    subtree = finalized_subtree(spec, store)
    assert head in subtree
    votes, support, ready = spec.get_declared_votes_and_readiness(store, subtree, spec.Slot(11))
    assert {v.supported_node for v in votes} == {head}
    assert not support
    assert not ready


def test_proposer_support_follows_bits_when_both_resolve_locally(spec, tree):
    store, nodes = tree
    propose_votes(spec, store, nodes, [(nodes["A"], [0, 1], False), (nodes["B"], [2, 3], True)])
    proposer_view = spec.get_declared_votes_and_readiness(
        store, finalized_subtree(spec, store), spec.Slot(11)
    )
    assert merged_voters(spec, proposer_view) == ({0, 1, 2, 3}, {2, 3})
    infinite = spec.INFINITE_TIME_MS
    local_view = spec.get_head_votes_by_cutoff(store, spec.Slot(10), infinite)
    assert merged_voters(spec, local_view) == ({0, 1, 2, 3}, {0, 1, 2, 3})


def test_support_index_addresses_filtered_messages(spec, tree):
    store, nodes = tree
    cutoff = at_slot(spec, 11)
    vote(spec, store, nodes["A"], 0, received=cutoff)
    vote(spec, store, nodes["B"], 1, received=cutoff - 1)
    messages, support = spec.get_head_vote_messages_by_cutoff(store, spec.Slot(10), cutoff, cutoff)
    assert [m.validator_index for m in messages] == [1]
    assert support == [0]


def test_unheld_declaration_delays_ready_sibling(spec, tree):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11)
    propose_votes(spec, store, nodes, [(nodes["B"], [0], True)])
    sibling = store.blocks[nodes["A1"].root]
    sibling.slot = spec.Slot(11)
    sibling.body.head_vote_declarations.append(
        spec.HeadVoteDeclaration(
            validator_index=spec.HEAD_VOTE_COMMITTEE_SIZE, beacon_block_root=spec.Root(b"\x99" * 32)
        )
    )
    sibling.body.head_vote_support_bits.append(True)
    store.block_states[nodes["A1"].root] = store.block_states[nodes["B1"].root].copy()
    votes, support, ready = spec.get_declared_votes_and_readiness(
        store, finalized_subtree(spec, store), spec.Slot(11)
    )
    assert not ready
    assert merged_voters(spec, (votes, support)) == ({0}, {0})


@pytest.mark.parametrize(
    ("age", "fast", "expiry", "expected"),
    [
        (2, True, 13, "A"),
        (3, True, 40, None),
        (1, False, 40, None),
        (1, True, 10, None),
    ],
)
def test_fg_source_requires_recent_live_fast_lock(spec, tree, age, fast, expiry, expected):
    store, nodes = tree
    store.locks = {lock(spec, nodes["A"], slot=11 - age, fast=fast, expiry=expiry)}
    spec.update_confirmation(store, spec.Slot(10))
    assert spec.get_ac_frame(store) == (nodes[expected] if expected else None)


def test_fg_source_uses_viable_lock_ancestor(spec, tree):
    store, nodes = tree
    store.locks = {lock(spec, nodes["A1"], slot=10)}
    store.max_height = spec.Height(2)
    store.block_states[nodes["B1"].root].target_checkpoint.height = spec.Height(2)
    assert spec.get_ac_frame(store) == nodes["P"]


def test_deep_fg_source_ages_from_local_acceptance(spec, tree):
    store, nodes = tree
    slot = spec.DEEP_FG_SOURCE_SLOTS + 8
    store.locks = {lock(spec, nodes["A"], slot=slot - 1, fast=False, expiry=slot + 2)}
    store.block_receipt_times[nodes["P"].root] = at_slot(spec, 8)
    store.block_receipt_times[nodes["A"].root] = at_slot(spec, 8) + 1
    for offset, expected in [(-1, None), (0, nodes["P"]), (1, nodes["A"])]:
        store.time_ms = at_slot(spec, int(slot) + offset) + spec.get_support_cutoff_ms()
        assert spec.get_ac_frame(store) == expected
    store.deep_since_ms = store.time_ms  # A restart invalidates both old receipts.
    assert spec.get_ac_frame(store) is None


def test_duplicate_envelope_preserves_deep_full_source(spec, tree, monkeypatch):
    store, nodes = tree
    full = replace(nodes["A"], payload_status=spec.PAYLOAD_STATUS_FULL)
    pending = replace(full, payload_status=spec.PAYLOAD_STATUS_PENDING)
    envelope = spec.SignedExecutionPayloadEnvelope(
        message=spec.ExecutionPayloadEnvelope(beacon_block_root=full.root)
    )
    monkeypatch.setattr(spec, "is_data_available", lambda root: True)
    monkeypatch.setattr(spec, "verify_execution_payload_envelope", lambda *args: None)
    store.time_ms = at_slot(spec, 8) + spec.get_support_cutoff_ms()
    spec.on_execution_payload_envelope(store, envelope)

    slot = spec.DEEP_FG_SOURCE_SLOTS + 8
    store.locks = {lock(spec, full, slot=slot - 1, fast=False, expiry=slot + 1)}
    store.time_ms = at_slot(spec, slot) + spec.get_support_cutoff_ms()
    assert spec.get_ac_frame(store) == pending
    store.time_ms += spec.config.SLOT_DURATION_MS
    assert spec.get_ac_frame(store) == full
    spec.on_execution_payload_envelope(store, envelope)
    assert spec.get_ac_frame(store) == full


def test_fg_source_fast_precedes_old_prefix_then_falls_back(spec, tree):
    store, nodes = tree
    slot = spec.DEEP_FG_SOURCE_SLOTS + 8
    store.time_ms = at_slot(spec, slot) + spec.get_support_cutoff_ms()
    assert spec.get_ac_frame(store) is None
    store.locks = {
        lock(spec, nodes["P"], slot=slot - 2, expiry=slot + 1),
        lock(spec, nodes["A"], slot=slot - 1, fast=False, expiry=slot + 1),
    }
    assert spec.get_ac_frame(store) == nodes["P"]
    store.time_ms += spec.config.SLOT_DURATION_MS
    assert spec.get_ac_frame(store) == nodes["A"]
    store.time_ms += spec.config.SLOT_DURATION_MS
    assert spec.get_ac_frame(store) is None


@pytest.mark.parametrize("vote_slot", [10, 11])
def test_head_vote_admission_and_gossip_cap(spec, tree, monkeypatch, vote_slot):
    store, nodes = tree
    monkeypatch.setattr(spec, "config", spec.config._replace(PQ_FORK_EPOCH=0))
    for state in store.block_states.values():
        state.slot = spec.Slot(11)
        state.finalized_checkpoint = store.finalized_checkpoint.copy()
        add_elected_validator(spec, state)
    seen = SimpleNamespace(head_vote_validator_slots={})
    state = store.block_states[nodes["F"].root]
    messages = []
    for name in ["A", "B", "P"]:
        data = spec.HeadVoteData(
            slot=vote_slot,
            beacon_block_root=nodes[name].root,
            payload_status=spec.PAYLOAD_STATUS_EMPTY,
        )
        messages.append(
            spec.HeadVoteMessage(
                validator_index=0,
                data=data,
                signature=spec.get_head_vote_signature(state, data, 1),
                proof=spec.VRFProof(data=get_vrf_proof(0, data.slot)),
            )
        )
    invalid = messages[0].copy()
    invalid.signature = spec.XMSSSignature()
    with pytest.raises(spec.GossipReject):
        spec.validate_head_vote_gossip(seen, store, invalid, store.time_ms, {})
    with pytest.raises(AssertionError):
        spec.on_head_vote_message(store, invalid)
    assert not seen.head_vote_validator_slots
    assert not store.head_vote_messages
    if vote_slot == 11:
        store.time_ms = at_slot(spec, 11) - spec.config.MAXIMUM_GOSSIP_CLOCK_DISPARITY - 1
        with pytest.raises(spec.GossipIgnore):
            spec.validate_head_vote_gossip(seen, store, messages[0], store.time_ms, {})
        spec.on_head_vote_message(store, messages[0])
        assert not store.pending_head_votes
        store.time_ms += 1
        spec.validate_head_vote_gossip(seen, store, messages[0], store.time_ms, {})
        spec.on_head_vote_message(store, messages[0])
        assert [message for message, _ in store.pending_head_votes] == [messages[0]]
        assert not store.head_vote_messages
        # A changed head-state validation context cannot abort queued admission.
        for block_state in store.block_states.values():
            block_state.validators[0].pubkey = spec.xmss.SkToPk(2)
        spec.on_tick(store, at_slot(spec, 11))
        for block_state in store.block_states.values():
            block_state.validators[0].pubkey = spec.xmss.SkToPk(1)
        assert not store.pending_head_votes
        assert store.head_vote_messages[11] == [messages[0]]
        seen.head_vote_validator_slots = {}
    for message in messages[:2]:
        spec.validate_head_vote_gossip(seen, store, message, store.time_ms, {})
        spec.on_head_vote_message(store, message)
    receipt_times = dict(store.head_vote_message_receipt_times)
    for message in [messages[0], messages[2]]:
        with pytest.raises(spec.GossipIgnore):
            spec.validate_head_vote_gossip(seen, store, message, store.time_ms, {})
        spec.on_head_vote_message(store, message)
    assert store.head_vote_messages[vote_slot] == messages[:2]
    assert store.head_vote_message_receipt_times == receipt_times
    spec.on_tick(store, at_slot(spec, vote_slot + 2) + spec.config.MAXIMUM_GOSSIP_CLOCK_DISPARITY)
    assert not store.head_vote_messages
    assert not store.head_vote_message_receipt_times
    with pytest.raises(spec.GossipIgnore):
        spec.validate_head_vote_gossip(seen, store, messages[0], store.time_ms, {})
    assert not seen.head_vote_validator_slots


@pytest.mark.parametrize("payload_available", [False, True])
def test_responsive_vote_waits_for_support_payload(spec, tree, payload_available):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11)
    proposal = store.blocks[nodes["B1"].root]
    proposal.slot = spec.Slot(11)
    propose_votes(
        spec,
        store,
        nodes,
        [(replace(nodes["A"], payload_status=spec.PAYLOAD_STATUS_FULL), [0], True)],
    )
    assert spec.get_ac_vote_head(store) is None
    if payload_available:
        store.payloads[nodes["A"].root] = spec.ExecutionPayloadEnvelope()
        store.payload_receipt_times[nodes["A"].root] = store.time_ms
        early = spec.get_ac_vote_head(store)
        assert early is not None
    else:
        assert store.prepared_head_vote is None
        # One ready equivocation suffices even if its sibling is unready.
        store.blocks[nodes["A1"].root].slot = spec.Slot(11)
        assert spec.get_ac_vote_head(store) is not None
    spec.on_tick(store, at_slot(spec, 11) + spec.get_head_vote_due_ms())
    assert spec.get_ac_vote_head(store) is not None
    if payload_available:
        assert spec.get_ac_vote_head(store) == early


@pytest.mark.parametrize(
    ("supported", "held"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_vote_readiness_waits_for_evidence_and_supported_heads(spec, tree, supported, held):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11)
    unknown = spec.ForkChoiceNode(
        root=spec.Root(b"\x99" * 32), payload_status=spec.PAYLOAD_STATUS_EMPTY
    )
    propose_votes(spec, store, nodes, [(unknown, [0], supported)])
    # The proposal is by the proposer that the head chain expects
    for state in store.block_states.values():
        state.proposer_lookahead[11 % spec.SLOTS_PER_EPOCH] = 1
    store.blocks[nodes["B1"].root].proposer_index = 1
    if not held:
        store.head_vote_messages.clear()
        store.head_vote_message_receipt_times.clear()
    ready = held and not supported
    assert (spec.get_ac_vote_head(store) is not None) == ready


def test_responsive_vote_deadline_and_duplicate_preparation(spec, tree):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11)
    assert spec.get_ac_vote_head(store) is None
    store.blocks[nodes["B1"].root].slot = spec.Slot(11)
    vote(spec, store, nodes["B"], 0)
    expected = replace(nodes["B1"], payload_status=spec.PAYLOAD_STATUS_PENDING)
    assert spec.get_ac_vote_head(store) == expected
    store.time_ms += spec.get_head_vote_due_ms() + 1
    assert spec.get_ac_vote_head(store) == expected
    store.time_ms = at_slot(spec, 12) + spec.get_head_vote_due_ms() + 1
    assert spec.get_ac_vote_head(store) is None


@pytest.mark.parametrize(("seat_offset", "fast_lock"), [(-1, False), (0, True)])
def test_scheduled_fast_lock_at_threshold(spec, tree, seat_offset, fast_lock):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11)
    cutoff = store.time_ms + spec.get_support_cutoff_ms()
    seats = spec.HEAD_VOTE_FAST_LOCK_THRESHOLD + seat_offset
    vote(spec, store, nodes["A"], 0, slot=10, weight=seats)
    spec.on_tick(store, cutoff - 1)
    assert not store.locks
    assert 10 not in store.saved_confirmations
    spec.on_tick(store, cutoff)
    assert store.latest_confirmed == nodes["A"]
    assert store.saved_confirmations[10] == nodes["A"]
    assert store.locks == ({lock(spec, nodes["A"], slot=10, expiry=12)} if fast_lock else set())
    assert spec.get_ac_frame(store) == (nodes["A"] if fast_lock else None)


def test_queued_vote_preserves_authenticated_opening(spec, tree):
    store, nodes = tree
    message = spec.HeadVoteMessage(
        validator_index=0,
        data=spec.HeadVoteData(
            slot=12, beacon_block_root=nodes["A"].root, payload_status=spec.PAYLOAD_STATUS_EMPTY
        ),
    )
    state = store.block_states[nodes["A"].root]
    add_elected_validator(spec, state)
    message.proof = spec.VRFProof(data=get_vrf_proof(0, message.data.slot))
    admitted = spec.get_ac_vote(state, message)
    store.pending_head_votes = [(message, admitted)]
    spec.on_tick(store, at_slot(spec, 12) + spec.get_head_vote_due_ms())
    assert not store.pending_head_votes
    assert store.head_vote_messages[12] == [message]
    assert store.prepared_head_vote[0] == 12
    assert store.validated_head_votes[spec.hash_tree_root(message)] == admitted


def test_responsive_vote_accepts_other_in_frame_proposer(spec, tree):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11)
    for state in store.block_states.values():
        state.proposer_lookahead[11 % spec.SLOTS_PER_EPOCH] = 1
    proposal = store.blocks[nodes["B1"].root]
    proposal.slot = spec.Slot(11)
    proposal.proposer_index = 2
    # An empty proposal is ready even before any previous-slot votes are held.
    assert not proposal.body.head_vote_declarations
    _, _, ready = spec.get_declared_votes_and_readiness(
        store, finalized_subtree(spec, store), spec.Slot(11)
    )
    assert ready
    vote(spec, store, nodes["B"], 0)
    assert spec.get_ac_vote_head(store) == replace(
        nodes["B1"], payload_status=spec.PAYLOAD_STATUS_PENDING
    )


def test_head_vote_receipt_respects_current_finalized_subtree(spec, tree):
    store, nodes = tree
    state = store.block_states[nodes["A"].root]
    add_elected_validator(spec, state)
    store.ac_tally_root = nodes["A"].root

    def message(status):
        data = spec.HeadVoteData(slot=11, beacon_block_root=nodes["A"].root, payload_status=status)
        return spec.HeadVoteMessage(
            validator_index=0,
            data=data,
            signature=spec.get_head_vote_signature(state, data, 1),
            proof=spec.VRFProof(data=get_vrf_proof(0, data.slot)),
        )

    admitted = message(spec.PAYLOAD_STATUS_EMPTY)
    spec.on_head_vote_message(store, admitted)
    store.finalized_checkpoint = spec.HeightCheckpoint(height=1, root=nodes["B"].root)
    spec.on_head_vote_message(store, message(spec.PAYLOAD_STATUS_FULL))
    assert store.head_vote_messages[11] == [admitted]


@pytest.mark.parametrize("in_frame", [False, True])
def test_matched_proposer_support_requires_frame_membership(spec, tree, in_frame):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11)
    store.blocks[nodes["A"].root].slot = spec.Slot(10)
    store.blocks[nodes["A1"].root].slot = spec.Slot(12)
    head = replace(nodes["A"], payload_status=spec.PAYLOAD_STATUS_PENDING)
    propose_votes(spec, store, nodes, [(head, [0], True)])
    if not in_frame:
        store.finalized_checkpoint = spec.HeightCheckpoint(height=0, root=nodes["B"].root)
        store.justified_checkpoint = store.finalized_checkpoint.copy()
    votes, support, ready = spec.get_declared_votes_and_readiness(
        store, finalized_subtree(spec, store), spec.Slot(11)
    )
    assert {v.supported_node for v in votes} == {head}
    assert {v.supported_node for v in support} == ({head} if in_frame else set())
    assert ready == in_frame


def test_genesis_deadline_vote(spec, tree):
    store, nodes = tree
    store.blocks = {nodes["F"].root: store.blocks[nodes["F"].root]}
    store.time_ms = spec.get_head_vote_due_ms() - 1
    assert spec.get_ac_vote_head(store) is None
    spec.on_tick(store, spec.get_head_vote_due_ms())
    expected = replace(nodes["F"], payload_status=spec.PAYLOAD_STATUS_PENDING)
    assert store.prepared_head_vote == (spec.GENESIS_SLOT, expected)
    assert spec.get_ac_vote_head(store) == expected
    store.time_ms += 1
    assert spec.get_ac_vote_head(store) == expected


def test_head_vote_gossip_uses_stale_voted_head(spec, tree):
    store, nodes = tree
    state = spec.BeaconState(slot=spec.Slot(5))
    add_elected_validator(spec, state)
    state.finalized_checkpoint = store.finalized_checkpoint.copy()
    root = nodes["B1"].root
    store.block_states[root] = state
    add_elected_validator(spec, store.block_states[nodes["F"].root])
    slot = spec.Slot(2 * spec.SLOTS_PER_EPOCH + 1)
    store.time_ms = at_slot(spec, slot)
    data = spec.HeadVoteData(
        slot=slot, beacon_block_root=root, payload_status=spec.PAYLOAD_STATUS_EMPTY
    )
    message = spec.HeadVoteMessage(
        validator_index=0,
        data=data,
        signature=spec.get_head_vote_signature(state, data, 1),
        proof=spec.VRFProof(data=get_vrf_proof(0, data.slot)),
    )
    seen = SimpleNamespace(head_vote_validator_slots={})
    spec.validate_head_vote_gossip(seen, store, message, store.time_ms, {})
    assert seen.head_vote_validator_slots[(slot, state.validators[0].pubkey)] == {
        spec.hash_tree_root(data)
    }
    spec.on_head_vote_message(store, message)
    assert store.head_vote_messages[slot] == [message]
    assert store.block_states[root].slot == 5


@pytest.mark.parametrize("gap", [1, 2])
def test_head_vote_admission_across_snapshot_rollover(spec, tree, gap):
    store, nodes = tree
    head = store.block_states[nodes["B"].root]
    add_elected_validator(spec, head)
    slot = spec.Slot(10)
    proof = spec.VRFProof(data=get_vrf_proof(0, slot))
    head.dynasty = spec.Dynasty(spec.AC_SNAPSHOT_CYCLE_LENGTH - 1)
    for seed in range(32):
        head.ac_snapshots[0].seed = spec.Bytes32(seed.to_bytes(32, "little"))
        if spec.get_head_vote_weight(head, slot, spec.ValidatorIndex(0), proof) > 0:
            break
    assert spec.get_head_vote_weight(head, slot, spec.ValidatorIndex(0), proof) > 0
    tally = head.copy()
    tally.dynasty += gap
    store.ac_tally_root = nodes["A"].root
    store.block_states[nodes["A"].root] = tally
    data = spec.HeadVoteData(
        slot=slot, beacon_block_root=nodes["B"].root, payload_status=spec.PAYLOAD_STATUS_EMPTY
    )
    message = spec.HeadVoteMessage(
        validator_index=0,
        data=data,
        signature=spec.get_head_vote_signature(head, data, 1),
        proof=proof,
    )
    proposal = store.blocks[nodes["B1"].root]
    proposal.slot = slot + 1
    proposal.body.head_vote_declarations.append(
        spec.HeadVoteDeclaration(
            validator_index=0,
            beacon_block_root=data.beacon_block_root,
            payload_status=data.payload_status,
        )
    )
    proposal.body.head_vote_support_bits.append(False)
    store.time_ms = at_slot(spec, slot + 1) + spec.get_head_vote_due_ms() - 1
    seen = SimpleNamespace(head_vote_validator_slots={})
    if gap == 2:
        # A valid opening but invalid signature is ignored before XMSS.
        invalid = message.copy()
        invalid.signature = spec.XMSSSignature()
        with pytest.raises(spec.GossipIgnore, match="no head vote ticket"):
            spec.validate_head_vote_gossip(seen, store, invalid, store.time_ms, {})
        spec.on_head_vote_message(store, invalid)
        # An honestly signed declaration still cannot make this receiver ready.
        spec.on_head_vote_message(store, message)
        assert not seen.head_vote_validator_slots
        assert not store.head_vote_messages
        assert spec.get_ac_vote_head(store) is None
        store.time_ms += 1
        assert spec.get_ac_vote_head(store) is not None
        return

    spec.validate_head_vote_gossip(seen, store, message, store.time_ms, {})
    spec.on_head_vote_message(store, message)
    assert store.head_vote_messages[slot] == [message]
    assert spec.sum_head_vote_weights(store, set(store.validated_head_votes.values()), slot) == 0
    assert spec.get_ac_vote_head(store) is not None


def test_admitted_vote_is_reweighed_when_max_dynasty_advances(spec, tree, monkeypatch):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11) + spec.get_head_vote_due_ms() + 1
    slot = spec.Slot(10)
    state = store.block_states[nodes["F"].root]
    add_elected_validator(spec, state)
    voted_state = state.copy()
    voted_state.ac_snapshots[0] = spec.ACSnapshot()
    store.block_states[nodes["B"].root] = voted_state
    data = spec.HeadVoteData(
        slot=slot, beacon_block_root=nodes["B"].root, payload_status=spec.PAYLOAD_STATUS_EMPTY
    )
    proof = spec.VRFProof(data=get_vrf_proof(0, slot))
    message = spec.HeadVoteMessage(
        validator_index=0,
        data=data,
        proof=proof,
        signature=spec.get_head_vote_signature(voted_state, data, 1),
    )
    assert spec.get_head_vote_weight(voted_state, slot, spec.ValidatorIndex(0), proof) == 0
    spec.on_head_vote_message(store, message)
    votes = set(store.validated_head_votes.values())
    assert len(votes) == 1
    assert spec.sum_head_vote_weights(store, votes, slot) == spec.HEAD_VOTE_COMMITTEE_SIZE

    # The imported branch wins the tally view, not the vote's signed branch.
    # Set a later dynasty start to distinguish current-dynasty from duty-slot lookup.
    parent = store.block_states[nodes["A"].root] = state.copy()
    parent.validators = spec.Validators(data=[spec.Validator(), state.validators[0]])
    parent.ac_snapshots[0] = spec.ACSnapshot()
    for seed in range(32):
        parent.ac_snapshots[1] = spec.ACSnapshot(
            weights=spec.ACWeights(data=[0, 1]), seed=spec.Bytes32(seed.to_bytes(32, "little"))
        )
        draws = spec.compute_ac_snapshot_seat_indices(
            parent.ac_snapshots[1], spec.Uint64(1), slot, spec.ValidatorIndex(1), proof[0]
        )
        if 0 in draws:
            break
    assert 0 in draws

    def transition(post, signed, validate_result):
        if signed.message.slot == 11:
            post.dynasty = spec.Dynasty(1)
            post.dynasty_start_slot = spec.Slot(11)

    monkeypatch.setattr(spec, "state_transition", transition)
    for block_slot in (11, 10):
        block = spec.BeaconBlock(slot=block_slot, parent_root=nodes["A"].root)
        spec.on_block(store, spec.SignedBeaconBlock(message=block))
        assert spec.get_ac_tally_state(store).dynasty == 1
        assert spec.sum_head_vote_weights(store, votes, slot) == draws.count(0)


@pytest.mark.parametrize("larger_first", [False, True])
def test_tally_state_breaks_equal_dynasty_ties_by_root(spec, tree, monkeypatch, larger_first):
    store, nodes = tree

    def transition(post, signed, validate_result):
        post.dynasty = spec.Dynasty(1)
        post.dynasty_start_slot = signed.message.slot

    monkeypatch.setattr(spec, "state_transition", transition)
    blocks = [spec.BeaconBlock(slot=slot, parent_root=nodes["A"].root) for slot in (10, 11)]
    blocks.sort(key=spec.hash_tree_root, reverse=larger_first)
    for block in blocks:
        spec.on_block(store, spec.SignedBeaconBlock(message=block))

    assert (
        spec.get_ac_tally_state(store).dynasty_start_slot
        == max(blocks, key=spec.hash_tree_root).slot
    )


def test_cross_branch_head_vote_identity(spec, tree):
    store, nodes = tree
    slot = spec.Slot(10)
    store.time_ms = at_slot(spec, slot)
    seen = SimpleNamespace(head_vote_validator_slots={})
    tally = store.block_states[nodes["F"].root]
    for key in (1, 2):
        tally.validators.append(
            spec.Validator(pubkey=spec.xmss.SkToPk(key), vrf_root=get_vrf_root(key - 1))
        )
    tally.ac_snapshots[0] = spec.ACSnapshot(weights=spec.ACWeights(data=[1, 1]))
    for name, index, key in [("A", 0, 1), ("B", 1, 1), ("A1", 2, 1), ("P", 0, 2)]:
        node = nodes[name]
        store.blocks[node.root].parent_root = nodes["F"].root
        state = spec.BeaconState(slot=store.blocks[node.root].slot)
        for prefix in range(index):
            state.validators.append(spec.Validator(pubkey=spec.xmss.SkToPk(10 + prefix)))
        state.validators.append(
            spec.Validator(pubkey=spec.xmss.SkToPk(key), vrf_root=get_vrf_root(key - 1))
        )
        state.ac_snapshots[0] = spec.ACSnapshot(weights=spec.ACWeights(data=[0] * index + [1]))
        store.block_states[node.root] = state
        data = spec.HeadVoteData(
            slot=slot, beacon_block_root=node.root, payload_status=node.payload_status
        )
        message = spec.HeadVoteMessage(
            validator_index=index,
            data=data,
            proof=spec.VRFProof(data=get_vrf_proof(key - 1, data.slot)),
            signature=spec.get_head_vote_signature(state, data, key),
        )
        if name == "A1":
            with pytest.raises(spec.GossipIgnore):
                spec.validate_head_vote_gossip(seen, store, message, store.time_ms, {})
        else:
            spec.validate_head_vote_gossip(seen, store, message, store.time_ms, {})
        spec.on_head_vote_message(store, message)
    infinite = spec.INFINITE_TIME_MS
    votes, _ = spec.get_head_votes_by_cutoff(store, slot, infinite)
    assert {vote.supported_node for vote in votes} == {nodes["P"], nodes["A"], nodes["B"]}
    assert spec.get_equivocating_pubkeys(votes) == {spec.XMSSPubkey(spec.xmss.SkToPk(1))}
    store.ac_tally_root = nodes["B"].root
    assert spec.sum_head_vote_weights(store, votes, slot) == spec.HEAD_VOTE_COMMITTEE_SIZE


@pytest.mark.parametrize(("block_slot", "status"), [(11, 0), (11, 1), (11, 2), (4, 2), (4, 0)])
def test_receipt_enforces_vote_slot_payload_status(spec, tree, monkeypatch, block_slot, status):
    store, nodes = tree
    monkeypatch.setattr(spec, "config", spec.config._replace(PQ_FORK_EPOCH=0))
    store.time_ms = at_slot(spec, 11) + 1
    store.blocks[nodes["A1"].root].slot = spec.Slot(block_slot)
    for state in store.block_states.values():
        add_elected_validator(spec, state)
    data = spec.HeadVoteData(slot=11, beacon_block_root=nodes["A1"].root, payload_status=status)
    state = store.block_states[nodes["F"].root]
    message = spec.HeadVoteMessage(
        validator_index=0,
        data=data,
        signature=spec.get_head_vote_signature(state, data, 1),
        proof=spec.VRFProof(data=get_vrf_proof(0, data.slot)),
    )
    if (block_slot == 11) == (status == spec.PAYLOAD_STATUS_PENDING):
        spec.on_head_vote_message(store, message)
        assert store.head_vote_messages[11] == [message]
        assert store.latest_confirmed == spec.get_fg_frame(store).finalized_node
        assert not store.locks
    else:
        with pytest.raises(AssertionError):
            spec.on_head_vote_message(store, message)
        assert not store.head_vote_messages
        assert not store.locks


@pytest.mark.parametrize("block_slot", [10, 11])
@pytest.mark.parametrize("status", [0, 1, 2])
@pytest.mark.parametrize("block_first", [False, True])
def test_vote_resolution_enforces_status_in_both_arrival_orders(
    spec, tree, block_slot, status, block_first
):
    store, nodes = tree
    store.time_ms = at_slot(spec, 11) + 1
    root = nodes["A1"].root
    store.blocks[root].slot = spec.Slot(block_slot)
    store.payloads[root] = spec.ExecutionPayloadEnvelope()
    store.payload_receipt_times[root] = store.time_ms
    for state in store.block_states.values():
        add_elected_validator(spec, state)
    data = spec.HeadVoteData(slot=11, beacon_block_root=root, payload_status=status)
    state = store.block_states[nodes["F"].root]
    message = spec.HeadVoteMessage(
        validator_index=0,
        data=data,
        signature=spec.get_head_vote_signature(state, data, 1),
        proof=spec.VRFProof(data=get_vrf_proof(0, data.slot)),
    )
    valid = (block_slot == 11) == (status == spec.PAYLOAD_STATUS_PENDING)
    if not block_first:
        block = store.blocks.pop(root)
        post_state = store.block_states.pop(root)
        spec.on_head_vote_message(store, message)
        assert (
            spec.get_head_vote_messages_by_cutoff(
                store, spec.Slot(11), spec.INFINITE_TIME_MS, spec.INFINITE_TIME_MS
            )[1]
            == []
        )
        store.blocks[root] = block
        store.block_states[root] = post_state
        store.block_receipt_times[root] = store.time_ms + 1
    if valid:
        spec.on_head_vote_message(store, message)
    else:
        with pytest.raises(AssertionError):
            spec.on_head_vote_message(store, message)
    view = spec.get_head_vote_messages_by_cutoff(
        store, spec.Slot(11), spec.INFINITE_TIME_MS, spec.INFINITE_TIME_MS
    )
    assert view[1] == ([0] if valid else [])
    proposal = store.blocks[nodes["B1"].root]
    proposal.slot = spec.Slot(12)
    proposal.body.head_vote_declarations.append(
        spec.HeadVoteDeclaration(
            validator_index=message.validator_index, beacon_block_root=root, payload_status=status
        )
    )
    proposal.body.head_vote_support_bits.append(True)
    votes, support, ready = spec.get_declared_votes_and_readiness(
        store, finalized_subtree(spec, store), spec.Slot(12)
    )
    expected = {store.validated_head_votes[spec.hash_tree_root(message)]} if valid else set()
    assert votes == support == expected
    assert ready == valid


def test_slow_lock_does_not_require_fast_threshold(spec, tree):
    store, nodes = tree
    store.saved_confirmations = dict.fromkeys(range(10, 41), nodes["A"])
    store.time_ms = at_slot(spec, 42) + spec.get_support_cutoff_ms()
    vote(spec, store, nodes["A"], 0, slot=41)
    spec.update_confirmation(store, spec.Slot(41))
    assert store.locks == {lock(spec, nodes["A"], slot=41, fast=False, expiry=73)}
