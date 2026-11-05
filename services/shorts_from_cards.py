"""확정한 카드 → 쇼츠 대본 변환.

원칙
- 장면 순서·이미지는 확정 카드를 그대로 따른다 (새 AI 이미지를 만들지 않음, 카드의 배경 에셋을 재사용).
- 화면 큰 문구 = 카드 제목, 내레이션 = 카드 본문을 '듣기 좋은 말'로 다시 쓴 것, 첫 장면은 3초 훅, 마지막은 연락처 안내.
- 카드에 없는 수치는 만들지 않고(검사), 업종 표현 규칙을 대본에 다시 적용한다.
AI 키가 있으면 Gemini/Claude로 변환하고, 없거나 실패하면 규칙 기반으로 만든다.
"""
import os
import re
import json
import asyncio
import logging
from typing import List, Literal, Optional, Tuple
from pydantic import BaseModel, Field
from models.schema import EditableStoryboard, ShortsScene, CardSlide
from services import emphasis
from services import compliance
from services.project_store import card_hash
from services import usage_log

logger = logging.getLogger(__name__)
CHARS_PER_SEC = 6.5  # 한국어 내레이션 초당 글자 수(공백 포함) 어림값
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:억|만|천)?\s*(?:원권|원|시간|점|%|명|분|자리|주|년|개월|개|곳|배|초|일)?")


class LLMScene(BaseModel):
    source_cards: List[int] = Field(description="이 장면이 가져온 카드 번호 (1부터). 여러 장을 묶으면 여러 개")
    section_type: Literal["hook", "body", "conclusion"]
    narration_script: str = Field(description="귀로 듣기 좋은 한국어 내레이션 (카드 문장을 그대로 읽지 말 것)")
    display_caption: str = Field(description="화면에 크게 보일 문구, 30자 이내 (보통 카드 제목을 줄인 것)")
    emphasis_words: List[str] = Field(default_factory=list, description="display_caption 속 강조 단어 1~2개 (문구에 적힌 그대로)")


class LLMShortsScript(BaseModel):
    scenes: List[LLMScene]


# ----------------------------------------------------------------------
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", emphasis.strip(text).replace("…", "")).strip().rstrip(".")


def _sentence(text: str) -> str:
    t = _clean(text)
    return t if not t or t[-1] in "?!.。" else t + "."


def _join(parts: List[str]) -> str:
    return " ".join(_sentence(p) for p in parts if _clean(p))


def _card_text(c: CardSlide) -> str:
    parts = [f"제목: {emphasis.strip(c.lead_headline)}"]
    if c.stat_value:
        parts.append(f"큰 숫자: {c.stat_value} ({c.stat_label})")
    if c.body_content:
        parts.append("본문: " + " / ".join(x.strip() for x in c.body_content.splitlines() if x.strip()))
    if c.comparison_table:
        parts.append("비교표: " + json.dumps(c.comparison_table, ensure_ascii=False))
    if c.takeaway:
        parts.append(f"요약: {c.takeaway}")
    return f"[카드 {c.slide_number} · {c.slide_type}] " + " | ".join(parts)


def _numbers(text: str) -> set:
    return {re.sub(r"\s+", "", m.group(0)) for m in NUMBER_RE.finditer(text or "") if any(ch.isdigit() for ch in m.group(0))}


# ----------------------------------------------------------------------
# AI 변환
# ----------------------------------------------------------------------
def _prompt(sb: EditableStoryboard, kit, pack, seconds: int, source_text: str = "") -> str:
    cards = "\n".join(_card_text(c) for c in sb.card_slides)
    contact = " / ".join(x for x in [f"전화 {kit.phone}" if kit.phone else "", kit.homepage_short] if x)
    max_scenes = max(3, min(8, seconds // 6))
    return f"""확정된 카드뉴스를 바탕으로 {seconds}초 내외의 세로형 쇼츠 대본을 만드세요.

[확정 카드 — 내용·수치의 유일한 근거]
{cards}

[원문 (참고용 — 카드 문장이 '…'로 잘렸을 때 원래 뜻 확인에만 사용, 카드에 없는 내용을 새로 넣지 말 것)]
{source_text.strip()[:2000] or '(없음)'}

[브랜드]
- 브랜드: {kit.brand_name} / 화자: {kit.persona or '브랜드 담당자'} / 톤: {sb.tone_and_manner or kit.tone}
- 마무리 안내: {kit.cta_narration or kit.cta_headline} ({contact})

[업종·회사 표현 규칙]
{compliance.prompt_rules(pack, kit)}

[작성 규칙]
- 장면 {max_scenes}개 이내. 카드 순서를 지키고, 내용이 짧은 카드는 앞뒤 카드와 묶어 한 장면으로 만들 것 (source_cards에 카드 번호들)
- 첫 장면은 hook: 표지 카드(1번) 내용으로 궁금증을 만드는 한 문장, 내레이션 30자 이내 (약 3~4초)
- 마지막 장면은 conclusion: 마지막 카드(문의·신청 안내)를 바탕으로 연락 방법을 말로 안내
- 내레이션은 카드 문장을 그대로 읽지 말고, 말하듯 자연스럽게 다시 쓸 것. 전체 길이는 한국어 약 {int(seconds * CHARS_PER_SEC)}자
- 카드에 없는 수치·순위·효과는 절대 만들지 말고, 카드의 수치는 한 글자도 바꾸지 말 것
- 수치의 뜻(증가·감소·절감·비용 등)을 바꾸지 말 것. 카드 문장이 '…'로 끝나 뜻이 불분명하면 원문으로 확인하고, 원문에도 없으면 그 내용은 말하지 말 것
- 카드에 없는 평가·수식어(합리적인, 부담 없는, 최고의, 확실한 등)를 덧붙이지 말 것
- display_caption은 30자 이내로, 보통 해당 카드 제목을 줄여 쓸 것
- 모든 문장은 한국어
"""


async def _call_llm(provider: str, api_key: str, prompt: str, kit) -> Tuple[LLMShortsScript, str]:
    if provider == "claude":
        from anthropic import AsyncAnthropic
        from services.ai_director import CLAUDE_MODEL
        client = AsyncAnthropic(api_key=api_key)
        response = await client.beta.messages.parse(
            model=CLAUDE_MODEL, max_tokens=8000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            output_config={"effort": "medium"},
            system=f"당신은 '{kit.brand_name}'의 쇼츠 대본 작가입니다. 카드에 있는 사실만 쓰고 업종 표현 규칙을 지키십시오.",
            messages=[{"role": "user", "content": prompt}], output_format=LLMShortsScript,
        )
        usage_log.record_claude("쇼츠 대본 변환", response.model, response)
        if response.stop_reason in ("refusal", "max_tokens") or response.parsed_output is None:
            raise RuntimeError(f"Claude 응답 불완전 (stop_reason={response.stop_reason})")
        return response.parsed_output, f"claude:{response.model}"
    from google import genai
    from google.genai import types
    from services.ai_director import planning_model
    model = planning_model()
    client = genai.Client(api_key=api_key)
    response = await asyncio.to_thread(
        client.models.generate_content, model=model, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=LLMShortsScript, temperature=0.3),
    )
    usage_log.record_gemini("쇼츠 대본 변환", model, response)
    return LLMShortsScript.model_validate_json(response.text), f"gemini:{model}"


# ----------------------------------------------------------------------
# 규칙 기반 변환 (키가 없거나 AI 호출 실패 시)
# ----------------------------------------------------------------------
def _rule_based(sb: EditableStoryboard, kit, seconds: int) -> List[LLMScene]:
    cards = sb.card_slides
    n = len(cards)
    last_is_cta = n > 1 and cards[-1].slide_type == "call_to_action"
    contact = " ".join(x for x in [f"상담 전화 {kit.phone}" if kit.phone else "", kit.homepage_short] if x)
    cta_text = kit.cta_narration or (_join([cards[-1].lead_headline, contact]) if last_is_cta else
                                     _join([f"자세한 내용은 {kit.brand_name}에 문의하세요", contact]))
    used = set()  # 같은 문장을 여러 장면에서 반복하지 않도록

    def fresh(text: str) -> str:
        key = re.sub(r"\W+", "", _clean(text))
        if not key or any(key in u or u in key for u in used):
            return ""
        used.add(key)
        return _clean(text)

    cover = cards[0]
    hook = fresh(cover.lead_headline) or _clean(cover.lead_headline)
    scenes = [LLMScene(source_cards=[1], section_type="hook", display_caption=hook[:34], narration_script=_sentence(hook))]
    if n == 1:  # 한 장 카드: 본문 줄과 큰 숫자를 장면으로
        lines = ([cover.stat_label] if cover.stat_value else []) + cover.body_content.splitlines()
        for line in lines:
            line = fresh(line)
            if line and len(scenes) < 4:
                scenes.append(LLMScene(source_cards=[1], section_type="body", display_caption=line[:34], narration_script=_sentence(line)))
    else:
        body = list(range(2, n)) if last_is_cta else list(range(2, n + 1))
        slots = max(1, max(3, min(8, seconds // 6)) - 2)
        size = -(-len(body) // slots) if body else 1  # 올림 나눗셈: 장면 수가 넘치면 이웃 카드끼리 묶음
        budget = int(seconds * CHARS_PER_SEC) - len(scenes[0].narration_script) - len(cta_text)
        per_scene = max(25, budget // max(1, -(-len(body) // size)))
        for i in range(0, len(body), size):
            group = body[i:i + size]
            parts: List[str] = []
            for no in group:
                c = cards[no - 1]
                candidates = [c.stat_label if c.stat_value else c.lead_headline] + c.body_content.splitlines()
                for cand in candidates:
                    text = fresh(cand)
                    if text and (not parts or len(" ".join(parts)) + len(text) < per_scene):
                        parts.append(text)
                        if len(parts) >= 2 * len(group):
                            break
            first_card = cards[group[0] - 1]
            caption = _clean(first_card.stat_value or first_card.lead_headline)[:34]
            if not parts and not first_card.stat_value:
                continue  # 앞 장면에서 이미 말한 내용뿐인 카드는 장면을 만들지 않음
            narration = _join(parts) or _sentence(caption)
            scenes.append(LLMScene(source_cards=group, section_type="body", display_caption=caption, narration_script=narration))
    scenes.append(LLMScene(source_cards=[n], section_type="conclusion",
                           display_caption=(f"상담 {kit.phone}" if kit.phone else _clean(cards[-1].lead_headline))[:34],
                           narration_script=cta_text))
    return scenes


# ----------------------------------------------------------------------
async def convert(sb: EditableStoryboard, kit, provider: str, api_key: Optional[str], seconds: int,
                  source_text: str = "") -> List[str]:
    """sb.shorts_scenes를 확정 카드에서 다시 만든다. 반환: 변환 로그"""
    pack = compliance.load_pack(kit.industry_pack)
    logs: List[str] = []
    llm_scenes, engine = None, "rule-based"
    if api_key and sb.card_slides:
        try:
            script, engine = await _call_llm(provider, api_key, _prompt(sb, kit, pack, seconds, source_text), kit)
            llm_scenes = script.scenes
        except Exception as e:
            logger.warning(f"쇼츠 대본 변환 AI 호출 실패: {e}")
            logs.append(f"⚠️ 쇼츠 대본 AI 변환 실패 → 규칙 기반으로 변환 ({type(e).__name__}: {str(e)[:100]})")
    elif not api_key:
        logs.append("ℹ️ API 키가 없어 규칙 기반으로 쇼츠 대본을 만들었습니다 (카드 문장을 짧게 이어 읽는 수준).")
    if not llm_scenes:
        llm_scenes, engine = _rule_based(sb, kit, seconds), "rule-based"

    n = len(sb.card_slides)
    scenes: List[ShortsScene] = []
    for i, s in enumerate(llm_scenes):
        src = [c for c in s.source_cards if 1 <= c <= n] or [min(n, max(1, i + 1))]
        card = sb.card_slides[src[0] - 1]
        asset_card = next((sb.card_slides[c - 1] for c in src if sb.card_slides[c - 1].asset_id), None)
        narration = s.narration_script.strip()
        asset = sb.asset(asset_card.asset_id) if asset_card else None
        clip = 0.0
        if asset is not None and asset.kind == "video" and asset.keyframe_times:  # 카드에 쓴 장면 근처부터
            clip = round(max(0.0, min(asset.keyframe_times[len(asset.keyframe_times) // 2] - 1.0, asset.duration - 3.0)), 2)
        scenes.append(ShortsScene(
            scene_number=i + 1, section_type=s.section_type, narration_script=narration,
            display_caption=s.display_caption.strip()[:40], emphasis_words=s.emphasis_words[:2],
            visual_motion_prompt=card.image_prompt or card.lead_headline,
            duration_seconds=round(max(3.0, len(narration) / CHARS_PER_SEC), 1),
            asset_id=asset_card.asset_id if asset_card else None, clip_start=clip, source_cards=src,
        ))

    # 강조어 검증 (화면 문구에 실제로 있는 단어만)
    from services.ai_director import AIDirectorService
    tmp = sb.model_copy(update={"shorts_scenes": scenes})
    AIDirectorService._ensure_emphasis(AIDirectorService.__new__(AIDirectorService), tmp, kit)

    # 업종 표현 규칙을 대본에만 다시 적용 (확정 카드는 건드리지 않음)
    check = sb.model_copy(deep=True)
    check.card_slides, check.shorts_scenes = [], scenes
    check.title, check.sns_caption = "", ""
    rule_logs = [l for l in compliance.apply(check, pack, kit) if not l.startswith(("🛡️", "📌"))]
    logs += ["🎬 대본 " + l for l in rule_logs]

    # 카드에 없는 수치가 대본에 생겼는지 검사
    card_text = " ".join(_card_text(c) for c in sb.card_slides) + " " + kit.phone
    known = _numbers(card_text)
    for sc in scenes:
        for num in _numbers(sc.narration_script + " " + sc.display_caption) - known:
            if not any(num in k or k in num for k in known):
                logs.append(f"⚠️ 쇼츠 {sc.scene_number} 장면: 카드에 없는 수치 '{num}' — 확인해 주세요.")

    sb.shorts_scenes = scenes
    sb.shorts_source_hash = card_hash(sb)
    sb.shorts_images_ready = True  # 카드 이미지를 그대로 쓰므로 쇼츠용 AI 이미지를 따로 만들지 않음
    total = sum(sc.duration_seconds for sc in scenes)
    logs.insert(0, f"🎬 확정 카드 {n}장 → 쇼츠 {len(scenes)}장면 (약 {total:.0f}초, {engine})")
    sb.fact_check_log = [l for l in sb.fact_check_log if not l.startswith(("🎬", "⚠️ 쇼츠"))] + logs
    return logs


def is_stale(sb: Optional[EditableStoryboard]) -> bool:
    """쇼츠 대본이 지금 카드와 다른 카드에서 만들어졌는지"""
    return bool(sb and sb.shorts_scenes and sb.shorts_source_hash and sb.shorts_source_hash != card_hash(sb))
