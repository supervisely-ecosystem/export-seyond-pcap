"""Offline tests of how captures and frames are found: no server, a fake API with item infos."""

import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import supervisely as sly  # noqa: E402

from export import plan_export, sensors_config, write_archive  # noqa: E402
from naming import capture_stem, item_stem, sensor_key  # noqa: E402

STEM = "truck-1_forwardRight_falcon_2026-08-26--16-48-00"
M1 = [1, 0, 0, 0, 0, 0, 1, 0, 0, -1, 0, 0]
M2 = [1, 0, 0, 5, 0, 1, 0, 0, 0, 0, 1, 0]


def item(id, name, frame, hash=None, link=None, source=None, matrix=M1):
    meta = {"frame": frame, "timestamp": f"t{frame}"}
    if matrix:
        meta["sensorsData"] = {"extrinsicMatrix": matrix}
    if source:
        meta["source"] = source
    return NS(id=id, name=name, hash=hash, link=link, meta=meta, figures_count=0, objects_count=0, tags=[])


class FakeApi:
    def __init__(self, project_type, datasets, items, tables=None):
        self._type, self._datasets, self._items = project_type, datasets, items
        self._tables = tables or {}  # dataset id -> [attachment entities]
        self.project = NS(
            get_info_by_id=lambda id: NS(id=id, name="P", type=str(project_type)),
            get_meta=lambda id: {"classes": [], "tags": []},
        )
        self.dataset = NS(get_list=lambda pid, recursive=True: self._datasets)
        self.pointcloud = NS(
            get_list=lambda ds_id: self._items[ds_id],
            get_list_all_pages=lambda method, data, convert_json_info_cb=None: (
                list(self._tables.get(data["datasetId"], [])) if data.get("type") == "attachment" else []
            ),
        )
        empty = {"framesCount": 0, "objects": [], "frames": [], "tags": [], "description": ""}
        self.pointcloud_episode = NS(annotation=NS(download=lambda ds_id: dict(empty)))


def ds(id, name, parent_id=None):
    return NS(id=id, name=name, parent_id=parent_id)


def test_names():
    assert sensor_key(f"{STEM}.pcap") == "truck-1_forwardRight"
    assert sensor_key("other.pcap") == "other"
    assert item_stem(f"{STEM}_0042.pcap") == STEM
    assert item_stem(f"{STEM}_0042_1.pcap") == STEM  # a name the importer made free
    assert capture_stem([f"{STEM}_0000.pcap", f"{STEM}_0001.pcap", "x.pcap"]) == STEM


def test_derived_episode_chain():
    """No meta.source: path from the dataset path and the item names, frameInFile from meta.frame."""
    second = STEM.replace("16-48-00", "16-48-30")
    items = [item(10 + i, f"{STEM}_{i:04d}.pcap", i, hash="h1") for i in range(3)]
    items += [item(20 + i, f"{second}_{i:04d}.pcap", 3 + i, hash="h2") for i in range(2)]
    api = FakeApi(
        sly.ProjectType.POINT_CLOUD_EPISODES,
        [ds(1, "dataset"), ds(2, "ep 1", 1)],
        {1: [], 2: items},
    )
    plan = plan_export(api, 7)
    assert [c.path for c in plan.captures.values()] == [
        f"dataset/ep 1/{STEM}.pcap",
        f"dataset/ep 1/{second}.pcap",
    ]
    frames = plan.datasets[0].frames
    assert [(f["frame"], f["frameInFile"]) for f in frames] == [(0, 0), (1, 1), (2, 2), (3, 0), (4, 1)]
    assert {f["sensor"] for f in frames} == {"truck-1_forwardRight"}
    assert "episodeAnnotation" in plan.datasets[0].labels


def test_source_wins_and_one_capture_per_hash():
    """meta.source gives the path; the same capture in two datasets is written once."""
    src = lambda n: {"file": f"chain/{STEM}.pcap", "frameInFile": n, "sensor": "s1"}  # noqa: E731
    a = [item(1 + i, f"{STEM}_{i:04d}.pcap", i, hash="h", source=src(i)) for i in range(2)]
    b = [item(5 + i, f"{STEM}_{i:04d}.pcap", i, hash="h") for i in range(2)]
    api = FakeApi(sly.ProjectType.POINT_CLOUDS, [ds(1, "a"), ds(2, "b")], {1: a, 2: b})
    plan = plan_export(api, 7)
    assert len(plan.captures) == 1
    capture = next(iter(plan.captures.values()))
    assert capture.path == f"chain/{STEM}.pcap" and capture.items == 4 and capture.sensor == "s1"
    assert {f["file"] for d in plan.datasets for f in d.frames} == {capture.path}


def test_path_collision_and_links():
    """Two different captures with the same name; a linked one keeps its link."""
    a = [item(1, f"{STEM}_0000.pcap", 0, hash="h1")]
    b = [item(2, f"{STEM}_0000.pcap", 0, link="s3://b/x.pcap")]
    api = FakeApi(sly.ProjectType.POINT_CLOUDS, [ds(1, "a"), ds(2, "b")], {1: a, 2: b})
    plan = plan_export(api, 7)
    paths = [c.path for c in plan.captures.values()]
    assert paths == [f"a/{STEM}.pcap", f"b/{STEM}.pcap"]
    assert [c.link for c in plan.captures.values()] == [None, "s3://b/x.pcap"]


def test_unsafe_source_path_is_ignored():
    a = [item(1, f"{STEM}_0000.pcap", 0, hash="h1", source={"file": "../../etc/x.pcap"})]
    plan = plan_export(FakeApi(sly.ProjectType.POINT_CLOUDS, [ds(1, "a")], {1: a}), 7)
    assert next(iter(plan.captures.values())).path == f"a/{STEM}.pcap"


def test_sensors_config_per_capture_when_matrices_differ():
    other = STEM.replace("16-48-00", "17-00-00")
    a = [item(1, f"{STEM}_0000.pcap", 0, hash="h1", matrix=M1)]
    b = [item(2, f"{other}_0000.pcap", 0, hash="h2", matrix=M2)]
    c = [item(3, "solo_0000.pcap", 0, hash="h3", matrix=None)]
    api = FakeApi(sly.ProjectType.POINT_CLOUDS, [ds(1, "a"), ds(2, "b"), ds(3, "c")], {1: a, 2: b, 3: c})
    config = sensors_config(list(plan_export(api, 7).captures.values()))
    assert config == {"sensors": {STEM: {"extrinsicMatrix": M1}, other: {"extrinsicMatrix": M2}}}


def table(id, serial, hash, kind="anglehvTable"):
    return {"id": id, "hash": hash, "meta": {"kind": kind, "serial": serial}}


ROBIN = "truck-1_forwardCenter_robinW_2026-08-26--16-48-00"


def test_angle_tables_go_next_to_their_captures():
    src = lambda f: {"file": f, "frameInFile": 0, "sensor": "s"}  # noqa: E731
    a = [item(1, f"{ROBIN}_0000.pcap", 0, hash="h1", source=src(f"{ROBIN}.pcap"))]
    b = [item(2, f"{STEM}_0000.pcap", 0, hash="h2", source=src(f"{STEM}.pcap"))]
    tables = {
        1: [table(51, "532102500133", "T1"), table(52, "x", "Z", kind="other"), table(53, "../up", "U")],
        # the same sensor, the same table, in a second dataset with a capture in the same folder
        2: [table(54, "532102500133", "T1")],
    }
    api = FakeApi(sly.ProjectType.POINT_CLOUD_EPISODES, [ds(1, "a"), ds(2, "b")], {1: a, 2: b}, tables)
    plan = plan_export(api, 7)
    assert [c.path for c in plan.captures.values()] == [f"{ROBIN}.pcap", f"{STEM}.pcap"]
    assert {f: {s: t.entity_id for s, t in ts.items()} for f, ts in plan.tables.items()} == {
        "": {"532102500133": 51}
    }


def test_a_different_table_for_a_sensor_gets_its_own_folder():
    """Two recordings of one sensor in one folder, the sensor recalibrated in between."""
    other = ROBIN.replace("16-48-00", "17-00-00")
    src = lambda f: {"file": f, "frameInFile": 0, "sensor": "s"}  # noqa: E731
    a = [item(1, f"{ROBIN}_0000.pcap", 0, hash="h1", source=src(f"{ROBIN}.pcap"))]
    b = [item(2, f"{other}_0000.pcap", 0, hash="h2", source=src(f"{other}.pcap"))]
    tables = {1: [table(51, "532102500133", "T1")], 2: [table(52, "532102500133", "T2")]}
    api = FakeApi(sly.ProjectType.POINT_CLOUD_EPISODES, [ds(1, "a"), ds(2, "b")], {1: a, 2: b}, tables)
    plan = plan_export(api, 7)
    paths = [c.path for c in plan.captures.values()]
    assert paths == [f"{ROBIN}.pcap", f"tables-2/b/{other}.pcap"]
    assert {f: {s: t.hash for s, t in ts.items()} for f, ts in plan.tables.items()} == {
        "": {"532102500133": "T1"},
        "tables-2/b": {"532102500133": "T2"},
    }
    # the labels follow the capture to its folder
    assert plan.datasets[1].frames[0]["file"] == f"tables-2/b/{other}.pcap"


def test_tables_are_written_and_checked(tmp_path):
    import base64
    import hashlib
    import tarfile

    data = b"angle table bytes"
    good = base64.b64encode(hashlib.sha256(data).digest()).decode()
    src = {"file": f"rec/{ROBIN}.pcap", "frameInFile": 0, "sensor": "s"}
    a = [item(1, f"{ROBIN}_0000.pcap", 0, link="s3://b/r.pcap", source=src)]
    api = FakeApi(sly.ProjectType.POINT_CLOUDS, [ds(1, "a")], {1: a}, {1: [table(51, "532102500133", good)]})

    def download_path(entity_id, path):
        assert entity_id == 51
        with open(path, "wb") as f:
            f.write(data)

    api.pointcloud.download_path = download_path
    plan = plan_export(api, 7)
    archive = tmp_path / "x.tar"
    summary = write_archive(api, plan, str(archive), str(tmp_path), links_only=True)
    assert summary["angle_tables"] == 1
    with tarfile.open(archive) as tar:
        assert tar.extractfile("P/rec/532102500133.anglehv").read() == data
        sums = tar.extractfile("P/checksums.sha256").read().decode()
    assert sums == f"{hashlib.sha256(data).hexdigest()}  rec/532102500133.anglehv\n"
    assert not list(tmp_path.glob("table-in-progress*"))
