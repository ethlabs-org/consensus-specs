"""Gossip validation of FG attestations on ``attestation_{subnet_id}``."""

from dataclasses import fields
from importlib import import_module
from typing import get_origin

import pytest

from eth_consensus_specs.test import context
from eth_consensus_specs.test.helpers.genesis import create_genesis_state
from eth_consensus_specs.test.helpers.keys import privkeys
from eth_consensus_specs.utils import bls

ATTESTER = 1


@pytest.fixture
def spec(preset):
    return import_module(f"eth_consensus_specs.eip8453.{context.DEFAULT_TEST_PRESET}")


@pytest.fixture(autouse=True)
def active_bls():
    previous = bls.bls_active
    bls.bls_active = True
    yield
    bls.bls_active = previous


@pytest.fixture
def state(spec):
    heze = import_module(f"eth_consensus_specs.heze.{context.DEFAULT_TEST_PRESET}")
    balances = [heze.MAX_EFFECTIVE_BALANCE] * 64
    pre = create_genesis_state(heze, balances, heze.MAX_EFFECTIVE_BALANCE)
    state = spec.upgrade_to_eip8453(pre)
    spec.process_slots(state, state.slot + 1)
    return state


def make_store(spec, head_root, head_state):
    block = spec.BeaconBlock(slot=head_state.slot)
    return spec.Store(
        time_ms=spec.Uint64(0),
        genesis_time_ms=spec.Uint64(0),
        justified_checkpoint=spec.HeightCheckpoint(),
        finalized_checkpoint=spec.HeightCheckpoint(),
        proposer_boost_root=spec.Root(),
        blocks={head_root: block},
        block_states={head_root: head_state},
        latest_messages={},
        payloads={},
        payload_timeliness_vote={},
        payload_data_availability_vote={},
        payload_inclusion_list_satisfaction={},
        max_height=spec.Height(0),
        block_receipt_times={},
        payload_receipt_times={},
        latest_confirmed=spec.ForkChoiceNode(
            root=head_root, payload_status=spec.PAYLOAD_STATUS_PENDING
        ),
        saved_confirmations={},
        locks=set(),
        head_vote_messages={},
        head_vote_message_receipt_times={},
        head_vote_committees={},
    )


def make_seen(spec):
    return spec.Seen(
        **{field.name: (get_origin(field.type) or field.type)() for field in fields(spec.Seen)}
    )


def sign(spec, state, data, attester=ATTESTER, committee_index=None):
    if committee_index is None:
        committee_index = attester % spec.ATTESTATION_COMMITTEES_PER_ROUND
    domain = spec.get_domain(
        state, spec.DOMAIN_FINALITY_ATTESTER, spec.get_attestation_epoch(state)
    )
    return spec.SingleAttestation(
        committee_index=committee_index,
        attester_index=attester,
        data=data,
        signature=bls.Sign(privkeys[attester], spec.compute_signing_root(data, domain)),
    )


def validate(spec, monkeypatch, store, attestation, head_root):
    head = spec.ForkChoiceNode(root=head_root, payload_status=spec.PAYLOAD_STATUS_EMPTY)
    monkeypatch.setattr(spec, "get_head", lambda _store: head)
    seen = make_seen(spec)
    subnet = spec.compute_subnet_for_attestation(attestation.committee_index)
    # A time after the first round, so every committee has a started slot
    time_ms = spec.compute_time_at_slot_ms(spec.Uint64(0), spec.Slot(2 * spec.SLOTS_PER_ROUND))
    spec.validate_attestation_gossip(seen, store, attestation, time_ms, subnet)
    return seen


def current_data(spec, state):
    return spec.AttestationData(
        target=state.target_checkpoint.copy(), timeout=False, finality=spec.HeightCheckpoint()
    )


def test_current_target_is_accepted(spec, state, monkeypatch):
    head = spec.Root(b"\x01" * 32)
    store = make_store(spec, head, state)
    seen = validate(spec, monkeypatch, store, sign(spec, state, current_data(spec, state)), head)
    assert len(seen.attestation_validator_rounds) == 1


def test_non_current_target_is_ignored(spec, state, monkeypatch):
    head = spec.Root(b"\x01" * 32)
    store = make_store(spec, head, state)
    data = current_data(spec, state)
    data.target.height += 1
    with pytest.raises(spec.GossipIgnore, match="not for the current target"):
        validate(spec, monkeypatch, store, sign(spec, state, data), head)


def test_head_on_branch_split_before_target_is_ignored(spec, state, monkeypatch):
    # The head's branch has another target, and on it the attester has exited,
    # so checking its committee there would wrongly reject the attestation.
    data = current_data(spec, state)
    attestation = sign(spec, state, data)
    branch = state.copy()
    branch.target_checkpoint.root = spec.Root(b"\x02" * 32)
    branch.validators[ATTESTER].exit_epoch = spec.Epoch(0)
    head = spec.Root(b"\x03" * 32)
    store = make_store(spec, head, branch)
    with pytest.raises(spec.GossipIgnore, match="not for the current target"):
        validate(spec, monkeypatch, store, attestation, head)


def test_wrong_committee_is_rejected(spec, state, monkeypatch):
    head = spec.Root(b"\x01" * 32)
    store = make_store(spec, head, state)
    wrong = (ATTESTER + 1) % spec.ATTESTATION_COMMITTEES_PER_ROUND
    attestation = sign(spec, state, current_data(spec, state), committee_index=wrong)
    with pytest.raises(spec.GossipReject, match="not a member of the committee"):
        validate(spec, monkeypatch, store, attestation, head)
