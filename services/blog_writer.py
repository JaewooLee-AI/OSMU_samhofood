"""네이버 블로그 원고 생성 · 이미지 배치 · 표현 점검.

원고는 '평문 한 덩어리'로 다룹니다 (사용자가 그대로 고칠 수 있도록).
- `## 소제목` 줄 = 소제목
- `[사진:A1]` 줄 = 그 자리에 첨부 사진 A1을 넣음
- `[카드:2]` 줄 = 그 자리에 카드뉴스 2번 이미지를 넣음
- 나머지는 문단 (빈 줄로 구분)
네이버 게시 때는 이 평문을 (html 조각, 이미지) 순서 목록으로 바꿔 붙여넣습니다.
"""
import re
import base64
import asyncio
import logging
from html import escape
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from services import brand_kit as bk
from services import compliance
from services import usage_log
from models.schema import MediaAsset

logger = logging.getLogger(__name__)

PHOTO_RE = re.compile(r"^\s*\[사진\s*:\s*([A-Za-z]+\d+)\s*\]\s*$")
CARD_RE = re.compile(r"^\s*\[카드\s*:\s*(\d+)\s*\]\s*$")
HEADING_RE = re.compile(r"^\s*#{1,3}\s+(.*\S)\s*$")

IMAGE_MODES = {
    "mine": "내가 올린 사진 사용",
    "cards": "카드뉴스 이미지 사용",
    "both": "내 사진 + 카드뉴스 이미지 모두",
}


class BlogLLM(BaseModel):
    title: str = Field(description="네이버 블로그 글 제목 (검색에 걸릴 핵심 키워드 포함, 40자 이내)")
    body: str = Field(description="본문. 소제목은 '## 소제목' 줄, 사진을 넣을 자리는 단독 줄 '[사진:A1]'. 문단은 빈 줄로 구분")
    hashtags: List[str] = Field(default_factory=list, description="해시태그 5~8개 (#포함)")


class BlogDraft(BaseModel):
    title: str = ""
    body: str = ""
    hashtags: List[str] = Field(default_factory=list)
    engine: str = ""
    notes: List[str] = Field(default_factory=list)


class BlogRequest(BaseModel):
    brand_id: str
    audience: str = ""        # 타깃 (예: 캠핑족)
    product: str = ""         # 이번에 알릴 상품
    news: str = ""            # 이번 주 소식·프로모션·나눔 이야기
    memo: str = ""            # 자유 메모 (선택)
    length: str = "보통"      # 짧게 | 보통 | 길게
    provider: str = "gemini"  # gemini | claude
    assets: List[MediaAsset] = Field(default_factory=list)


LENGTHS = {"짧게": "공백 포함 약 800~1,000자", "보통": "공백 포함 약 1,500~1,800자", "길게": "공백 포함 약 2,500~3,000자"}


# ----------------------------------------------------------------------
# 프롬프트
# ----------------------------------------------------------------------
def _prompt(req: BlogRequest, kit: bk.BrandKit, pack) -> str:
    facts = "\n".join(f"- {f}" for f in kit.core_facts) or "- (제공된 사실 없음)"
    photos = ""
    if req.assets:
        photos = ("\n[첨부 사진] 아래 ID의 사진이 함께 전달됩니다. 사진 내용을 보고 어울리는 문단 뒤에 단독 줄 '[사진:ID]'를 넣으세요. "
                  "사진마다 한 번씩만 쓰고, 사진에 보이지 않는 내용을 지어내지 마세요.\n"
                  + ", ".join(a.id for a in req.assets))
    else:
        photos = "\n[첨부 사진] 없음 — '[사진:...]' 표시를 넣지 마세요."
    return f"""네이버 블로그 글을 한 편 써 주세요.

[브랜드]
- 이름: {kit.brand_name} ({kit.tagline})
- 화자: {kit.persona}
- 말투: {kit.tone}
- 근거로 쓸 수 있는 사실(이 목록과 입력 내용에 있는 것만 사실로 쓸 것):
{facts}

[이번 글 입력]
- 타깃: {req.audience or '(지정 없음 — 브랜드 타깃에 맞춰 선택)'}
- 알릴 상품: {req.product or '(지정 없음)'}
- 이번 주 소식: {req.news or '(없음)'}
- 추가 메모: {req.memo or '(없음)'}
{photos}

[글 구성]
- 분량: {LENGTHS.get(req.length, LENGTHS['보통'])}
- 도입(공감 되는 고민) → 소제목 3~4개 본론 → 마무리(부드러운 안내: {kit.cta_headline})
- 소제목은 '## 소제목' 형식, 문단은 2~4문장, 문단 사이 빈 줄
- 제목은 타깃이 검색할 만한 키워드를 자연스럽게 넣을 것
- 해시태그 후보: {' '.join(kit.default_hashtags)}

[지킬 규칙]
- 입력과 사실 목록에 없는 수치·인증·효능·가격·순위를 만들지 말 것
{compliance.prompt_rules(pack, kit)}
"""


def _system(kit: bk.BrandKit) -> str:
    return f"당신은 '{kit.brand_name}'의 블로그 담당자입니다. 사실을 지어내지 않고, 식품 표시·광고 규칙을 지키며, 읽는 사람에게 도움이 되는 글을 씁니다."


def _image_parts(assets: List[MediaAsset]) -> List[Tuple[str, bytes]]:
    out = []
    for a in assets:
        src = a.keyframes[0] if a.keyframes else a.path
        try:
            with open(src, "rb") as f:
                out.append((f"[{a.id}] 첨부 사진 '{a.original_name}'", f.read()))
        except OSError:
            continue
    return out


# ----------------------------------------------------------------------
# 생성
# ----------------------------------------------------------------------
async def generate(req: BlogRequest, keys: Dict[str, str]) -> BlogDraft:
    """블로그 원고 생성. 키가 없거나 호출이 실패하면 규칙 기반 초안으로 대신한다 (사유는 notes)."""
    kit = bk.load_kit(req.brand_id)
    pack = compliance.load_pack(kit.industry_pack)
    provider = (req.provider or "gemini").lower()
    api_key = keys.get("anthropic" if provider == "claude" else "gemini")
    draft: Optional[BlogDraft] = None
    notes: List[str] = []
    if api_key:
        try:
            llm = await (_claude if provider == "claude" else _gemini)(req, kit, pack, api_key)
            draft = BlogDraft(title=llm.title.strip(), body=_clean_body(llm.body, req.assets),
                              hashtags=[h if h.startswith("#") else f"#{h}" for h in llm.hashtags][:10])
        except Exception as ex:  # noqa: BLE001
            logger.warning("블로그 생성 실패(%s): %s", provider, ex)
            notes.append(f"AI 호출에 실패해 기본 초안으로 대신했습니다 ({type(ex).__name__}). API 키와 인터넷 연결을 확인해 주세요.")
    else:
        notes.append(f"{'Claude' if provider == 'claude' else 'Gemini'} API 키가 없어 기본 초안으로 만들었습니다. 설정에서 키를 등록하면 훨씬 자연스러운 글이 나옵니다.")
    if draft is None:
        draft = _rule_based(req, kit)
        draft.engine = "기본 초안"
    draft.body, replaced = apply_rules(draft.body, kit, pack)
    draft.title, _ = apply_rules(draft.title, kit, pack)
    if replaced:
        notes.append("표현 규칙에 따라 바꾼 말: " + ", ".join(replaced))
    draft.notes = notes
    return draft


async def _gemini(req: BlogRequest, kit, pack, api_key: str) -> BlogLLM:
    from google import genai
    from google.genai import types
    from services.ai_director import planning_model

    client = genai.Client(api_key=api_key)
    model = planning_model()
    contents: list = [_system(kit) + "\n\n" + _prompt(req, kit, pack)]
    for label, data in _image_parts(req.assets):
        contents.append(label)
        contents.append(types.Part.from_bytes(data=data, mime_type="image/jpeg"))
    response = await asyncio.to_thread(
        client.models.generate_content, model=model, contents=contents,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=BlogLLM, temperature=0.6))
    usage_log.record_gemini("블로그 원고", model, response)
    return BlogLLM.model_validate_json(response.text)


async def _claude(req: BlogRequest, kit, pack, api_key: str) -> BlogLLM:
    from anthropic import AsyncAnthropic
    from services.ai_director import CLAUDE_MODEL

    client = AsyncAnthropic(api_key=api_key)
    content = [{"type": "text", "text": _prompt(req, kit, pack)}]
    for label, data in _image_parts(req.assets):
        content.append({"type": "text", "text": label})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(data).decode()}})
    response = await client.beta.messages.parse(
        model=CLAUDE_MODEL, max_tokens=8000,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "medium"},
        system=_system(kit), messages=[{"role": "user", "content": content}], output_format=BlogLLM)
    usage_log.record_claude("블로그 원고", response.model, response)
    if response.stop_reason in ("refusal", "max_tokens") or response.parsed_output is None:
        raise RuntimeError(f"Claude 응답 불완전 (stop_reason={response.stop_reason})")
    return response.parsed_output


def _rule_based(req: BlogRequest, kit: bk.BrandKit) -> BlogDraft:
    """AI 없이 입력을 문단으로 엮은 초안 — 사용자가 고쳐 쓰는 출발점"""
    topic = req.product or kit.brand_name
    title = f"{topic} 이야기 — {req.audience}을 위한 안내" if req.audience else f"{topic} 이야기"
    parts = [f"안녕하세요, {kit.brand_name}입니다."]
    if req.news:
        parts += ["## 이번 소식", req.news.strip()]
    if req.product:
        parts += [f"## {req.product} 소개", f"{req.product}에 대해 알려 드립니다. (여기에 상품의 특징과 조리·보관 방법을 적어 주세요.)"]
    if req.memo:
        parts += ["## 더 알려 드릴 이야기", req.memo.strip()]
    parts += ["## 마무리", f"{kit.cta_headline}. {' '.join(kit.cta_body)}"]
    body = "\n\n".join(parts)
    # 사진은 소제목 뒤 문단 아래에 차례로
    blocks = body.split("\n\n")
    for i, a in enumerate(req.assets):
        pos = min(len(blocks), 2 + i * 3)
        while pos < len(blocks) and HEADING_RE.match(blocks[pos - 1]):
            pos += 1
        blocks.insert(pos, f"[사진:{a.id}]")
    return BlogDraft(title=title, body="\n\n".join(blocks), hashtags=list(kit.default_hashtags))


def _clean_body(body: str, assets: List[MediaAsset]) -> str:
    """모델이 돌려준 본문 정리: 줄바꿈 글자, 없는 사진 ID 제거, 빠진 사진은 끝에 추가"""
    body = (body or "").replace("\\n", "\n").strip()
    valid = {a.id for a in assets}
    lines = []
    used = set()
    for line in body.splitlines():
        m = PHOTO_RE.match(line)
        if m:
            pid = m.group(1)
            if pid not in valid or pid in used:
                continue
            used.add(pid)
            line = f"[사진:{pid}]"
        lines.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    missing = [a.id for a in assets if a.id not in used]
    if missing:
        text += "\n\n" + "\n\n".join(f"[사진:{i}]" for i in missing)
    return text


# ----------------------------------------------------------------------
# 표현 규칙 · 점검
# ----------------------------------------------------------------------
def apply_rules(text: str, kit: bk.BrandKit, pack) -> Tuple[str, List[str]]:
    """금지 표현을 대체 표현으로 바꾼다. (바꾼 목록 반환)"""
    done = []
    for bad, good in {**pack.replacements, **kit.extra_replacements}.items():
        if bad and bad in text:
            text = text.replace(bad, good)
            done.append(f"'{bad.strip()}'→'{good.strip() or '삭제'}'")
    return text, done


def review(text: str, kit: bk.BrandKit) -> List[str]:
    """게시 전에 사람이 확인할 표현 (경고만, 자동 수정 없음)"""
    pack = compliance.load_pack(kit.industry_pack)
    out = []
    for wp in pack.warn_patterns:
        hits = sorted({m.group(0).strip() for m in re.finditer(wp.pattern, text)})
        if hits:
            out.append(f"'{', '.join(hits[:4])}' — {wp.reason}")
    for term in kit.extra_warn_terms:
        if term and term in text:
            out.append(f"'{term}' — 회사 주의 단어입니다. 근거를 확인해 주세요.")
    return out


# ----------------------------------------------------------------------
# 이미지 배치 · 네이버용 변환
# ----------------------------------------------------------------------
def _blocks(body: str) -> List[str]:
    return [b.strip() for b in re.split(r"\n\s*\n", body or "") if b.strip()]


def arrange_images(body: str, mode: str, photo_ids: List[str], card_count: int) -> str:
    """이미지 옵션에 맞게 본문의 이미지 표시를 정리한다.
    mine: 내 사진만 / cards: 내 사진 표시를 지우고 카드뉴스를 고르게 배치 / both: 내 사진은 제자리, 카드뉴스는 글 끝에 모아서"""
    blocks = [b for b in _blocks(body) if not (CARD_RE.match(b))]
    if mode in ("cards", "both"):
        if mode == "cards":
            blocks = [b for b in blocks if not PHOTO_RE.match(b)]
        cards = [f"[카드:{i}]" for i in range(1, card_count + 1)]
        if mode == "both":
            blocks += ["## 카드뉴스로 한눈에 보기"] + cards
        elif cards:
            n = len(blocks)
            # 도입 문단 뒤부터 글 전체에 고르게 끼워 넣기 (뒤 카드부터 넣어야 앞 위치가 밀리지 않음)
            for i in range(len(cards) - 1, -1, -1):
                pos = min(n, max(1, round((i + 1) * n / (len(cards) + 1))))
                while pos < n and HEADING_RE.match(blocks[pos - 1]):  # 소제목 바로 아래는 피함
                    pos += 1
                blocks.insert(pos, cards[i])
    elif mode == "mine":
        placed = {PHOTO_RE.match(b).group(1) for b in blocks if PHOTO_RE.match(b)}
        blocks += [f"[사진:{pid}]" for pid in photo_ids if pid not in placed]
    return "\n\n".join(blocks)


def to_segments(title: str, body: str, hashtags: List[str], photos: Dict[str, str], cards: List[str]) -> List[Tuple[str, str]]:
    """평문 원고 → [("html", ...), ("image", 경로)] 문서 순서 목록 (네이버 붙여넣기용)"""
    segs: List[Tuple[str, str]] = []
    html: List[str] = []

    def flush():
        if html:
            segs.append(("html", "".join(html)))
            html.clear()

    for block in _blocks(body):
        pm, cm = PHOTO_RE.match(block), CARD_RE.match(block)
        if pm or cm:
            path = photos.get(pm.group(1)) if pm else (cards[int(cm.group(1)) - 1] if 0 < int(cm.group(1)) <= len(cards) else None)
            if path:
                flush()
                segs.append(("image", path))
            continue
        hm = HEADING_RE.match(block)
        if hm:
            html.append(f"<p><b>{escape(hm.group(1))}</b></p><p><br></p>")
        else:
            html.append("".join(f"<p>{escape(line)}</p>" for line in block.splitlines() if line.strip()) + "<p><br></p>")
    if hashtags:
        html.append(f"<p>{escape(' '.join(hashtags))}</p>")
    flush()
    return segs


def plain_text(body: str) -> str:
    """카드뉴스·쇼츠 기획에 넘길 원문 (이미지 표시·소제목 기호 제거)"""
    out = []
    for b in _blocks(body):
        if PHOTO_RE.match(b) or CARD_RE.match(b):
            continue
        hm = HEADING_RE.match(b)
        out.append(hm.group(1) + "." if hm else b)
    return "\n".join(out)
