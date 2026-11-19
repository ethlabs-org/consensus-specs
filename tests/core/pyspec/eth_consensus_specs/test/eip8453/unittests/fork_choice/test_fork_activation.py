"""Fork choice, gossip and import across the EIP-8453 fork."""

from dataclasses import fields
from importlib import import_module
from typing import get_origin

import pytest

from eth_consensus_specs.test import context
from eth_consensus_specs.test.helpers import constants
from eth_consensus_specs.test.helpers.block import build_empty_block, sign_block
from eth_consensus_specs.test.helpers.genesis import create_genesis_state
from eth_consensus_specs.test.helpers.keys import privkeys
from eth_consensus_specs.utils import bls

FORK_EPOCH = 1


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


@pytest.fixture
def heze_store(spec):
    """
    A store at the fork slot whose head is the last Heze block, stored with its
    Heze post-state, as the Heze fork choice left it.
    """
    heze = import_module(f"eth_consensus_specs.heze.{context.DEFAULT_TEST_PRESET}")
    genesis_state = create_genesis_state(
        heze, [heze.MAX_EFFECTIVE_BALANCE] * 64, heze.MAX_EFFECTIVE_BALANCE
    )
    genesis_block = heze.BeaconBlock(slot=0)
    # The last Heze block, at slot 1, on top of the finalized genesis block (root 0)
    heze_block = heze.BeaconBlock(slot=1, parent_root=heze.Root())
    heze_state = genesis_state.copy()
    heze_state.slot = heze.Slot(1)
    heze_state.latest_block_header = heze.BeaconBlockHeader(
        slot=1, parent_root=heze.Root(), body_root=heze_block.body.hash_tree_root()
    )
    heze_block.state_root = heze_state.hash_tree_root()
    heze_root = heze_block.hash_tree_root()
    fork_slot = spec.compute_start_slot_at_epoch(spec.Epoch(FORK_EPOCH))
    # The store is anchored at the last Heze block, the target of height 1
    anchor = spec.HeightCheckpoint(height=1, root=heze_root)
    blocks = {spec.Root(): genesis_block, heze_root: heze_block}
    store = spec.Store(
        time_ms=spec.compute_time_at_slot_ms(spec.Uint64(0), fork_slot),
        genesis_time_ms=spec.Uint64(0),
        justified_checkpoint=anchor,
        finalized_checkpoint=anchor,
        proposer_boost_root=spec.Root(),
        blocks=blocks,
        block_states={spec.Root(): genesis_state, heze_root: heze_state},
        latest_messages={},
        payloads={},
        payload_timeliness_vote={root: [None] * spec.PTC_SIZE for root in blocks},
        payload_data_availability_vote={root: [None] * spec.PTC_SIZE for root in blocks},
        payload_inclusion_list_satisfaction=dict.fromkeys(blocks, True),
        max_height=spec.Height(1),
        block_receipt_times=dict.fromkeys(blocks, spec.Uint64(0)),
        payload_receipt_times={},
        latest_confirmed=spec.ForkChoiceNode(
            root=heze_root, payload_status=spec.PAYLOAD_STATUS_PENDING
        ),
        saved_confirmations={},
        locks=set(),
        head_vote_messages={},
        head_vote_message_receipt_times={},
        head_vote_committees={},
    )
    return store, heze_root, fork_slot


def make_seen(spec):
    return spec.Seen(
        **{field.name: (get_origin(field.type) or field.type)() for field in fields(spec.Seen)}
    )


def first_post_fork_block(spec, store, heze_root, fork_slot):
    upgraded = spec.get_block_state(store, heze_root)
    block = build_empty_block(spec, upgraded, fork_slot)
    assert block.parent_root == heze_root
    post = upgraded.copy()
    spec.process_block(post, block)
    block.state_root = post.hash_tree_root()
    return sign_block(spec, upgraded, block, proposer_index=block.proposer_index)


def test_head_selection_reads_a_stored_heze_state(spec, heze_store):
    store, heze_root, _ = heze_store
    assert spec.get_head(store).root == heze_root


def test_first_post_fork_block_gossip_and_import(spec, heze_store):
    store, heze_root, fork_slot = heze_store
    signed = first_post_fork_block(spec, store, heze_root, fork_slot)
    seen = make_seen(spec)
    spec.validate_beacon_block_gossip(seen, store, signed, store.time_ms)
    assert (signed.message.slot, signed.message.proposer_index) in seen.proposer_slots

    spec.on_block(store, signed)
    root = signed.message.hash_tree_root()
    post = store.block_states[root]
    assert post.target_checkpoint == spec.HeightCheckpoint(height=1, root=heze_root)
    assert spec.get_head(store).root in (root, heze_root)


def test_pre_fork_block_is_ignored_on_gossip(spec, heze_store):
    store, heze_root, fork_slot = heze_store
    signed = first_post_fork_block(spec, store, heze_root, fork_slot)
    signed.message.slot = fork_slot - 1
    with pytest.raises(spec.GossipIgnore, match="before the eip8453 fork"):
        spec.validate_beacon_block_gossip(make_seen(spec), store, signed, store.time_ms)


def test_head_vote_with_heze_head(spec, heze_store):
    store, heze_root, fork_slot = heze_store
    upgraded = spec.get_block_state(store, heze_root)
    validator = spec.get_head_vote_committee(upgraded, fork_slot)[0]
    data = spec.HeadVoteData(
        slot=fork_slot, beacon_block_root=heze_root, payload_status=spec.PAYLOAD_STATUS_EMPTY
    )
    message = spec.HeadVoteMessage(
        validator_index=validator,
        data=data,
        signature=spec.get_head_vote_signature(upgraded, data, privkeys[validator]),
    )
    statuses = (
        {heze_root: spec.PAYLOAD_VALIDATION_STATUS_VALID}
        if hasattr(spec, "PAYLOAD_VALIDATION_STATUS_VALID")
        else {}
    )
    spec.validate_head_vote_gossip(make_seen(spec), store, message, store.time_ms, statuses)
