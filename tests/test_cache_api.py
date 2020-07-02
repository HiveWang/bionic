import pytest

import pickle

import bionic as bn
from bionic import interpret
from bionic.persistence import (
    path_from_url,
    is_file_url,
    is_gcs_url,
    bucket_and_object_names_from_gs_url,
)
from bionic.util import get_gcs_client_without_warnings


class CacheTester:
    """
    A helper class for testing changes to Bionic's cache.

    Tracks the current and previous states of the Cache API, allow us to express tests
    in terms of changes between states.
    """

    def __init__(self, flow, tier=["local", "cloud"]):
        self.flow = flow
        self._old_entries = set()

        self._tiers = interpret.str_or_seq_as_list(tier)

    def expect_new_entries(self, *expected_new_entity_names):
        cur_entries = set(self._get_entries())
        assert cur_entries.issuperset(self._old_entries)
        new_entries = set(cur_entries) - self._old_entries
        self._old_entries = cur_entries

        new_entity_names = {entry.entity for entry in new_entries}
        assert new_entity_names == set(expected_new_entity_names)

        for entry in new_entries:
            self._validate_entry(entry)

    def expect_zero_entries(self):
        assert list(self._get_entries()) == []

    def _get_entries(self):
        return [
            entry
            for entry in self.flow.cache.get_entries()
            if entry.tier in self._tiers
        ]

    def _validate_entry(self, entry):
        artifact_bytes = read_bytes_from_url(entry.artifact_url)
        value = pickle.loads(artifact_bytes)
        assert value == self.flow.get(entry.entity)

        if entry.tier == "local":
            artifact_path_bytes = entry.artifact_path.read_bytes()
            assert artifact_path_bytes == artifact_bytes
        else:
            assert entry.artifact_path is None

        # We won't make too many assumptions about the format of the metadata, but we
        # can check that it contains the entity name. (Unfortunately it won't
        # necessarily contain the absolute artifact URL; it may be a relative URL
        # instead.)
        metadata_str = read_bytes_from_url(entry.metadata_url).decode("utf-8")
        assert entry.entity in metadata_str


def read_bytes_from_url(url):
    """Reads the contents of a URL and returns them as a bytes object."""

    if is_file_url(url):
        path = path_from_url(url)
        return path.read_bytes()
    elif is_gcs_url(url):
        gcs_client = get_gcs_client_without_warnings()
        bucket_name, object_name = bucket_and_object_names_from_gs_url(url)
        bucket = gcs_client.get_bucket(bucket_name)
        blob = bucket.get_blob(object_name)
        return blob.download_as_string()
    else:
        raise AssertionError(f"Unexpected scheme in URL: {url}")


@pytest.fixture(scope="function")
def preset_flow(builder):
    builder.assign("x", 2)
    builder.assign("y", 3)

    @builder
    def xy(x, y):
        return x * y

    @builder
    def xy_squared(xy):
        return xy ** 2

    return builder.build()


def test_get_entries(preset_flow):
    tester = CacheTester(preset_flow)

    tester.expect_zero_entries()

    tester.flow.get("x")
    tester.expect_new_entries("x")

    tester.flow.get("xy")
    tester.expect_new_entries("y", "xy")

    tester.flow.get("xy_squared")
    tester.expect_new_entries("xy_squared")

    tester.flow = tester.flow.setting("x", 4)
    tester.flow.get("xy_squared")
    tester.expect_new_entries("x", "xy", "xy_squared")

    builder = tester.flow.to_builder()

    @builder  # noqa: F811
    @bn.version(1)
    def xy(x, y):  # noqa: F811
        return x ** y

    tester.flow = builder.build()

    tester.flow.get("xy_squared")
    tester.expect_new_entries("xy", "xy_squared")


# It would be nice if we could parameterize the above tests to run with or without GCS.
# However, it doesn't seem to be possible to have a parametrized fixture where only some
# of the variations depend on other fixtures; this is important because the GCS fixtures
# have important setup/teardown properties that we only want to trigger if GCS is
# enabled. (In theory it seems like `request.getfixturevalue` should be able to do
# this, but it has some kind of interaction with the parametrization of
# `parallel_execution_enabled` and breaks.) I think the way forward might be to make
# the GCS setup/teardown into `autouse` fixtures that are directly activated by the GCS
# command line flag.
@pytest.mark.needs_gcs
def test_cache_on_gcs(gcs_builder):
    builder = gcs_builder

    builder.assign("a", 1)

    @builder
    def b(a):
        return a + 1

    @builder
    def c(b):
        return b + 1

    flow = builder.build()

    local_tester = CacheTester(flow, tier="local")
    cloud_tester = CacheTester(flow, tier="cloud")
    total_tester = CacheTester(flow, tier=["local", "cloud"])

    local_tester.expect_zero_entries()
    cloud_tester.expect_zero_entries()
    total_tester.expect_zero_entries()

    flow.get("b")
    local_tester.expect_new_entries("a", "b")
    cloud_tester.expect_new_entries("a", "b")
    total_tester.expect_new_entries("a", "a", "b", "b")

    flow.get("c")
    local_tester.expect_new_entries("c")
    cloud_tester.expect_new_entries("c")
    total_tester.expect_new_entries("c", "c")
