"""원고 서식 레이어 (요청 4 — 굵게·형광·소제목·인용구·구분선·요약 박스·사진 자리).

왜 본문에 서식 기호를 섞지 않는가
--------------------------------
생성 뒤의 단계들 — 컴플라이언스 문구 교체, SEO 키워드 보정, 맞춤법 교정, 글자 수·키워드
밀도 측정, 유사문서 비교, 인스타·X·쇼츠 변환 — 은 모두 평문 본문을 전제로 돕니다. 기호가 섞이면
교정 문구를 못 찾거나 기호를 깨뜨립니다. 그래서:

1. 초안을 쓰는 **같은 호출**에서 서식 기호까지 받고(`instruction`),
2. 받자마자 기호를 떼어 서식 목록으로 옮기고(`extract` — 본문은 평문으로 계속 흘러감),
3. 모든 교정이 끝난 본문에 서식을 **문장으로 찾아** 다시 붙입니다(`bind`). 교정으로 문장이 바뀌어
   자리를 못 찾은 서식은 버립니다 — 엉뚱한 곳에 붙이는 것보다 안전합니다.

campaigns.content_format(JSON)에 저장하는 모양::

    {"version": 1, "template": "basic", "strength": "standard", "source": "generate",
     "items": [
        {"id": 1, "type": "heading", "level": 2, "text": "<문단 전체>"},
        {"id": 2, "type": "bold" | "highlight", "text": "<문단 안의 구절>"},
        {"id": 3, "type": "quote", "text": "<문단 전체>", "style": "quote"},
        {"id": 4, "type": "divider", "before": "<다음 문단 전체>", "style": "line"},
        {"id": 5, "type": "box", "start": "<첫 문단>", "end": "<끝 문단>", "title": "핵심 정리"},
        {"id": 6, "type": "photo_hint", "after": "<앞 문단 또는 ''>", "caption": "..."},
        {"id": 7, "type": "photo_caption", "path": "<사진 경로>", "caption": "..."},
     ]}

글머리·번호 목록(`- `, `1. `)은 평문에도 자연스러워 본문에 그대로 둡니다. 네이버 HTML로 바꿀 때만 목록이 됩니다.
네이버 에디터 툴바는 누르지 않습니다(작업계획 결정 3) — 서식은 붙여넣는 HTML에만 담습니다.
"""
from __future__ import annotations

import difflib
import html
import json
import re
from typing import Dict, List, Optional, Tuple

from services.photo_placement import IMAGE_TAG_RE, split_segments

FORMAT_VERSION = 1

TEMPLATES = {
    "basic": {"label": "기본형", "desc": "소제목으로 구간을 나누고 핵심 문장을 강조, 인용구 하나로 핵심 메시지를 세웁니다."},
    "info": {"label": "정보형", "desc": "소제목·번호 목록 중심, 수치와 조건을 강조하고 글 끝에 '핵심 정리' 박스를 둡니다."},
    "review": {"label": "후기형", "desc": "현장·고객의 말을 인용구로, 사진이 들어갈 자리를 제안하고 감정이 담긴 문장을 형광으로 표시합니다."},
}
TEMPLATE_ORDER = ("basic", "info", "review")

STRENGTHS = {
    "none": {"label": "사용 안 함", "desc": "지금처럼 문단으로만 씁니다."},
    "minimal": {"label": "최소", "desc": "소제목과 꼭 필요한 강조만."},
    "standard": {"label": "표준", "desc": "소제목·강조·인용구 1~2개·구분선·요약 박스."},
    "rich": {"label": "풍부", "desc": "표준보다 강조와 구분을 더 자주 씁니다(인용구는 2개까지)."},
}
STRENGTH_ORDER = ("none", "minimal", "standard", "rich")
DEFAULT_TEMPLATE = "basic"
DEFAULT_STRENGTH = "standard"

QUOTE_STYLES = {"quote": "따옴표형", "line": "선형", "bubble": "말풍선형"}
DIVIDER_STYLES = {"line": "기본", "dotted": "점선", "bold": "굵은 선"}

# 네이버 붙여넣기 방식 (app_state "naver_format_mode"): 'editor' = HTML 인용·구분선·표를 그대로 붙여
# 에디터가 자기 서식으로 바꾸길 기대, 'symbol' = 인용구·구분선·박스를 ❝ ❞ · ──── 같은 기호 줄로 대체.
NAVER_FORMAT_MODES = {"editor": "에디터 서식 우선", "symbol": "기호로 대체"}
NAVER_FORMAT_STATE_KEY = "naver_format_mode"
DEFAULT_NAVER_FORMAT_MODE = "editor"

HIGHLIGHT_COLOR = "#FFF3A3"
PER_PARAGRAPH_EMPHASIS = 2

_TYPE_LABELS = {
    "heading": "소제목", "bold": "굵게", "highlight": "형광", "quote": "인용구", "divider": "구분선",
    "box": "요약 박스", "photo_hint": "사진 자리", "photo_caption": "사진 설명",
}


def type_label(kind: str) -> str:
    return _TYPE_LABELS.get(kind, kind)


# --- 설정 해석 ---------------------------------------------------------------------

def resolve(campaign_strength: Optional[str], brand_kit: dict) -> Dict[str, str]:
    """글의 서식 강도(없으면 킷 기본)와 킷 템플릿."""
    strength = campaign_strength if campaign_strength in STRENGTHS else None
    strength = strength or (brand_kit.get("design_strength") if brand_kit.get("design_strength") in STRENGTHS else DEFAULT_STRENGTH)
    template = brand_kit.get("design_template") if brand_kit.get("design_template") in TEMPLATES else DEFAULT_TEMPLATE
    return {"strength": strength, "template": template}


def limits(strength: str, chars: int) -> Dict[str, int]:
    """글 길이·강도별 서식 상한. 과하면 오히려 읽기 어렵다는 요청서의 주의를 숫자로 옮긴 것."""
    chars = max(0, int(chars or 0))
    if strength == "none":
        return {k: 0 for k in ("emphasis", "heading", "quote", "divider", "box", "photo_hint")}
    if strength == "minimal":
        return {"emphasis": max(2, chars // 1000), "heading": 4, "quote": 1 if chars >= 800 else 0,
                "divider": 1, "box": 0, "photo_hint": 2}
    if strength == "rich":
        return {"emphasis": min(12, max(5, chars // 350)), "heading": 8, "quote": 2,
                "divider": 4, "box": 1, "photo_hint": 6}
    return {"emphasis": min(8, max(3, chars // 500)), "heading": 6, "quote": 2 if chars >= 800 else 1,
            "divider": 3, "box": 1, "photo_hint": 4}


def instruction(template: str, strength: str, has_photos: bool) -> Optional[str]:
    """초안 시스템 프롬프트에 넣을 서식 지시. 'none'이면 None(지금처럼 평문)."""
    if strength not in STRENGTHS or strength == "none":
        return None
    lim = limits(strength, 2000)
    tpl = TEMPLATES.get(template, TEMPLATES[DEFAULT_TEMPLATE])
    lines = [
        f"[원고 서식 — {tpl['label']} · 강도 {STRENGTHS[strength]['label']}]",
        "네이버 블로그에 바로 쓸 수 있게 아래 표시로 서식을 넣으세요. 표시 외의 마크다운(#만 쓰기, 표, 링크 등)은 쓰지 마세요.",
        "- 소제목: 줄 맨 앞에 `## ` (작은 소제목은 `### `). 소제목은 짧은 한 줄로 쓰고, 본문 문장을 소제목으로 만들지 마세요.",
        f"- 굵게: `**구절**`, 형광 표시: `==구절==`. 한 문단에 최대 {PER_PARAGRAPH_EMPHASIS}곳, 글 전체에 {lim['emphasis']}곳 이하. "
        "핵심 문장이나 숫자·조건처럼 독자가 꼭 봐야 할 곳에만.",
        "- 글머리 목록은 줄 맨 앞 `- `, 번호 목록은 `1. `.",
    ]
    if lim["quote"]:
        lines.append(
            f"- 인용구: 문단 맨 앞에 `> `. 글 전체 {lim['quote']}개 이하. 이 글의 핵심 메시지 한 문장이나, 제공된 자료에 "
            "실제로 있는 고객 후기·전문가 멘트만 인용하세요. **없는 후기나 멘트를 지어내지 마세요.**"
        )
    if lim["divider"]:
        lines.append(f"- 구분선: 한 줄에 `---`만. 도입과 본문 사이, 주제가 바뀌는 곳, 마무리 앞에. {lim['divider']}개 이하.")
    if lim["box"]:
        lines.append("- 요약 박스: 글 끝에 `[요약 박스]` 줄과 `[/요약 박스]` 줄 사이에 '핵심 정리'를 3~5줄의 `- ` 목록으로. "
                     "본문에 이미 쓴 내용만 요약하고 새 사실을 넣지 마세요.")
    if has_photos:
        lines.append("- 사진 설명: `[IMAGE: …]` 줄 바로 다음 줄에 `[사진 설명: 한 줄 설명]`을 둘 수 있습니다(사진에 실제로 보이는 것만).")
    elif lim["photo_hint"]:
        lines.append(f"- 사진 자리 제안: 사진이 있으면 좋을 곳에 `[사진 제안: 어떤 사진]` 줄을 {lim['photo_hint']}개 이하로.")
    if template == "info":
        lines.append("- 정보형: 소제목으로 단계·항목을 나누고, 절차나 조건은 번호 목록으로 쓰세요. 요약 박스를 꼭 넣으세요." if lim["box"]
                     else "- 정보형: 소제목으로 단계·항목을 나누고, 절차나 조건은 번호 목록으로 쓰세요.")
    elif template == "review":
        lines.append("- 후기형: 현장·고객의 말이 자료에 있으면 인용구로 세우고, 감정이 담긴 문장을 형광으로 표시하세요.")
    else:
        lines.append("- 기본형: 소제목 3~4개로 구간을 나누고, 핵심 메시지를 인용구 하나로 세우세요." if lim["quote"]
                     else "- 기본형: 소제목 3~4개로 구간을 나누세요.")
    lines.append("서식 표시는 꾸밈일 뿐이니, 서식 때문에 문장을 바꾸거나 내용을 늘리지 마세요.")
    return "\n".join(lines)


# --- 추출 -------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{2,4})\s+(.+?)\s*#*\s*$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_DIVIDER_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,}|—{2,}|─{3,})\s*$")
_BOX_OPEN_RE = re.compile(r"^\s*\[\s*요약\s*박스\s*(?::\s*(.+?))?\s*\]\s*$")
_BOX_CLOSE_RE = re.compile(r"^\s*\[\s*/\s*요약\s*박스\s*\]\s*$")
_PHOTO_HINT_RE = re.compile(r"^\s*\[\s*사진\s*제안\s*[:：]\s*(.+?)\s*\]\s*$")
_PHOTO_CAPTION_RE = re.compile(r"^\s*\[\s*사진\s*설명\s*[:：]\s*(.+?)\s*\]\s*$")
_BULLET_RE = re.compile(r"^\s*[*•·]\s+")
_INLINE_RE = re.compile(r"\*\*(.+?)\*\*|==(.+?)==")
_STRAY_RE = re.compile(r"\*\*|==")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _strip_inline(line: str, items: List[dict]) -> str:
    def repl(m):
        bold, hl = m.group(1), m.group(2)
        phrase = (bold if bold is not None else hl).strip()
        if phrase:
            items.append({"type": "bold" if bold is not None else "highlight", "text": phrase})
        return bold if bold is not None else hl

    out = _INLINE_RE.sub(repl, line)
    return _STRAY_RE.sub("", out)


def extract(draft: str) -> Tuple[str, List[dict]]:
    """초안의 서식 표시를 떼어 (평문 본문, 서식 목록)을 돌려줍니다.

    서식 표시가 없는 초안(강도 '사용 안 함'이나 예전 글)은 본문이 그대로 나옵니다.
    `[IMAGE: …]` 태그와 그 위치는 건드리지 않습니다.
    """
    items: List[dict] = []
    out_lines: List[str] = []
    pending_divider = False
    box_open: Optional[dict] = None
    last_para = ""
    last_image: Optional[str] = None

    for raw in (draft or "").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            out_lines.append("")
            continue
        if _DIVIDER_RE.match(stripped):
            pending_divider = True
            continue
        m = _BOX_OPEN_RE.match(stripped)
        if m:
            box_open = {"type": "box", "start": None, "end": None, "title": (m.group(1) or "핵심 정리").strip()}
            continue
        if _BOX_CLOSE_RE.match(stripped):
            if box_open and box_open["start"]:
                items.append(box_open)
            box_open = None
            continue
        m = _PHOTO_HINT_RE.match(stripped)
        if m:
            items.append({"type": "photo_hint", "after": last_para, "caption": m.group(1).strip()})
            continue
        m = _PHOTO_CAPTION_RE.match(stripped)
        if m:
            if last_image:
                items.append({"type": "photo_caption", "path": last_image, "caption": m.group(1).strip()})
            continue
        if IMAGE_TAG_RE.fullmatch(stripped):
            last_image = IMAGE_TAG_RE.fullmatch(stripped).group(1).strip()
            out_lines.append(stripped)
            continue

        kind, level = "p", 0
        m = _HEADING_RE.match(stripped)
        if m:
            kind, level, stripped = "heading", min(3, len(m.group(1))), m.group(2)
        else:
            m = _QUOTE_RE.match(stripped)
            if m:
                kind, stripped = "quote", m.group(1).strip()
        stripped = _BULLET_RE.sub("- ", stripped)
        text = _strip_inline(stripped, items).strip()
        if kind == "heading":
            text = text.strip("*= ").strip()
        if not text:
            continue
        if kind == "heading":
            items.append({"type": "heading", "level": level, "text": text})
        elif kind == "quote":
            items.append({"type": "quote", "text": text, "style": "quote"})
        if pending_divider:
            items.append({"type": "divider", "before": text, "style": "line"})
            pending_divider = False
        if box_open is not None:
            box_open["start"] = box_open["start"] or text
            box_open["end"] = text
        out_lines.append(text)
        last_para = text

    if box_open and box_open.get("start"):
        items.append(box_open)
    plain = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip()
    return plain, items


def has_markup(text: str) -> bool:
    """본문에 서식 표시가 남아 있는지 (수정 반영·직접 편집으로 들어온 경우)."""
    for line in (text or "").split("\n"):
        s = line.strip()
        if (_HEADING_RE.match(s) or _DIVIDER_RE.match(s) or _BOX_OPEN_RE.match(s) or _PHOTO_HINT_RE.match(s)
                or _PHOTO_CAPTION_RE.match(s) or (s.startswith(">") and len(s) > 1)):
            return True
    return bool(_INLINE_RE.search(text or ""))


# --- 문단 찾기 ----------------------------------------------------------------------

def paragraphs(content: str) -> List[Tuple[str, str]]:
    """('text', 문단) / ('image', 경로)를 문서 순서대로 — 시뮬레이터와 같은 나눔."""
    out: List[Tuple[str, str]] = []
    for kind, value in split_segments(content or ""):
        if kind == "image":
            out.append(("image", value))
        else:
            for para in value.split("\n"):
                if para.strip():
                    out.append(("text", para.strip()))
    return out


def _find_para(anchor: str, texts: List[str], used: Optional[set] = None) -> Optional[int]:
    """문단 전체로 단 서식의 자리. 같은 문장 → 공백만 다른 문장 → 거의 같은 문장(교정으로 몇 글자 바뀐 경우)."""
    target = _norm(anchor)
    if not target:
        return None
    for i, t in enumerate(texts):
        if _norm(t) == target and (used is None or i not in used):
            return i
    squeezed = target.replace(" ", "")
    for i, t in enumerate(texts):
        if _norm(t).replace(" ", "") == squeezed and (used is None or i not in used):
            return i
    best, best_i = 0.0, None
    for i, t in enumerate(texts):
        if used is not None and i in used:
            continue
        ratio = difflib.SequenceMatcher(None, _norm(t), target).ratio()
        if ratio > best:
            best, best_i = ratio, i
    return best_i if best >= 0.86 else None


def _find_span(phrase: str, text: str) -> Optional[Tuple[int, int]]:
    """문단 안 구절의 위치. 띄어쓰기만 바뀐 경우도 찾습니다."""
    phrase = (phrase or "").strip()
    if not phrase:
        return None
    pos = text.find(phrase)
    if pos >= 0:
        return pos, pos + len(phrase)
    chars = [re.escape(c) for c in phrase if not c.isspace()]
    if not chars:
        return None
    m = re.search(r"\s*".join(chars), text)
    return (m.start(), m.end()) if m else None


def _merge_spans(spans: List[Tuple[int, int, str, int]]) -> List[Tuple[int, int, str, int]]:
    """겹치는 강조는 앞의 것만 남깁니다."""
    out: List[Tuple[int, int, str, int]] = []
    for s in sorted(spans):
        if out and s[0] < out[-1][1]:
            continue
        out.append(s)
    return out


# --- 상한 · 정리 --------------------------------------------------------------------

def finalize(content: str, items: List[dict], strength: str, template: str, source: str = "generate") -> dict:
    """교정이 끝난 본문 기준으로 서식을 정리해 저장할 모양으로 만듭니다.

    붙일 자리를 못 찾은 서식은 버리고(개수는 `dropped`에), 강도·길이 상한을 넘는 것은 뒤에서부터 자릅니다.
    """
    texts = [v for k, v in paragraphs(content) if k == "text"]
    chars = len(re.sub(r"\s", "", "".join(texts)))
    lim = limits(strength, chars)
    kept: List[dict] = []
    dropped = 0
    counts = {"emphasis": 0, "heading": 0, "quote": 0, "divider": 0, "box": 0, "photo_hint": 0}
    per_para: Dict[int, int] = {}
    seen: set = set()
    for item in items or []:
        kind = item.get("type")
        if kind in ("bold", "highlight"):
            idx = next((i for i, t in enumerate(texts) if _find_span(item.get("text", ""), t)), None)
            if idx is None:
                dropped += 1
                continue
            if counts["emphasis"] >= lim["emphasis"] or per_para.get(idx, 0) >= PER_PARAGRAPH_EMPHASIS:
                continue
            key = (kind, _norm(item["text"]))
            if key in seen:
                continue
            seen.add(key)
            counts["emphasis"] += 1
            per_para[idx] = per_para.get(idx, 0) + 1
        elif kind in ("heading", "quote"):
            if _find_para(item.get("text", ""), texts) is None:
                dropped += 1
                continue
            if counts[kind] >= lim[kind]:
                continue
            counts[kind] += 1
        elif kind == "divider":
            idx = _find_para(item.get("before", ""), texts)
            if idx is None or idx == 0:
                dropped += idx is None
                continue
            if counts["divider"] >= lim["divider"]:
                continue
            counts["divider"] += 1
        elif kind == "box":
            if _find_para(item.get("start", ""), texts) is None or _find_para(item.get("end", ""), texts) is None:
                dropped += 1
                continue
            if counts["box"] >= lim["box"]:
                continue
            counts["box"] += 1
        elif kind == "photo_hint":
            if counts["photo_hint"] >= lim["photo_hint"]:
                continue
            counts["photo_hint"] += 1
        elif kind == "photo_caption":
            pass
        else:
            continue
        kept.append(dict(item))
    for n, item in enumerate(kept, start=1):
        item["id"] = n
    return {"version": FORMAT_VERSION, "template": template, "strength": strength, "source": source,
            "items": kept, "dropped": dropped}


def empty() -> dict:
    return {}


# --- 붙이기 (미리보기·네이버 공용) ------------------------------------------------------

def bind(content: str, fmt: Optional[dict]) -> Tuple[List[dict], List[dict]]:
    """본문 + 서식 → 그릴 블록 목록, 그리고 자리를 못 찾은 서식 목록.

    블록: {"kind": "image", "path"} 또는
          {"kind": "text", "text", "style": p|h2|h3|quote|li|oli, "spans": [(시작, 끝, bold|highlight, 서식 id)],
           "divider_before": 스타일|None, "box": {"title", "first", "last"}|None, "hints_after": [캡션…], "ids": [서식 id…],
           "quote_style": …}
    """
    fmt = fmt or {}
    items = fmt.get("items") or []
    paras = paragraphs(content)
    text_idx = [i for i, (k, _) in enumerate(paras) if k == "text"]
    texts = [paras[i][1] for i in text_idx]
    blocks: List[dict] = []
    for kind, value in paras:
        if kind == "image":
            blocks.append({"kind": "image", "path": value, "caption": None})
        else:
            style = "p"
            if re.match(r"^-\s+", value):
                style = "li"
            elif re.match(r"^\d+[.)]\s+", value):
                style = "oli"
            blocks.append({"kind": "text", "text": value, "style": style, "spans": [], "divider_before": None,
                           "box": None, "hints_after": [], "ids": [], "quote_style": None})

    def tb(n: int) -> dict:
        return blocks[text_idx[n]]

    unmatched: List[dict] = []
    used_heading: set = set()
    for item in items:
        kind = item.get("type")
        iid = item.get("id")
        if kind in ("bold", "highlight"):
            hit = None
            for n, t in enumerate(texts):
                span = _find_span(item.get("text", ""), t)
                if span:
                    hit = (n, span)
                    break
            if not hit:
                unmatched.append(item)
                continue
            n, (a, b) = hit
            tb(n)["spans"].append((a, b, kind, iid))
            tb(n)["ids"].append(iid)
        elif kind in ("heading", "quote"):
            n = _find_para(item.get("text", ""), texts, used_heading)
            if n is None:
                unmatched.append(item)
                continue
            used_heading.add(n)
            block = tb(n)
            if kind == "heading":
                block["style"] = "h3" if int(item.get("level") or 2) >= 3 else "h2"
            else:
                block["style"] = "quote"
                block["quote_style"] = item.get("style") or "quote"
            block["ids"].append(iid)
        elif kind == "divider":
            n = _find_para(item.get("before", ""), texts)
            if n is None:
                unmatched.append(item)
                continue
            tb(n)["divider_before"] = item.get("style") or "line"
            tb(n)["ids"].append(iid)
        elif kind == "box":
            a, b = _find_para(item.get("start", ""), texts), _find_para(item.get("end", ""), texts)
            if a is None or b is None or b < a:
                unmatched.append(item)
                continue
            for n in range(a, b + 1):
                tb(n)["box"] = {"title": item.get("title") or "핵심 정리", "first": n == a, "last": n == b, "id": iid}
        elif kind == "photo_hint":
            after = item.get("after") or ""
            n = _find_para(after, texts) if after else None
            if after and n is None:
                unmatched.append(item)
                continue
            target = tb(n) if n is not None else None
            if target is None:
                blocks.insert(0, {"kind": "hint", "caption": item.get("caption", ""), "id": iid})
                text_idx = [i + 1 for i in text_idx]
            else:
                target["hints_after"].append((item.get("caption", ""), iid))
        elif kind == "photo_caption":
            for block in blocks:
                if block["kind"] == "image" and block["path"] == item.get("path"):
                    block["caption"] = item.get("caption")
                    break
            else:
                unmatched.append(item)
    for block in blocks:
        if block["kind"] == "text":
            block["spans"] = _merge_spans(block["spans"])
    return blocks, unmatched


def summary(content: str, fmt: Optional[dict]) -> Dict[str, int]:
    """종류별로 실제로 붙는 서식 수와 못 붙는 수."""
    blocks, unmatched = bind(content, fmt)
    out = {k: 0 for k in _TYPE_LABELS}
    for item in (fmt or {}).get("items") or []:
        out[item.get("type")] = out.get(item.get("type"), 0) + 1
    out["unmatched"] = len(unmatched)
    return out


# --- 편집 (워크벤치 🎨 서식) -------------------------------------------------------------

def remove_item(fmt: dict, item_id: int) -> dict:
    fmt = dict(fmt or {})
    fmt["items"] = [i for i in fmt.get("items") or [] if i.get("id") != item_id]
    return fmt


def update_item(fmt: dict, item_id: int, **changes) -> dict:
    fmt = dict(fmt or {})
    out = []
    for item in fmt.get("items") or []:
        if item.get("id") == item_id:
            item = {**item, **changes}
        out.append(item)
    fmt["items"] = out
    return fmt


# --- 기존 글에 [서식 적용] ------------------------------------------------------------------

APPLY_SYSTEM_PROMPT = (
    "당신은 네이버 블로그 편집 디자이너입니다. 이미 완성되어 검수까지 끝난 본문에 서식만 입힙니다. "
    "본문의 글자는 한 글자도 바꾸거나 더하거나 빼지 마세요 — 서식을 입힐 문단 번호와, 본문에 그대로 있는 구절만 고릅니다. "
    "출력은 JSON 하나만, 다른 설명 없이."
)


def apply_prompt(content: str, template: str, strength: str, has_photos: bool) -> str:
    texts = [v for k, v in paragraphs(content) if k == "text"]
    chars = len(re.sub(r"\s", "", "".join(texts)))
    lim = limits(strength, chars)
    numbered = "\n".join(f"[{n}] {t}" for n, t in enumerate(texts))
    tpl = TEMPLATES.get(template, TEMPLATES[DEFAULT_TEMPLATE])
    return (
        f"[서식 템플릿] {tpl['label']} — {tpl['desc']}\n"
        f"[강도] {STRENGTHS[strength]['label']}\n"
        f"[상한] 강조(굵게+형광) {lim['emphasis']}곳(한 문단 {PER_PARAGRAPH_EMPHASIS}곳), 소제목 {lim['heading']}개, "
        f"인용구 {lim['quote']}개, 구분선 {lim['divider']}개, 요약 박스 {lim['box']}개, 사진 자리 제안 "
        f"{0 if has_photos else lim['photo_hint']}개\n\n"
        f"[본문 — 문단 번호]\n{numbered}\n\n"
        "아래 JSON 형식으로만 답하세요. 문단 번호는 위 번호, 구절은 그 문단에 글자 그대로 있는 부분이어야 합니다.\n"
        "- headings: 이미 소제목처럼 짧은 문단(30자 이하)만 고르세요. 새 소제목 문장을 만들 수 없습니다.\n"
        "- quotes: 핵심 메시지 문단. 본문에 없는 말을 인용할 수 없습니다.\n"
        "- dividers_before: 구분선을 그 문단 '앞'에 둡니다(0번 문단 제외).\n"
        "- box: 글 끝의 정리·목록 문단 범위가 있으면 그 범위. 없으면 null.\n"
        "- photo_hints: 사진이 있으면 좋을 곳(그 문단 뒤)과 어떤 사진인지.\n"
        '{"headings": [{"para": 0, "level": 2}], "bold": [{"para": 0, "text": "구절"}], '
        '"highlight": [{"para": 0, "text": "구절"}], "quotes": [0], "dividers_before": [0], '
        '"box": {"start": 0, "end": 0}, "photo_hints": [{"after": 0, "caption": "어떤 사진"}]}'
    )


def parse_apply_response(raw: str, content: str) -> List[dict]:
    """[서식 적용] 응답 → 서식 목록. 본문에 없는 구절·없는 문단 번호는 버립니다(본문 글자 불변)."""
    text = (raw or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("서식 응답을 읽지 못했습니다.")
    data = json.loads(m.group(0))
    texts = [v for k, v in paragraphs(content) if k == "text"]

    def para(n) -> Optional[str]:
        try:
            n = int(n)
        except (TypeError, ValueError):
            return None
        return texts[n] if 0 <= n < len(texts) else None

    items: List[dict] = []
    for h in data.get("headings") or []:
        t = para(h.get("para") if isinstance(h, dict) else h)
        # 소제목처럼 보이는 줄만: 짧고, 목록이 아니고, 문장으로 끝나지 않는 줄.
        if t and len(t) <= 30 and not t.startswith("- ") and not re.search(r"[.!?。]\s*$|[.!?]\s", t):
            items.append({"type": "heading", "level": 3 if (isinstance(h, dict) and h.get("level") == 3) else 2, "text": t})
    for kind in ("bold", "highlight"):
        for b in data.get(kind) or []:
            if not isinstance(b, dict):
                continue
            t = para(b.get("para"))
            phrase = (b.get("text") or "").strip()
            if t and phrase and _find_span(phrase, t):
                items.append({"type": kind, "text": phrase})
    for q in data.get("quotes") or []:
        t = para(q.get("para") if isinstance(q, dict) else q)
        if t:
            items.append({"type": "quote", "text": t, "style": "quote"})
    for d in data.get("dividers_before") or []:
        try:
            n = int(d.get("para") if isinstance(d, dict) else d)
        except (TypeError, ValueError):
            continue
        if n > 0 and para(n):
            items.append({"type": "divider", "before": texts[n], "style": "line"})
    box = data.get("box")
    if isinstance(box, dict):
        a, b = para(box.get("start")), para(box.get("end"))
        if a and b:
            items.append({"type": "box", "start": a, "end": b, "title": "핵심 정리"})
    for h in data.get("photo_hints") or []:
        if isinstance(h, dict) and (h.get("caption") or "").strip():
            items.append({"type": "photo_hint", "after": para(h.get("after")) or "", "caption": h["caption"].strip()})
    return items


# --- 네이버 HTML ------------------------------------------------------------------

def _inline_html(block: dict) -> str:
    text = block["text"]
    if block["style"] == "li":
        text = re.sub(r"^-\s+", "", text)
    elif block["style"] == "oli":
        text = re.sub(r"^\d+[.)]\s+", "", text)
    offset = len(block["text"]) - len(text)
    out, pos = [], 0
    for a, b, kind, _iid in block["spans"]:
        a, b = a - offset, b - offset
        if a < pos or a < 0:
            continue
        out.append(html.escape(text[pos:a]))
        inner = html.escape(text[a:b])
        out.append(f"<b>{inner}</b>" if kind == "bold" else f'<span style="background-color:{HIGHLIGHT_COLOR};">{inner}</span>')
        pos = b
    out.append(html.escape(text[pos:]))
    return "".join(out)


def _divider_html(style: str, mode: str) -> str:
    if mode == "symbol":
        line = {"dotted": "· · · · · · · · · · · ·", "bold": "━━━━━━━━━━━━"}.get(style, "────────────")
        return f'<p style="text-align:center;"><span style="color:#999999;">{line}</span></p>'
    css = {"dotted": "border-top:1px dotted #999;", "bold": "border-top:3px solid #333;"}.get(style, "")
    return f'<hr style="{css}">' if css else "<hr>"


def text_blocks_html(blocks: List[dict], mode: str = DEFAULT_NAVER_FORMAT_MODE) -> str:
    """사진 사이 텍스트 블록들 → 붙여넣을 HTML 한 덩어리."""
    out: List[str] = []
    list_open: Optional[str] = None
    in_box = False

    def close_list():
        nonlocal list_open
        if list_open and mode == "editor":
            out.append(f"</{list_open}>")
        list_open = None

    for block in blocks:
        if block["kind"] == "hint":
            close_list()
            out.append(_hint_html(block["caption"]))
            continue
        if block["kind"] != "text":
            continue
        if block["divider_before"]:
            close_list()
            out.append(_divider_html(block["divider_before"], mode))
        box = block.get("box")
        if box and box["first"]:
            close_list()
            title = html.escape(box["title"])
            if mode == "editor":
                out.append('<table style="border-collapse:collapse;width:100%;"><tr>'
                           '<td style="border:1px solid #d0d7de;background-color:#f6f8fa;padding:12px;">'
                           f"<p><b>📌 {title}</b></p>")
            else:
                out.append(f"<p><b>📌 {title}</b></p>")
            in_box = True
        inner = _inline_html(block)
        style = block["style"]
        if style in ("li", "oli"):
            tag = "ul" if style == "li" else "ol"
            if mode == "editor" and not in_box:
                if list_open != tag:
                    close_list()
                    out.append(f"<{tag}>")
                    list_open = tag
                out.append(f"<li>{inner}</li>")
            else:
                bullet = "•" if style == "li" else re.match(r"^(\d+[.)])", block["text"]).group(1)
                out.append(f"<p>{bullet} {inner}</p>")
        else:
            close_list()
            if style == "h2":
                out.append(f'<p><span style="font-size:24px;"><b>{inner}</b></span></p>')
            elif style == "h3":
                out.append(f'<p><span style="font-size:19px;"><b>{inner}</b></span></p>')
            elif style == "quote":
                if mode == "editor" and not in_box:
                    out.append(f"<blockquote><p>{inner}</p></blockquote>")
                else:
                    out.append(f'<p><span style="color:#888888;">❝</span> <i>{inner}</i> <span style="color:#888888;">❞</span></p>')
            else:
                out.append(f"<p>{inner}</p>")
        for caption, _iid in block.get("hints_after") or []:
            close_list()
            out.append(_hint_html(caption))
        if box and box["last"]:
            close_list()
            if mode == "editor":
                out.append("</td></tr></table>")
            else:
                out.append(_divider_html("line", "symbol"))
            in_box = False
    close_list()
    if in_box and mode == "editor":
        out.append("</td></tr></table>")
    return "".join(out)


def _hint_html(caption: str) -> str:
    return ('<p><span style="background-color:#FFE3E3;color:#C62828;"><b>📷 사진 자리: '
            f"{html.escape(caption)} — 발행 전에 사진을 넣고 이 줄을 지우세요</b></span></p>")


def naver_segments(content: str, fmt: Optional[dict], mode: str = DEFAULT_NAVER_FORMAT_MODE) -> List[Tuple[str, str]]:
    """붙여넣기 순서: ('html', 덩어리) / ('image', 경로). 서식이 없으면 지금과 같은 <p> 문단입니다."""
    blocks, _ = bind(content, fmt)
    out: List[Tuple[str, str]] = []
    run: List[dict] = []
    for block in blocks:
        if block["kind"] == "image":
            if run:
                out.append(("html", text_blocks_html(run, mode)))
                run = []
            out.append(("image", block["path"]))
        else:
            run.append(block)
    if run:
        out.append(("html", text_blocks_html(run, mode)))
    return [(k, v) for k, v in out if v]


def expected_components(content: str, fmt: Optional[dict]) -> Dict[str, int]:
    """붙여 넣은 뒤 에디터에 생겨야 할 인용구·구분선·표 수 — 붙여넣기 결과를 세어 볼 기준."""
    blocks, _ = bind(content, fmt)
    quotes = sum(1 for b in blocks if b["kind"] == "text" and b["style"] == "quote" and not b.get("box"))
    dividers = sum(1 for b in blocks if b["kind"] == "text" and b["divider_before"])
    boxes = sum(1 for b in blocks if b["kind"] == "text" and (b.get("box") or {}).get("first"))
    hints = sum(len(b.get("hints_after") or []) for b in blocks if b["kind"] == "text") + sum(1 for b in blocks if b["kind"] == "hint")
    return {"quote": quotes, "divider": dividers, "box": boxes, "hint": hints}


def naver_full_html(content: str, fmt: Optional[dict], mode: str = DEFAULT_NAVER_FORMAT_MODE) -> str:
    """손으로 붙여 넣을 때 쓰는 본문 전체 HTML. 사진 자리는 '[사진: 파일명]' 줄로 남깁니다."""
    parts = []
    for kind, value in naver_segments(content, fmt, mode):
        if kind == "image":
            name = value.replace("\\", "/").rsplit("/", 1)[-1]
            parts.append(f'<p><span style="color:#2B6CB0;"><b>[사진: {html.escape(name)}]</b></span></p>')
        else:
            parts.append(value)
    return "".join(parts)


def fill_photo_hints(content: str, fmt: Optional[dict], attached: List[str]) -> Tuple[str, dict, int]:
    """초안을 만든 뒤 첨부한 사진을 '사진 자리' 표시가 있던 곳에 차례로 넣습니다 (AI 호출 없음).

    본문에 아직 배치되지 않은 첨부 사진만 씁니다. 사진 자리마다 그 앞 문단 바로 뒤에 `[IMAGE: 경로]` 줄을
    넣고 그 '사진 자리' 서식은 지웁니다. 사진이 자리보다 많으면 남은 사진은 그대로(게시 때 글 끝에 붙음),
    자리가 더 많으면 남은 자리는 그대로 둡니다. (새 본문, 새 서식, 넣은 사진 수)를 돌려줍니다.
    """
    fmt = dict(fmt or {})
    items = list(fmt.get("items") or [])
    tagged = set(IMAGE_TAG_RE.findall(content or ""))
    pending_photos = [p for p in (attached or []) if p not in tagged]
    hints = [i for i in items if i.get("type") == "photo_hint"]
    if not pending_photos or not hints:
        return content, fmt, 0

    lines = (content or "").split("\n")
    text_lines = [n for n, line in enumerate(lines) if line.strip() and not IMAGE_TAG_RE.fullmatch(line.strip())]
    texts = [lines[n].strip() for n in text_lines]

    # 문서 순서대로 자리를 고릅니다(앞 문단이 없는 자리 = 글 맨 앞). 못 찾은 자리는 건너뜁니다.
    located = []
    for hint in hints:
        after = hint.get("after") or ""
        if not after:
            located.append((-1, hint))
            continue
        idx = _find_para(after, texts)
        if idx is not None:
            located.append((text_lines[idx], hint))
    located.sort(key=lambda x: x[0])
    pairs = list(zip(located, pending_photos))

    # 뒤에서부터 넣어야 앞 줄 번호가 밀리지 않습니다.
    for (line_no, _hint), photo in reversed(pairs):
        lines.insert(line_no + 1, f"[IMAGE: {photo}]")
    used_ids = {hint.get("id") for (_n, hint), _p in pairs}
    fmt["items"] = [i for i in items if not (i.get("type") == "photo_hint" and i.get("id") in used_ids)]
    return "\n".join(lines), fmt, len(pairs)


def unplaced_photos(content: str, attached: List[str]) -> List[str]:
    """첨부했지만 본문 어디에도 배치되지 않은 사진 (첨부 순서)."""
    tagged = set(IMAGE_TAG_RE.findall(content or ""))
    return [p for p in dict.fromkeys(attached or []) if p not in tagged]
