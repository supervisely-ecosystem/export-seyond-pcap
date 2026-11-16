"""Export a Seyond lidar pcap project: the original captures, once each, with their labels.

A Seyond capture is one libpcap file with many lidar frames. Supervisely stores it once and adds an
item per frame that reuses it (the same ``hash``, or the same ``link`` for a capture in cloud
storage). This module finds the captures behind the items, fetches each one once, and writes::

    <project name>/
      <capture paths>                 the captures, byte-identical to what was imported
      sensors.json                    {"sensors": {<sensor>: {"extrinsicMatrix": [12 numbers]}}}
      links.json                      {<capture path>: <link>}, "Links only" mode
      supervisely_labels/
        project_meta.json
        <dataset path>/labels.json    {"frames": [...], "episodeAnnotation" | "annotations": ...}
      checksums.sha256

The capture paths are what the SDK importer computes when the folder is imported again, so the
labels land on the same (capture, frame in capture) frames.
"""

import base64
import hashlib
import io
import json
import os
import posixpath
import tarfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import supervisely as sly
from supervisely.video_annotation.key_id_map import KeyIdMap

from naming import (
    PCAP_EXT,
    capture_stem,
    is_pcap_name,
    item_frame_number,
    sensor_key,
)

LABELS_DIR_NAME = "supervisely_labels"
LABELS_META_NAME = "project_meta.json"
LABELS_FILE_NAME = "labels.json"
SENSORS_CONFIG_NAME = "sensors.json"
LINKS_FILE_NAME = "links.json"
CHECKSUMS_FILE_NAME = "checksums.sha256"

DOWNLOAD_ATTEMPTS = 3
ANNOTATIONS_BATCH = 50
# classic libpcap (both byte orders, micro and nano seconds) and pcapng
_PCAP_MAGICS = {
    bytes.fromhex(m) for m in ("d4c3b2a1", "a1b2c3d4", "4d3cb2a1", "a1b23c4d", "0a0d0d0a")
}


@dataclass
class Capture:
    """One stored capture file and how it is exported."""

    key: Tuple[str, str]  # ("hash", <hash>) or ("link", <link>)
    item_id: int  # an item to download the capture from
    path: str  # where it is written, relative to the project folder
    sensor: str
    items: int = 0
    matrices: Counter = field(default_factory=Counter)
    sha256: Optional[str] = None
    size: Optional[int] = None

    @property
    def link(self) -> Optional[str]:
        return self.key[1] if self.key[0] == "link" else None

    @property
    def hash(self) -> Optional[str]:
        return self.key[1] if self.key[0] == "hash" else None

    @property
    def matrix(self) -> Optional[Tuple[float, ...]]:
        return self.matrices.most_common(1)[0][0] if self.matrices else None


@dataclass
class DatasetExport:
    path: str  # the dataset path in the project, "parent/child"
    info: sly.DatasetInfo
    frames: List[dict]
    labels: dict  # {"episodeAnnotation": ...} or {"annotations": {...}}


@dataclass
class ExportPlan:
    project: sly.ProjectInfo
    meta: sly.ProjectMeta
    captures: Dict[Tuple[str, str], Capture]
    datasets: List[DatasetExport]
    skipped_items: int = 0


# ---------------------------------------------------------------- plan


def dataset_paths(
    api: sly.Api, project_id: int, dataset_id: Optional[int] = None
) -> List[Tuple[str, sly.DatasetInfo]]:
    """Datasets to export with their paths in the project; a dataset brings its nested ones."""
    infos = api.dataset.get_list(project_id, recursive=True)
    by_id = {ds.id: ds for ds in infos}

    def chain(ds):
        while ds is not None:
            yield ds
            ds = by_id.get(ds.parent_id)

    result = []
    for ds in infos:
        parents = list(chain(ds))
        if dataset_id is not None and dataset_id not in {p.id for p in parents}:
            continue
        result.append(("/".join(p.name for p in reversed(parents)), ds))
    return sorted(result, key=lambda pair: pair[0])


def _capture_key(item) -> Optional[Tuple[str, str]]:
    if item.link:
        return ("link", item.link)
    if item.hash:
        return ("hash", item.hash)
    return None


def _safe_relative(path) -> Optional[str]:
    """A relative path inside the export folder, or None."""
    if not isinstance(path, str) or not path.strip():
        return None
    path = posixpath.normpath(path.replace("\\", "/")).lstrip("/")
    if path in ("", ".") or path.split("/")[0] == ".." or not is_pcap_name(path):
        return None
    return path


def _free_path(path: str, dataset_path: str, taken: set) -> str:
    """A capture path no other capture uses: as is, under the dataset path, or under ``dup-N/``."""
    if path not in taken:
        return path
    candidate = f"{dataset_path}/{path}"
    n = 1
    while candidate in taken:
        n += 1
        candidate = f"dup-{n}/{dataset_path}/{path}"
    sly.logger.warning(f"Two different captures are both '{path}': one is written as '{candidate}'.")
    return candidate


def plan_export(api: sly.Api, project_id: int, dataset_id: Optional[int] = None) -> ExportPlan:
    """Find the captures behind the items and every item's (capture, frame in capture)."""
    project = api.project.get_info_by_id(project_id)
    if project is None:
        raise ValueError(f"Project {project_id} is not found or is not accessible")
    if project.type not in (str(sly.ProjectType.POINT_CLOUDS), str(sly.ProjectType.POINT_CLOUD_EPISODES)):
        raise ValueError(f"Project type '{project.type}' is not supported: point clouds or point cloud episodes only")
    is_episodes = project.type == str(sly.ProjectType.POINT_CLOUD_EPISODES)
    meta = sly.ProjectMeta.from_json(api.project.get_meta(project.id))

    plan = ExportPlan(project, meta, {}, [])
    taken_paths = set()
    name_mismatch = 0
    for ds_path, ds in dataset_paths(api, project.id, dataset_id):
        items = api.pointcloud.get_list(ds.id)
        groups: Dict[Tuple[str, str], list] = defaultdict(list)
        for item in items:
            key = _capture_key(item)
            if key is None or not (is_pcap_name(item.name) or is_pcap_name(item.link)):
                plan.skipped_items += 1
                continue
            groups[key].append(item)
        if not groups:
            continue

        frames, pcap_items = [], []
        for key, group in groups.items():
            metas = [item.meta or {} for item in group]
            sources = [m.get("source") if isinstance(m.get("source"), dict) else {} for m in metas]
            numbers = [m.get("frame") for m in metas]
            numbers = [
                n if isinstance(n, int) else item_frame_number(item.name) or 0
                for n, item in zip(numbers, group)
            ]
            # the labeling tool's rule: the frame in the file counts from the dataset's first
            # item of that file
            first = min(numbers)

            capture = plan.captures.get(key)
            if capture is None:
                source_file = next((s["file"] for s in sources if s.get("file")), None)
                source_file = _safe_relative(source_file)
                if source_file is None:
                    stem = capture_stem(i.name for i in group)
                    if stem is None:
                        base = os.path.basename(key[1]) if key[0] == "link" else group[0].name
                        stem = os.path.splitext(base)[0]
                    source_file = f"{ds_path}/{stem}{PCAP_EXT}"
                path = _free_path(source_file, ds_path, taken_paths)
                taken_paths.add(path)
                sensor = next((s["sensor"] for s in sources if s.get("sensor")), None)
                capture = Capture(key, group[0].id, path, sensor or sensor_key(path))
                plan.captures[key] = capture

            for item, item_meta, source, number in zip(group, metas, sources, numbers):
                frame_in_file = source.get("frameInFile")
                if not isinstance(frame_in_file, int):
                    frame_in_file = number - first
                if item_frame_number(item.name) not in (None, frame_in_file):
                    name_mismatch += 1
                matrix = (item_meta.get("sensorsData") or {}).get("extrinsicMatrix")
                if matrix:
                    capture.matrices[tuple(matrix)] += 1
                capture.items += 1
                frames.append(
                    {
                        "name": item.name,
                        "frame": number,
                        "file": capture.path,
                        "frameInFile": frame_in_file,
                        "timestamp": item_meta.get("timestamp"),
                        "sensor": source.get("sensor") or capture.sensor,
                    }
                )
                pcap_items.append(item)

        if is_episodes:
            frames.sort(key=lambda f: (f["frame"], f["name"]))
            labels = {"episodeAnnotation": _episode_annotation(api, ds.id, meta)}
        else:
            frames.sort(key=lambda f: (f["file"], f["frameInFile"], f["name"]))
            labels = {"annotations": _pointcloud_annotations(api, ds.id, pcap_items, meta)}
        plan.datasets.append(DatasetExport(ds_path, ds, frames, labels))

    if name_mismatch:
        sly.logger.warning(
            f"{name_mismatch} item(s) have a frame number in their name that differs from their "
            "frame in the capture: the frame in the capture follows the items' meta.frame."
        )
    if plan.skipped_items:
        sly.logger.warning(f"Skipped {plan.skipped_items} item(s) that are not Seyond pcap frames.")
    for capture in plan.captures.values():
        if len(capture.matrices) > 1:
            sly.logger.warning(
                f"Items of '{capture.path}' have {len(capture.matrices)} different extrinsic matrices: "
                "the most common one is exported."
            )
    return plan


def _episode_annotation(api: sly.Api, dataset_id: int, meta: sly.ProjectMeta) -> dict:
    ann_json = api.pointcloud_episode.annotation.download(dataset_id)
    return sly.PointcloudEpisodeAnnotation.from_json(ann_json, meta, KeyIdMap()).to_json()


def _pointcloud_annotations(api: sly.Api, dataset_id: int, items: list, meta: sly.ProjectMeta) -> dict:
    """Annotations of the labeled items only, by item name."""
    labeled = [i for i in items if i.figures_count or getattr(i, "objects_count", 0) or i.tags]
    result = {}
    for start in range(0, len(labeled), ANNOTATIONS_BATCH):
        batch = labeled[start : start + ANNOTATIONS_BATCH]
        ann_jsons = api.pointcloud.annotation.download_bulk(dataset_id, [i.id for i in batch])
        for item, ann_json in zip(batch, ann_jsons):
            if ann_json.get("name", item.name) != item.name:
                raise RuntimeError("Point cloud annotations came back in a different order")
            ann = sly.PointcloudAnnotation.from_json(ann_json, meta, KeyIdMap())
            result[item.name] = ann.to_json()
    return result


def sensors_config(captures: List[Capture]) -> dict:
    """``sensors.json`` for the importer: a sensor's matrix, or a capture's when they differ.

    The importer looks a capture's matrix up by its sensor, then by its file stem, so a sensor whose
    captures do not all share one matrix gets an entry per capture stem instead.
    """
    by_sensor: Dict[str, List[Capture]] = defaultdict(list)
    for capture in captures:
        by_sensor[capture.sensor].append(capture)
    sensors = {}
    for sensor, group in sorted(by_sensor.items()):
        matrices = {c.matrix for c in group}
        if len(matrices) == 1 and None not in matrices:
            sensors[sensor] = {"extrinsicMatrix": list(matrices.pop())}
            continue
        for capture in group:
            if capture.matrix is not None:
                stem = os.path.splitext(os.path.basename(capture.path))[0]
                sensors[stem] = {"extrinsicMatrix": list(capture.matrix)}
    return {"sensors": sensors} if sensors else {}


# ---------------------------------------------------------------- captures


def _sha256(path: str) -> Tuple[str, str]:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), base64.b64encode(digest.digest()).decode()


def fetch_capture(api: sly.Api, capture: Capture, path: str) -> None:
    """Download the stored original of a capture once and check it is the file that was imported.

    A capture uploaded by hash must have that hash (base64 of its sha256); a linked one must be a pcap.
    """
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            started = time.monotonic()
            api.pointcloud.download_path(capture.item_id, path)
            hex_digest, b64_digest = _sha256(path)
            if capture.hash is not None and b64_digest != capture.hash:
                raise RuntimeError(f"the downloaded file has hash {b64_digest}, expected {capture.hash}")
            with open(path, "rb") as f:
                if f.read(4) not in _PCAP_MAGICS:
                    raise RuntimeError("the downloaded file is not a pcap file")
            capture.sha256, capture.size = hex_digest, os.path.getsize(path)
            elapsed = max(time.monotonic() - started, 1e-3)
            sly.logger.info(
                f"Downloaded '{capture.path}': {capture.size / 2**20:.1f} MB "
                f"in {elapsed:.0f} s, {capture.items} item(s)"
            )
            return
        except Exception as e:  # noqa: BLE001 - retried, then raised
            sly.fs.silent_remove(path)
            if attempt == DOWNLOAD_ATTEMPTS:
                raise RuntimeError(
                    f"Cannot download capture '{capture.path}' (item {capture.item_id}): {e}"
                ) from e
            sly.logger.warning(f"Download of '{capture.path}' failed ({e}), retrying...")
            time.sleep(5 * attempt)


def _add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _json_bytes(data) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_archive(
    api: sly.Api, plan: ExportPlan, archive_path: str, work_dir: str, links_only: bool = False
) -> dict:
    """Write the export as an uncompressed tar, fetching each capture once; return a summary.

    The captures go into the tar one by one, so the disk holds the archive and a single capture.
    """
    root = plan.project.name
    captures = list(plan.captures.values())
    to_fetch = [c for c in captures if not (links_only and c.link is not None)]
    links = {c.path: c.link for c in captures if links_only and c.link is not None}
    temp_path = os.path.join(work_dir, f"capture-in-progress{PCAP_EXT}")

    progress = sly.tqdm_sly(desc="Downloading captures", total=len(to_fetch))
    with tarfile.open(archive_path, "w", format=tarfile.PAX_FORMAT) as tar:
        for capture in to_fetch:
            fetch_capture(api, capture, temp_path)
            tar.add(temp_path, arcname=f"{root}/{capture.path}")
            sly.fs.silent_remove(temp_path)
            progress.update(1)

        sensors = sensors_config(captures)
        if sensors:
            _add_bytes(tar, f"{root}/{SENSORS_CONFIG_NAME}", _json_bytes(sensors))
        if links:
            _add_bytes(tar, f"{root}/{LINKS_FILE_NAME}", _json_bytes(links))
        labels_root = f"{root}/{LABELS_DIR_NAME}"
        _add_bytes(tar, f"{labels_root}/{LABELS_META_NAME}", _json_bytes(plan.meta.to_json()))
        for ds in plan.datasets:
            labels = {"frames": ds.frames, **ds.labels}
            _add_bytes(tar, f"{labels_root}/{ds.path}/{LABELS_FILE_NAME}", _json_bytes(labels))
        if to_fetch:
            checksums = "".join(f"{c.sha256}  {c.path}\n" for c in to_fetch)
            _add_bytes(tar, f"{root}/{CHECKSUMS_FILE_NAME}", checksums.encode("utf-8"))

    summary = {
        "captures": len(captures),
        "downloaded": len(to_fetch),
        "links": len(links),
        "bytes": sum(c.size or 0 for c in to_fetch),
        "datasets": len(plan.datasets),
        "frames": sum(len(ds.frames) for ds in plan.datasets),
        "archive_bytes": os.path.getsize(archive_path),
    }
    sly.logger.info(f"Export written to '{archive_path}': {summary}")
    return summary
