import ercot_lake


def test_imports_without_ingest() -> None:
    assert ercot_lake.CONTRACT_VERSION
