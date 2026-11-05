"""제목 강조 표기: `*강조어*` — 카드에서는 형광펜으로 칠하고, 다른 곳(쇼츠 자막·HTML·검토 시안)에서는 별표만 지운다."""
import re
from typing import List, Tuple

STAR_RE = re.compile(r"\*+([^*\n]+?)\*+")
# 강조 표기가 없을 때 자동으로 칠할 수치 (예: 97.14점, 92%, 1만 2천 명, 3가지)
NUMBER_RE = re.compile(r"\d[\d,.]*\s*(?:억|만|천)?\s*(?:원|시간|점|%|명|분|주|년|개월|개|곳|배|가지|일|회|위)")


def strip(text: str) -> str:
    return STAR_RE.sub(r"\1", text or "").replace("*", "")


def parse(text: str) -> Tuple[str, List[str]]:
    """(별표를 지운 문장, 강조어 목록)"""
    terms = [m.group(1).strip() for m in STAR_RE.finditer(text or "") if m.group(1).strip()]
    return strip(text), terms


def auto_terms(text: str, limit: int = 1) -> List[str]:
    """강조 표기가 없을 때 쓸 수치 강조어"""
    return [m.group(0).strip() for m in NUMBER_RE.finditer(text or "")][:limit]


def spans(text: str, terms: List[str]) -> List[Tuple[int, int]]:
    """문장 안에서 강조어 위치 (겹치지 않게 처음 나온 곳만)"""
    out = []
    for t in terms:
        i = (text or "").find(t)
        if i >= 0 and not any(a < i + len(t) and i < b for a, b in out):
            out.append((i, i + len(t)))
    return out
