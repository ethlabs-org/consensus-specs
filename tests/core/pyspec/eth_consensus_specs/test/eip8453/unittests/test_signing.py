"""Persistent FG signing records and repeated attestation selection."""

from importlib import import_module

import pytest

from eth_consensus_specs.test import context


@pytest.fixture
def spec(preset):
    return import_module(f"eth_consensus_specs.eip8453.{context.DEFAULT_TEST_PRESET}")


@pytest.fixture
def record(spec):
    return spec.FinalitySigningRecord(targets={}, timeouts={}, finality_locks={}, entries={})


def checkpoint(spec, height, root_byte):
    return spec.HeightCheckpoint(height=height, root=spec.Root(bytes([root_byte]) * 32))


def entry_state(spec, height, justified=None, pending=()):
    """The state in which a chain entered ``height``."""
    return spec.BeaconState(
        target_checkpoint=spec.HeightCheckpoint(height=height),
        justified_checkpoint=justified or spec.HeightCheckpoint(),
        pending_finality=spec.PendingFinalities(
            data=[spec.PendingFinality(checkpoint=c) for c in pending]
        ),
    )


def test_repeated_target_suppresses_duplicate(spec, record):
    target = checkpoint(spec, 1, 1)
    empty = spec.HeightCheckpoint.empty()
    entry = entry_state(spec, 1)
    data = spec.get_finality_attestation_data(record, target, entry)
    assert data is not None
    assert data.target == target
    assert not data.timeout
    assert data.finality == empty
    spec.record_finality_attestation(record, data)

    assert record.targets == {target.height: target.root}
    assert record.entries == {target.height: {target.root}}
    assert record.timeouts == {}
    assert record.finality_locks == {}
    assert spec.get_finality_attestation_data(record, target, entry) is None


def test_distinct_targets_timeout_and_suppress_finality(spec, record):
    first = checkpoint(spec, 1, 1)
    second = checkpoint(spec, 1, 2)
    third = checkpoint(spec, 1, 3)
    entry = entry_state(spec, 1)
    data = spec.get_finality_attestation_data(record, first, entry)
    assert data is not None
    spec.record_finality_attestation(record, data)

    assert spec.is_finality_pair_permitted(record, first)
    assert not spec.is_finality_pair_permitted(record, second)
    data = spec.get_finality_attestation_data(record, second, entry)
    assert data is not None
    assert data.target == second
    assert data.timeout
    spec.record_finality_attestation(record, data)

    assert record.targets == {first.height: first.root}
    assert record.timeouts == {first.height: {second.root}}
    assert record.entries == {first.height: {first.root, second.root}}
    assert not spec.is_finality_pair_permitted(record, first)
    assert not spec.is_finality_pair_permitted(record, second)
    assert spec.get_height_vote(record, first) == (first, True)
    for target in (first, second):
        assert spec.get_finality_attestation_data(record, target, entry) is None

    data = spec.get_finality_attestation_data(record, third, entry)
    assert data is not None
    assert data.target == third
    assert data.timeout
    spec.record_finality_attestation(record, data)
    assert record.timeouts == {first.height: {second.root, third.root}}


def test_finality_lock_allows_same_root_and_rejects_distinct_root(spec, record):
    locked = checkpoint(spec, 1, 1)
    conflicting = checkpoint(spec, 1, 2)
    target = checkpoint(spec, 2, 3)
    empty = spec.HeightCheckpoint.empty()
    data = spec.get_finality_attestation_data(record, target, entry_state(spec, 2, locked))
    assert data is not None
    assert data.finality == locked
    spec.record_finality_attestation(record, data)
    assert record.finality_locks == {locked.height: locked.root}

    next_target = checkpoint(spec, 3, 4)
    # A pending justification at the locked height is offered only for the
    # locked root
    data = spec.get_finality_attestation_data(
        record, next_target, entry_state(spec, 3, locked, pending=[locked])
    )
    assert data is not None
    assert data.finality == locked
    data = spec.get_finality_attestation_data(
        record, next_target, entry_state(spec, 3, conflicting, pending=[conflicting])
    )
    assert data is not None
    assert data.finality == empty

    entry = entry_state(spec, 1)
    assert spec.get_finality_attestation_data(record, conflicting, entry) is None
    data = spec.get_finality_attestation_data(record, locked, entry)
    assert data is not None
    assert data.target == locked
    assert not data.timeout


def test_finality_pair_comes_from_entry_state(spec, record):
    older = checkpoint(spec, 1, 1)
    newer = checkpoint(spec, 2, 2)
    blocked = checkpoint(spec, 3, 3)
    empty = spec.HeightCheckpoint.empty()
    record.timeouts[blocked.height] = {checkpoint(spec, 3, 9).root}

    # The previous height is justified: the predecessor is offered, also when
    # its pending entry has gone because a later state finalized it
    entry = entry_state(spec, 4, blocked)
    assert spec.get_finality_vote(record, entry) == empty
    entry = entry_state(spec, 3, newer)
    assert spec.get_finality_vote(record, entry) == newer

    # Otherwise the newest permitted pending justification is offered, and an
    # older one when the record rules out the newest
    entry = entry_state(spec, 5, blocked, pending=[older, newer])
    assert spec.get_finality_vote(record, entry) == newer
    entry = entry_state(spec, 5, blocked, pending=[newer, blocked])
    assert spec.get_finality_vote(record, entry) == newer
    entry = entry_state(spec, 5, blocked, pending=[blocked])
    assert spec.get_finality_vote(record, entry) == empty
    assert spec.get_finality_vote(record, entry_state(spec, 1)) == empty


def test_entry_state_must_be_at_target_height(spec, record):
    with pytest.raises(AssertionError):
        spec.get_finality_attestation_data(record, checkpoint(spec, 2, 1), entry_state(spec, 1))
