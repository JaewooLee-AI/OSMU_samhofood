"""참조 이미지 → 구조·분위기 분석 → 디자인 안 여러 개 → 마음에 드는 안을 템플릿으로 저장.

법적·윤리적 원칙
- 업로드한 참조 이미지는 '분석 호출' 1회에만 쓰고 디스크에 저장하지 않는다. 이미지 생성 모델에는 절대 넘기지 않는다.
- 분석 결과는 일반적인 구조·분위기·그림 스타일 설명(글)만 담도록 지시한다 (브랜드명·캐릭터·로고·문구·구체적 그림 묘사 금지).
- 디자인 안의 샘플 그림은 이 글 설명과 우리 브랜드 정보만으로 새로 그린다.
"""
import os
import re
import asyncio
import logging
import tempfile
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Literal, Optional
from pydantic import BaseModel, Field
from services import usage_log
from services import design_templates as dt

logger = logging.getLogger(__name__)
N_VARIANTS = 3
ProgressFn = Optional[Callable[[str], Awaitable[None]]]


class LLMColors(BaseModel):
    primary: str = Field(description="제목·강조 바에 쓸 진한 주 색 (#RRGGBB, 흰 글씨가 읽힐 만큼 어두운 색)")
    secondary: str = Field(description="보조 색 (#RRGGBB)")
    accent: str = Field(description="포인트 색 (#RRGGBB, 번호 원·라벨용 밝고 눈에 띄는 색)")
    background: str = Field(description="카드 바탕색 (#RRGGBB, 아주 밝은 색)")


class LLMVariant(BaseModel):
    name: str = Field(description="안 이름 (한국어 3~8자, 예: 부드러운 파스텔)")
    style_prompt: str = Field(description="AI 그림 스타일을 일반적으로 설명하는 영어 문장 (예: flat vector illustration, soft pastel palette). "
                                         "특정 브랜드·캐릭터·로고·글자·원본의 구체적 그림을 언급하지 말 것")
    layout: Literal["classic", "illustrated", "color_block", "poster"]
    colors: LLMColors = Field(description="이 안의 색 구성. 참조 이미지의 주요 색에서 가져오되 안마다 다르게")
    radius: int = Field(description="모서리 둥글기 px (0~60)")
    box_style: Literal["filled", "outlined"]
    show_numbers: bool


class LLMAnalysis(BaseModel):
    summary: str = Field(description="한국어 2~3문장: 카드 구성 방식과 분위기 (예: 표지→문제→실천 방법→안내, 글자 적고 그림 큼, 친근한 톤)")
    card_pattern: List[str] = Field(description="카드별 역할 요약 (예: '표지: 제목+큰 그림')")
    text_density: str = Field(description="글자 양: 적음 | 보통 | 많음")
    mood: str = Field(description="분위기 한 줄 (한국어)")
    palette: List[str] = Field(description="참조 이미지의 주요 색 4~6개 (#RRGGBB, 많이 쓰인 순)")
    background_style: str = Field(description="카드 배경 방식 (한국어, 예: '카드마다 주황·머스터드 등 꽉 찬 단색 배경' / '밝은 크림색 바탕')")
    title_style: str = Field(description="제목 글씨 특징 (한국어, 예: '아주 크고 굵은 둥근 고딕, 가운데 정렬, 주변에 작은 장식')")
    decorations: str = Field(description="장식 요소의 종류만 일반적으로 (한국어, 예: '모서리의 작은 계절 아이콘, 하단의 큰 풍경 일러스트'). 구체적 캐릭터·사물 이름은 쓰지 말 것")
    structure_hint: Literal["text_cards", "illustrated_cards", "color_block_list", "poster_cover_mix"] = Field(
        description="참조의 대표 구조: text_cards(글 중심) | illustrated_cards(그림 + 항목 박스) | color_block_list(꽉 찬 색 배경 + 줄 목록/시간표) | poster_cover_mix(큰 제목 표지 + 목록 카드 조합)")
    sample_scene: str = Field(description="참조의 일반적인 주제 분위기에 맞는 샘플 그림 장면을 영어 한 문장으로 (예: 'a calm autumn countryside with hills and a traditional tiled-roof house'). "
                                         "특정 브랜드·캐릭터·로고·글자·실존 인물 없이, 흔한 풍경·사물 수준으로만")
    variants: List[LLMVariant] = Field(description=f"서로 다른 디자인 안 {N_VARIANTS}개")


ANALYSIS_PROMPT = f"""첨부 이미지는 사용자가 '이런 느낌의 카드뉴스를 만들고 싶다'고 올린 참고 자료입니다.
이 이미지를 복제하려는 것이 아니라, 일반적인 '구성 방식과 분위기'만 파악해 새 디자인 템플릿 안을 제안하려는 것입니다.

규칙(반드시 지킬 것)
- 브랜드명·회사명·캐릭터·마스코트·로고를 쓰거나 언급하지 말 것. 이미지 속 글자를 인용하지 말 것.
- 그림 속 구체적인 인물·캐릭터·사물을 묘사하지 말고, '평면 벡터', '파스텔', '둥근 도형' 같은 일반적 스타일만 쓸 것.
- 디자인 안 {N_VARIANTS}개는 서로 분명히 다르게(예: 참고 느낌에 가깝게 / 더 부드럽게 / 더 단정하게) 제안할 것.
- style_prompt는 영어로, 이미지에 글자·로고가 들어가지 않는 일반적인 그림 스타일 설명으로 쓸 것.
- layout: 표지가 큰 제목 + 꽉 찬 색 배경 + 하단 큰 풍경 그림이고 나머지 카드는 색 면 목록이면 poster, 카드 전체가 꽉 찬 색 면에 줄 목록·시간표로 정리된 구성이면 color_block, 그림이 큰 구성이면 illustrated, 글 중심이면 classic.
  안 3개 중 최소 1개는 참조의 대표 구조에 가장 가까운 layout으로 하고, 나머지는 다른 layout을 섞어 비교할 수 있게 할 것.
- sample_scene: 참조가 다루는 일반적 주제(계절·행사·풍경 등)의 분위기를 살린 영어 장면 한 문장. 구체적 캐릭터·브랜드·글자 금지.
- 색은 저작권 대상이 아니므로 참조 이미지의 실제 색을 #RRGGBB로 읽어 palette와 각 안의 colors에 사용할 것.
  안 1은 참조 색에 가깝게, 안 2는 같은 계열을 더 부드럽고 밝게, 안 3은 같은 계열을 더 단정하고 차분하게 조정할 것.
- colors.primary는 흰 글씨가 읽힐 만큼 어둡게, colors.background는 아주 밝게, colors.accent는 primary와 구분되는 밝은 포인트 색으로 할 것.
"""


@dataclass
class Proposal:
    template: dt.DesignTemplate
    image_path: str       # 우리가 새로 그린 샘플 그림 (원본 아님)
    text_leak: bool       # 그림에 글자가 섞였을 가능성 (재생성 후에도)
    variant: Optional["LLMVariant"] = None   # 색 사용 여부를 바꿔 템플릿을 다시 만들 때 사용
    index: int = 0


def estimate_cost(image_model: str) -> float:
    """분석 1회 + 샘플 그림 N장 예상 비용(USD, 단가를 모르면 장당 $0.07로 어림)"""
    prices = usage_log.load_prices()["models"]
    per = (prices.get(f"gemini/{image_model}") or {}).get("per_image", 0.07)
    return round(0.01 + N_VARIANTS * per * 1.3, 2)  # 글자 섞임 재생성 여유 30%


async def analyze(api_key: str, image_bytes: bytes, mime: str) -> LLMAnalysis:
    from google import genai
    from google.genai import types
    from services.ai_director import planning_model
    model = planning_model()
    client = genai.Client(api_key=api_key)
    resp = await asyncio.to_thread(
        client.models.generate_content, model=model,
        contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime), ANALYSIS_PROMPT],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=LLMAnalysis, temperature=0.4))
    usage_log.record_gemini("디자인 분석", model, resp)
    result = LLMAnalysis.model_validate_json(resp.text)
    result.variants = result.variants[:N_VARIANTS]
    return result


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _mix(hex_color: str, to: int, amount: float) -> str:
    h = hex_color.lstrip("#")
    return "#{:02X}{:02X}{:02X}".format(*(int(int(h[i:i + 2], 16) + (to - int(h[i:i + 2], 16)) * amount) for i in (0, 2, 4)))


def fix_colors(c: LLMColors) -> dict:
    """AI가 준 색을 안전하게 보정: 형식 확인, 주 색은 흰 글씨 대비 4.5 이상, 바탕은 충분히 밝게"""
    ok = lambda x: bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", (x or "").strip()))
    vals = {"primary": c.primary, "secondary": c.secondary, "accent": c.accent, "background": c.background}
    out = {k: v.strip().upper() for k, v in vals.items() if ok(v)}
    if "primary" in out:
        for _ in range(8):  # 흰색(휘도 1.0)과의 대비율 (1.05)/(L+0.05) ≥ 4.5 가 될 때까지 어둡게
            if 1.05 / (_lum(out["primary"]) + 0.05) >= 4.5:
                break
            out["primary"] = _mix(out["primary"], 0, 0.15)
    if "background" in out and _lum(out["background"]) < 0.8:
        out["background"] = _mix(out["background"], 255, 0.75)
    return out


def variant_to_template(v: LLMVariant, idx: int, use_ref_colors: bool = True) -> dt.DesignTemplate:
    return dt.DesignTemplate(
        id=f"proposal-{idx + 1}", name=v.name.strip()[:20] or f"디자인 안 {idx + 1}", description="참조 이미지의 구성·분위기를 바탕으로 만든 디자인 안",
        layout=v.layout, radius=max(0, min(60, int(v.radius))), box_style=v.box_style, show_numbers=v.show_numbers,
        illustration_height=440, style_prompt=v.style_prompt.strip(), image_aspect="16:9" if v.layout in ("illustrated", "color_block", "poster") else "4:5",
        colors=fix_colors(v.colors) if use_ref_colors else {})


async def build_proposals(api_key: str, image_model: str, analysis: LLMAnalysis, kit, progress: ProgressFn = None) -> List[Proposal]:
    """디자인 안마다 샘플 그림을 새로 생성 (분석한 글 설명 + 브랜드 맥락만 사용, 참조 이미지는 넘기지 않음)"""
    from google import genai
    from services.image_generator import AIImageGenerator
    from services.media_assets import MediaLibrary
    client = genai.Client(api_key=api_key)
    gen = AIImageGenerator(MediaLibrary(tempfile.mkdtemp(prefix="osmu_proposals_")))
    gen.model_override = image_model
    subject = (getattr(analysis, "sample_scene", "") or "").strip() or kit.visual_context or kit.brand_name
    scene = (f"A friendly, simple scene: {subject}. "
             "Clean composition with plenty of empty space, suitable as a card-news illustration.")
    out: List[Proposal] = []
    for i, v in enumerate(analysis.variants):
        t = variant_to_template(v, i)
        if progress:
            await progress(f"디자인 안 {i + 1}/{len(analysis.variants)} 샘플 그림을 그리는 중...")
        prompt = f"그림 스타일(반드시 따를 것): {t.image_suffix()}\n{scene}"
        data, ext = await gen._generate(client, prompt, t.image_aspect, "디자인 샘플 그림")
        leak = False
        if await gen.has_text(client, data, ext):
            data, ext = await gen._generate(client, prompt, t.image_aspect, "디자인 샘플 그림")
            leak = await gen.has_text(client, data, ext)
        path = os.path.join(tempfile.mkdtemp(prefix="osmu_proposal_"), f"sample{ext}")
        with open(path, "wb") as f:
            f.write(data)
        out.append(Proposal(t, path, leak, v, i))
    return out
