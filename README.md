<div align="center" markdown>

# Export Seyond lidar pcap

<p align="center">
  <a href="#Overview">Overview</a> •
  <a href="#How-To-Run">How To Run</a> •
  <a href="#Output">Output</a> •
  <a href="#Import-it-again">Import it again</a> •
  <a href="#Notes">Notes</a>
</p>

[![](https://img.shields.io/badge/supervisely-ecosystem-brightgreen)](https://ecosystem.supervisely.com/apps/supervisely-ecosystem/export-seyond-pcap)
[![](https://img.shields.io/badge/slack-chat-green.svg?logo=slack)](https://supervisely.com/slack)
![GitHub release (latest SemVer)](https://img.shields.io/github/v/release/supervisely-ecosystem/export-seyond-pcap)

</div>

# Overview

Seyond (Innovusion) lidar captures are raw `libpcap` files of the sensor's UDP output: one file holds
a whole recording with hundreds of lidar frames. When such a capture is imported, Supervisely stores
the file once and adds one point cloud item per frame that reuses it; the labeling tool decodes each
item's frame from the file.

This app exports such a **point cloud** or **point cloud episodes** project (or one dataset with its
nested datasets) as it was imported:

- the **original captures**, byte-identical to the imported files, each one fetched and written
  **once**, however many frames (items) use it;
- the **labels** of every frame, with the capture and the frame in the capture each item shows, so
  that importing the folder again puts every label back on the same lidar frame;
- the **sensor mounts** (`sensors.json`) and a **`checksums.sha256`** of the captures.

Captures uploaded to Supervisely are checked against their stored hash after download. Captures linked
from cloud storage are copied too, unless **Links only** is selected (see below).

# How To Run

1. Open the context menu of a **Point Clouds** or **Point Cloud Episodes** project or dataset →
   `Download as` → `Export Seyond lidar pcap`.
2. Choose whether captures linked from cloud storage are copied (default) or only listed
   (**Links only**), and press **Run**.
3. When the task finishes, download the archive from the `Tasks` page, or from `Team Files` →
   `tmp` → `supervisely` → `export` → `Export Seyond lidar pcap` →
   `<task id>_<project id>_<project name>[_<dataset name>].tar`.

The archive is an uncompressed `.tar` (pcap captures do not compress); its size is about the sum of the
captures.

# Output

```
<project name>/
  <dataset path>/<capture>.pcap       original captures, each written once
  sensors.json                        {"sensors": {<sensor>: {"extrinsicMatrix": [12 numbers]}}}
  links.json                          {<capture path>: <link>}   ("Links only" only)
  checksums.sha256                    sha256 of every exported capture (sha256sum format)
  supervisely_labels/
    project_meta.json                 classes and tags of the project
    <dataset path>/labels.json
```

`labels.json` of a dataset:

```json
{
  "frames": [
    {
      "name": "truck-1_forwardRight_falcon_2026-08-26--16-48-00_0042.pcap",
      "frame": 42,
      "file": "dataset/truck-1_forwardRight 2026-08-26 23-47-59/truck-1_forwardRight_falcon_2026-08-26--16-48-00.pcap",
      "frameInFile": 42,
      "timestamp": "2026-08-26T23:48:04.165Z",
      "sensor": "truck-1_forwardRight"
    }
  ],
  "episodeAnnotation": { "...": "point cloud episodes: the episode annotation in Supervisely format" },
  "annotations": { "<item name>": { "...": "point clouds: the annotation of each labeled item" } }
}
```

- `file` is the capture's path relative to `<project name>/`, `frameInFile` the frame inside it
  (from 0), `frame` the item's frame in the project (in an episode, frames continue across chained
  captures).
- `sensor` is `{device}_{location}` from capture names like
  `{device}_{location}_{model}_{YYYY-MM-DD--HH-MM-SS}.pcap`, or the capture's file stem for other names.
- `sensors.json` holds each sensor's extrinsic matrix (sensor to world, row-major 3x4) as the items
  have it. A sensor whose captures have different matrices gets one entry per capture file stem.
- Labels are exported as they are: in world axes, after the sensor's matrix.
- Where a capture is written: the path it was imported from, when the items keep it; otherwise
  `<dataset path>/<capture name>.pcap`, the capture name being the item name without its frame number.

**Links only.** Captures linked from cloud storage (for example `s3://...`) are not copied: `links.json`
lists the path each one would have and its link. Captures uploaded to Supervisely are still copied.
To import such an export again, put each linked capture at its path first.

# Import it again

Upload the extracted `<project name>` folder (or the archive's contents) to Team Files and run
**Auto Import** on it, into a **Point Clouds** or **Point Cloud Episodes** project. The Seyond pcap
importer reads the captures and `sensors.json`, finds `supervisely_labels/`, and attaches the exported
labels to the frames they were on, matched by capture file and frame in the capture (not by item name
or dataset). For point cloud episodes the captures are chained into episodes again by sensor and time.

Requires the Supervisely version with the Seyond pcap importer in Auto Import.

# Notes

- Point clouds are not decoded: the export contains the captures, not PCD files. Exporting decoded
  frames as PCD is a possible later option.
- Episode-level tags (tags of the whole episode, not of an object) are exported in
  `episodeAnnotation`, but the SDK's episode annotation upload does not create them, so they are not
  restored on import yet. Object tags, including frame ranges, are.
- The app uses only the Supervisely SDK (no extra dependencies).

## For developers

- Run locally: put `SERVER_ADDRESS` and `API_TOKEN` in `~/supervisely.env`, set `PROJECT_ID` or
  `DATASET_ID` in `local.env`, and run `python src/main.py` with `src` on `PYTHONPATH`. Without
  `TASK_ID` the archive is uploaded to Team Files but not set as a task output.
- Tests: `python -m pytest tests` (offline, no server).
- The item meta written by the importer includes `source: {file, frameInFile, sensor}`. Instances that
  do not store it yet get the same values derived the way the labeling tool does: items of a dataset
  are grouped by the capture they reuse (`hash` or `link`), and `frameInFile` is the item's
  `meta.frame` minus the smallest `meta.frame` of its group.
- `docker_image` is pinned to `supervisely/base-py-sdk-hardened:6.74.54` (and `supervisely==6.74.54`
  in `dev_requirements.txt`) for now. **Bump both** to the SDK release that ships the Seyond pcap
  importer once it is released, and then replace `src/naming.py`'s copy of `sensor_key` with the SDK's.
