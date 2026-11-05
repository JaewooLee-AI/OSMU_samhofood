"""원문 대조 점검(무료, AI 호출 없음): AI가 쓴 카드 문장 중 원문·브랜드 킷에 근거가 거의 없는 줄을 찾아 알려 준다.

단어 앞 두 글자 기준으로 원문과 겹치는 비율을 본다. 표현만 바꾼 문장은 대부분 통과하고,
'더 알뜰하다', '전담 가이드' 처럼 원문에 없는 내용을 보탠 줄이 걸리도록 맞춘 대략적인 검사이므로 '확인 필요' 경고로만 쓴다.
"""
import re
from typing import Iterable, List, Tuple

# 어느 문장에나 나오는 말(근거 판단에서 뺌)
STOP = {"있어", "있습", "없어", "없이", "해요", "하세", "합니", "드려", "드립", "주세", "하시", "원하", "맞춰", "별도", "함께",
        "먼저", "모두", "바로", "편하", "편안", "꼭", "확인", "필요", "위해", "통해", "대한", "가지", "이런", "그런", "어떤",
        "이렇", "그렇", "많은", "많이", "더욱", "정말", "아주", "가장", "모든", "우리", "여러", "하나", "다양", "직접",
        "있도", "있는", "되는", "하는", "해서", "에서", "으로", "부터", "까지", "에게", "처럼", "보세", "살펴", "알아", "챙기"}


def _keys(text: str) -> List[str]:
    words = re.findall(r"[가-힣A-Za-z0-9]{2,}", text or "")
    return [w[:2] for w in words if w[:2] not in STOP]


def unsupported_lines(cards, sources: Iterable[str], threshold: float = 0.34) -> List[Tuple[int, str]]:
    """(카드 번호, 문장) — 근거 단어 비율이 threshold 미만인 본문 줄"""
    base = set()
    for s in sources:   # 원문 단어의 모든 두 글자 조각 (예: '노쇼핑' → 노쇼·쇼핑) — 앞뒤에 말이 붙어도 근거로 인정
        for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", s or ""):
            base.update(w[i:i + 2] for i in range(len(w) - 1))
    out = []
    for i, c in enumerate(cards, 1):
        for line in (c.body_content or "").splitlines():
            keys = _keys(line)
            if len(keys) < 3:
                continue
            hit = sum(1 for k in keys if k in base)
            if hit / len(keys) < threshold:
                out.append((i, line.strip()))
    return out
