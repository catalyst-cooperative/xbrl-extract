from collections import namedtuple
from pathlib import Path

import pytest

from ferc_xbrl_extractor.cli import (  # TODO (daz) move this function out of CLI!
    TAXONOMY_MAP,
)
from ferc_xbrl_extractor.xbrl import ExtractOutput, extract

Dataset = namedtuple("Dataset", ["form", "year"])

DATASETS = [Dataset(form=f, year=y) for f in {1, 2, 6, 60, 714} for y in {2021, 2022}]


@pytest.fixture(scope="session")
def metadata_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("metadata")


@pytest.fixture(scope="session")
def data_dir(request) -> Path:
    return request.config.getoption("--integration-data-dir")


@pytest.fixture(
    scope="session",
    params=DATASETS,
    ids=[f"form{ds.form}_{ds.year}" for ds in DATASETS],
)
def extracted(metadata_dir, data_dir, request) -> ExtractOutput:
    form, year = request.param
    return extract(
        taxonomy_path=TAXONOMY_MAP[form],
        form_number=form,
        db_path="path",
        archive_path=None,
        metadata_path=metadata_dir / "metadata.json",
        datapackage_path=metadata_dir / "datapackage.json",
        instance_path=data_dir / f"ferc{form}-xbrl-{year}.zip",
        workers=None,
        batch_size=2,
    )


def test_lost_facts_pct(extracted, request):
    tables, instances, filings, stats = extracted
    total_facts = sum(len(i.fact_id_counts) for i in instances)
    total_used_facts = sum(len(f_ids) for f_ids in stats["fact_ids"].values())

    used_fact_ratio = total_used_facts / total_facts

    if "form6_" in request.node.name:
        # We have unallocated data for Form 6 for some reason.
        total_threshold = 0.9
        per_filing_threshold = 0.85
        # Assert that this is < 0.95 so we remember to fix this test once we
        # fix the bug. We don't use xfail here because the parametrization is
        # at the *fixture* level, and only the lost facts tests should fail
        # for form 6.
        assert used_fact_ratio > total_threshold and used_fact_ratio <= 0.95
    else:
        total_threshold = 0.99
        per_filing_threshold = 0.95
        assert used_fact_ratio > total_threshold and used_fact_ratio <= 1

    for instance in instances:
        instance_used_ratio = len(stats["fact_ids"][instance.filing_name]) / len(
            instance.fact_id_counts
        )
        assert instance_used_ratio > per_filing_threshold and instance_used_ratio <= 1


def test_primary_key_uniqueness(extracted):
    tables, _instances, filings, _stats = extracted

    for table_name, table in tables.items():
        if table.instant:
            date_cols = ["date"]
        else:
            date_cols = ["start_date", "end_date"]
        primary_key_cols = ["entity_id", "filing_name"] + date_cols + table.axes
        filing = filings[table_name]
        if filing.empty:
            continue
        assert set(filing.index.names) == set(primary_key_cols)
        assert not filing.index.duplicated().any()


def test_null_values(extracted):
    tables, _instances, filings, _stats = extracted

    for table_name, table in tables.items():
        filing = filings[table_name]
        if filing.empty:
            continue
        # every row has at least one non-null value
        assert filing.notna().sum(axis=1).all()
