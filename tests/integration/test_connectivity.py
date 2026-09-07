"""Sanity check: confirm the deployed Promise Ledger contract is reachable
and its schema matches this source file before running the full live
lifecycle suite."""

import json

import pytest
from gltest.contracts import get_contract_factory

CONTRACT_ADDRESS = "0x3034F21a81ce366a6ae1489744Aa89897c9D6E21"


@pytest.mark.integration
def test_contract_is_reachable_and_schema_matches():
    factory = get_contract_factory(contract_file_path="promise_ledger.py")
    contract = factory.build_contract(CONTRACT_ADDRESS)
    raw = contract.get_stats().call()
    stats = json.loads(raw) if isinstance(raw, str) else raw
    print("\nlive get_stats():", json.dumps(stats, indent=2))
    assert "total_pools" in stats
    assert "total_commitments" in stats
