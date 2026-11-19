"""Fork choice, gossip and import across the EIP-8453 fork, from a real Heze store."""

from dataclasses import fields
from importlib import import_module
from typing import get_origin

import pytest

from eth_consensus_specs.test import context
from eth_consensus_specs.test.helpers import constants
from eth_consensus_specs.test.helpers.block import (
    build_empty_block,
    build_empty_block_for_next_slot,
    sign_block,
)
from eth_consensus_specs.test.helpers.fork_choice import get_genesis_forkchoice_store_and_block
from eth_consensus_specs.test.helpers.genesis import create_genesis_state
from eth_consensus_specs.test.helpers.keys import privkeys
from eth_consensus_specs.test.helpers.state import state_transition_and_sign_block
from eth_consensus_specs.utils import bls

FORK_EPOCH = 1


@pytest.fixture
def heze():
    return import_module(f"eth_consensus_specs.heze.{context.DEFAULT_TEST_PRESET}")


@pytest.fixture
def spec(preset, monkeypatch):
    spec = import_module(f"eth_consensus_specs.eip8453.{context.DEFAULT_TEST_PRESET}")
    monkeypatch.setattr(
        spec, "config", spec.config._replace(EIP8453_FORK_EPOCH=spec.Epoch(FORK_EPOCH))
    )
    # Let the block helpers treat EIP-8453 as the fork after Heze
    monkeypatch.setitem(constants.PREVIOUS_FORK_OF, spec.fork, constants.HEZE)
    return spec


@pytest.fixture(autouse=True)
def active_bls():
    previous = bls.bls_active
    bls.bls_active = True
    yield
    bls.bls_active = previous


def slot_time(spec, slot):
    return spec.compute_time_at_slot_ms(spec.Uint64(0), spec.Slot(slot))


@pytest.fixture
def chain(spec, heze):
    """
    A real Heze store with blocks at slots 1 (imported) and 2 (held back), at
    the fork slot, converted with ``upgrade_store_to_eip8453``.
    """
    genesis_state = create_genesis_state(
        heze, [heze.MAX_EFFECTIVE_BALANCE] * 64, heze.MAX_EFFECTIVE_BALANCE
    )
    heze_store, genesis_block = get_genesis_forkchoice_store_and_block(heze, genesis_state)
    state = genesis_state.copy()
    signed_a = state_transition_and_sign_block(
        heze, state, build_empty_block_for_next_slot(heze, state)
    )
    signed_b = state_transition_and_sign_block(
        heze, state, build_empty_block_for_next_slot(heze, state)
    )
    heze.on_tick(heze_store, slot_time(heze, 1))
    heze.on_block(heze_store, signed_a)
    fork_slot = spec.compute_start_slot_at_epoch(spec.Epoch(FORK_EPOCH))
    heze.on_tick(heze_store, slot_time(heze, fork_slot))
    store = spec.upgrade_store_to_eip8453(heze_store)
    return store, heze_store, genesis_block, signed_a, signed_b, fork_slot


def make_seen(spec):
    return spec.Seen(
        **{field.name: (get_origin(field.type) or field.type)() for field in fields(spec.Seen)}
    )


def post_fork_block(spec, store, parent_root, slot):
    upgraded = spec.get_block_state(store, parent_root)
    block = build_empty_block(spec, upgraded, slot)
    assert block.parent_root == parent_root
    post = upgraded.copy()
    if post.slot < slot:
        spec.process_slots(post, slot)
    spec.process_block(post, block)
    block.state_root = post.hash_tree_root()
    return sign_block(spec, upgraded, block, proposer_index=block.proposer_index)


def assert_viable_with_anchor_head(spec, store, root):
    # The new block is in the FG frame; with no head votes yet, the AC head
    # stays at the activation anchor
    assert any(node.root == root for node in spec.get_fg_frame(store).tree)
    assert spec.get_head(store).root == store.finalized_checkpoint.root


def test_store_conversion_anchors_at_casper_finality(spec, chain):
    store, heze_store, genesis_block, signed_a, _, _ = chain
    anchor = spec.HeightCheckpoint(height=0, root=heze_store.finalized_checkpoint.root)
    assert anchor.root == genesis_block.hash_tree_root()
    assert store.finalized_checkpoint == anchor
    assert store.justified_checkpoint == anchor
    assert store.max_height == 1
    # The head is not finalized
    a_root = signed_a.message.hash_tree_root()
    assert a_root != store.finalized_checkpoint.root
    assert spec.get_head(store).root == anchor.root
    upgraded = spec.get_block_state(store, a_root)
    assert upgraded.target_checkpoint == spec.HeightCheckpoint(height=1, root=a_root)
    assert upgraded.finalized_checkpoint.height == anchor.height


def test_first_post_fork_block_import(spec, chain):
    store, _, _, signed_a, _, fork_slot = chain
    a_root = signed_a.message.hash_tree_root()
    signed_c = post_fork_block(spec, store, a_root, fork_slot)
    spec.on_block(store, signed_c)
    c_root = signed_c.message.hash_tree_root()
    assert store.block_states[c_root].target_checkpoint == spec.HeightCheckpoint(
        height=1, root=a_root
    )
    assert_viable_with_anchor_head(spec, store, c_root)


def test_restart_skips_heze_blocks(spec, chain):
    store, _, _, signed_a, _, _ = chain
    spec.on_ac_restart(store, store.time_ms)
    assert store.block_receipt_times[signed_a.message.hash_tree_root()] == store.time_ms


def test_late_pre_fork_parent_is_imported_with_heze_rules(spec, heze, chain):
    store, _, _, _, signed_b, fork_slot = chain
    b_root = signed_b.message.hash_tree_root()
    spec.on_block(store, signed_b)
    assert isinstance(store.block_states[b_root], heze.BeaconState)
    signed_c = post_fork_block(spec, store, b_root, fork_slot)
    spec.on_block(store, signed_c)
    c_root = signed_c.message.hash_tree_root()
    assert store.block_states[c_root].target_checkpoint == spec.HeightCheckpoint(
        height=1, root=b_root
    )
    assert_viable_with_anchor_head(spec, store, c_root)


def test_pre_fork_block_is_ignored_on_gossip(spec, chain):
    store, _, _, signed_a, _, fork_slot = chain
    signed = post_fork_block(spec, store, signed_a.message.hash_tree_root(), fork_slot)
    signed.message.slot = fork_slot - 1
    with pytest.raises(spec.GossipIgnore, match="before the eip8453 fork"):
        spec.validate_beacon_block_gossip(make_seen(spec), store, signed, store.time_ms)


def test_head_vote_with_heze_head(spec, chain):
    store, _, _, signed_a, _, fork_slot = chain
    a_root = signed_a.message.hash_tree_root()
    upgraded = spec.get_block_state(store, a_root)
    validator = spec.get_head_vote_committee(upgraded, fork_slot)[0]
    data = spec.HeadVoteData(
        slot=fork_slot, beacon_block_root=a_root, payload_status=spec.PAYLOAD_STATUS_EMPTY
    )
    message = spec.HeadVoteMessage(
        validator_index=validator,
        data=data,
        signature=spec.get_head_vote_signature(upgraded, data, privkeys[validator]),
    )
    # The pulled-up Heze head state is read without error; the genesis Casper
    # finalized root (zero in the state) then stops the vote
    with pytest.raises(spec.GossipIgnore, match="finalized block is not an ancestor"):
        spec.validate_head_vote_gossip(make_seen(spec), store, message, store.time_ms, {})
