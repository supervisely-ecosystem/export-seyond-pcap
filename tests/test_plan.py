"""Offline tests of how captures and frames are found: no server, a fake API with item infos."""

import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import supervisely as sly  # noqa: E402

from export import plan_export, sensors_config  # noqa: E402
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
    def __init__(self, project_type, datasets, items):
        self._type, self._datasets, self._items = project_type, datasets, items
        self.project = NS(
            get_info_by_id=lambda id: NS(id=id, name="P", type=str(project_type)),
            get_meta=lambda id: {"classes": [], "tags": []},
        )
        self.dataset = NS(get_list=lambda pid, recursive=True: self._datasets)
        self.pointcloud = NS(get_list=lambda ds_id: self._items[ds_id])
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
