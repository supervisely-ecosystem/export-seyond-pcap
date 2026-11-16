"""Names of Seyond captures and their items, as the SDK importer writes them.

``sensor_key`` and the capture name pattern are copied from the SDK importer
(``supervisely/convert/pointcloud/seyond_pcap/seyond_pcap_helper.py``) so that the sensors written
here are the ones the importer computes on re-import. Drop the copy and import it from the SDK once
the release with the importer is the app's SDK version.
"""

import os
import re
from collections import Counter
from typing import Iterable, Optional

PCAP_EXT = ".pcap"

# {device}_{location}_{model}_{timestamp}.pcap, e.g. truck-1_forwardRight_falcon_2026-08-26--16-48-00
_CAPTURE_NAME = re.compile(
    r"^(?P<device>.+)_(?P<location>[^_]+)_(?P<model>[^_]+)_"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}--\d{2}-\d{2}-\d{2}(?:[.\-]\d+)?)$"
)

# an item: {capture stem}_{frame in file, 4+ digits}.pcap; the importer may add "_N" to a name
# that is already taken in the dataset
_ITEM_NAME = re.compile(r"^(?P<stem>.+)_(?P<frame>\d{4,})(?:_\d+)?\.pcap$", re.IGNORECASE)


def sensor_key(file_name: str) -> str:
    """The sensor a capture comes from: ``{device}_{location}``, or the file stem for other names."""
    stem = os.path.splitext(os.path.basename(file_name))[0]
    match = _CAPTURE_NAME.match(stem)
    if match is None:
        return stem
    return f"{match['device']}_{match['location']}"


def is_pcap_name(name: Optional[str]) -> bool:
    return bool(name) and name.lower().endswith(PCAP_EXT)


def item_stem(item_name: str) -> Optional[str]:
    """The capture stem of an item name: ``a_b_c_2026-08-26--16-48-00_0042.pcap`` -> ``a_b_c_2026-08-26--16-48-00``."""
    match = _ITEM_NAME.match(item_name)
    return match["stem"] if match else None


def item_frame_number(item_name: str) -> Optional[int]:
    """The frame number in an item name, the frame in the capture the importer named it after."""
    match = _ITEM_NAME.match(item_name)
    return int(match["frame"]) if match else None


def capture_stem(item_names: Iterable[str]) -> Optional[str]:
    """The capture stem shared by the items of one capture: the most common one, ties by name."""
    stems = Counter(s for s in (item_stem(n) for n in item_names) if s)
    if not stems:
        return None
    return sorted(stems.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
