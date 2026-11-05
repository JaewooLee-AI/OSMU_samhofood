"""`[IMAGE: 키]` 태그 분할 — formatting.py가 사진 위치를 다룰 때 쓰는 최소 모듈 (OSMU_cjc에서 가져옴)."""
from __future__ import annotations

import re

IMAGE_TAG_RE = re.compile(r"\[IMAGE:\s*([^\]]+)\]")


def split_segments(content: str):
    """초안을 문서 순서대로 ('text', 블록) / ('image', 키) 로 나눈다."""
    segments = []
    last_end = 0
    for match in IMAGE_TAG_RE.finditer(content or ""):
        text_part = content[last_end:match.start()]
        if text_part.strip():
            segments.append(("text", text_part))
        segments.append(("image", match.group(1).strip()))
        last_end = match.end()
    tail = (content or "")[last_end:]
    if tail.strip():
        segments.append(("text", tail))
    return segments
