"""AI 디렉터: 프롬프트·첨부 미디어 → 카드뉴스·쇼츠 스토리보드(시나리오).

공통 엔진이다. 회사별 내용(브랜드명·연락처·가치·CTA·해시태그·실명 마스킹·강조 단어·이미지 맥락)은 브랜드 킷에서,
업종별 표현 규칙은 컴플라이언스 팩에서, 장 구성은 카드 형식(card_formats)에서 가져온다.
"""
import logging
import os
import json
import re
import base64
import asyncio
from typing import Optional, List, Dict, Any, Literal, Tuple
from pydantic import BaseModel, Field
from models.schema import UserRequestPayload, EditableStoryboard, CardSlide, ShortsScene, MediaAsset
from services import brand_kit as bk
from services import compliance
from services import usage_log
from services import design_templates
from services.card_formats import get_format, SINGLE_CARD_STRUCTURE, fit_warning

logger = logging.getLogger(__name__)

# 모델 ID는 환경변수로 교체 가능 (모델 지원 종료 시 코드 수정 없이 대응)
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")


# ---------------------------------------------------------------------------
# LLM 구조화 출력 전용 스키마 (비교표는 행 리스트로 받아 변환)
# ---------------------------------------------------------------------------
class LLMCardSlide(BaseModel):
    slide_number: int
    slide_type: Literal["cover", "body", "numbered", "big_number", "comparison_table", "event_info", "call_to_action", "single"]
    lead_headline: str
    body_content: str
    takeaway: str
    image_prompt: str = Field(description="asset_id가 비어 있을 때 AI 이미지 생성에 쓸 장면 묘사 (글자 없는 이미지)")
    asset_id: str = Field(default="", description="이 슬라이드에 배치할 첨부 에셋 ID (예: A1). 적합한 에셋이 없으면 빈 문자열")
    comparison_rows: List[List[str]] = Field(default_factory=list,
                                             description="comparison_table에서만 사용. 첫 행은 헤더, 이후 데이터 행")
    badge: str = Field(default="", description="numbered 카드의 번호 (01, 02 …)")
    stat_value: str = Field(default="", description="big_number/single 카드의 큰 숫자 (원문 그대로)")
    stat_label: str = Field(default="", description="큰 숫자의 한 줄 설명")


class LLMShortsScene(BaseModel):
    scene_number: int
    section_type: Literal["hook", "body", "conclusion"]
    narration_script: str
    display_caption: str
    visual_motion_prompt: str = Field(description="asset_id가 비어 있을 때 AI 이미지 생성에 쓸 장면 묘사")
    duration_seconds: float
    asset_id: str = Field(default="", description="이 씬 화면에 쓸 첨부 에셋 ID. 없으면 빈 문자열")
    clip_start: float = Field(default=0.0, description="동영상 에셋이면 사용 시작 시각(초)")
    emphasis_words: List[str] = Field(default_factory=list, description="display_caption 안에서 크게 강조할 핵심 단어 1~2개 (문구에 있는 그대로)")


class LLMAssetNote(BaseModel):
    asset_id: str
    description: str = Field(description="에셋에 보이는 내용 한 문장 요약 (한국어)")


class LLMStoryboard(BaseModel):
    asset_notes: List[LLMAssetNote] = Field(default_factory=list)
    title: str
    target_audience: str
    tone_and_manner: str
    visual_style: str
    card_slides: List[LLMCardSlide]
    shorts_scenes: List[LLMShortsScene] = Field(default_factory=list, description="비워 둘 것 (쇼츠는 카드 확정 후 따로 만듦)")
    sns_caption: str
    hashtags: List[str]


# 원문에서 "수치형 팩트"를 추출하는 패턴 (금액/시간/점수/비율/전화번호 등)
FACT_REGEX = re.compile(
    r"\d{4}-\d{4}"
    r"|\d[\d,]*(?:\.\d+)?\s*(?:억|만|천)?\s*(?:원권|원|시간|점|%|명|분|자리|주|년|개월|개|곳|배)"
)


def planning_model() -> str:
    """설정 화면에서 고른 Gemini 기획 모델 (auto면 기본값)"""
    m = (bk.load_settings().planning_model or "auto").strip()
    return GEMINI_MODEL if m in ("", "auto") else m


class AIDirectorService:
    """Gemini·Claude 멀티모달 기획 + 규칙 기반 대체 엔진 + 마스킹·팩트 락·컴플라이언스 가드레일"""

    REQUEST_SENTENCE = re.compile(r"(만들|제작|생성|작성|편집)\S*\s*(고\s*싶|해\s*줘|해\s*주세요|주세요|줘|바랍니다|부탁)")
    EMPHASIS_NUMBER = re.compile(r"\d{4}-\d{4}|\d[\d,]*(?:\.\d+)?\s*(?:억|만|천)?\s*(?:원권|원|점|%|시간|분|명|개월|년|곳|배|자리)?")

    def __init__(self):
        self.scenarios_data = self._load_scenarios()

    def _load_scenarios(self) -> List[Dict[str, Any]]:
        """선택 사항: data/scenarios.json 이 있으면 '예시 불러오기'에 사용 (공개 저장소에는 없음)"""
        preset_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "scenarios.json")
        if os.path.exists(preset_path):
            with open(preset_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    # ------------------------------------------------------------------
    # 진입점
    # ------------------------------------------------------------------
    async def generate_storyboard(self, payload: UserRequestPayload) -> EditableStoryboard:
        """시나리오(텍스트 스토리보드) 생성. AI 이미지는 다음 단계(카드 만들기)에서 채운다."""
        kit = bk.load_kit(payload.brand_id) if payload.brand_id else bk.active_kit()
        if design_templates.get(payload.design_id).style_prompt:
            # 일러스트 같은 그림 스타일이 있는 디자인이면 카드마다 그림 스타일이 섞이지 않도록 비주얼 스타일을 맞춤
            payload.visual_style = "2D 일러스트 / 일상툰"
        pack = compliance.load_pack(kit.industry_pack)
        provider = (payload.ai_provider or "gemini").lower()
        api_key = payload.anthropic_api_key if provider == "claude" else payload.gemini_api_key
        source = self._resolve_source(payload, kit)

        storyboard = None
        notes: List[str] = []
        if api_key:
            try:
                if provider == "claude":
                    storyboard = await self._call_claude(payload, source, kit, pack, api_key)
                else:
                    storyboard = await self._call_gemini(payload, source, kit, pack, api_key)
            except Exception as e:
                notes.append(f"⚠️ {provider} 호출 실패 → 규칙 기반 엔진으로 전환 ({type(e).__name__}: {str(e)[:120]})")
                logger.warning(f"{provider} 호출 오류: {e}")
        else:
            notes.append(f"ℹ️ {provider.upper()} API 키 미설정 → 규칙 기반 엔진으로 생성 (문구 품질이 낮을 수 있음)")

        auto_assign = storyboard is None
        if not storyboard:
            storyboard = self._generate_rule_based_storyboard(payload, source, kit)
        storyboard.card_format = get_format(payload.card_format)["id"]
        storyboard.brand_id = kit.id
        storyboard.brand_elements = payload.brand_elements
        storyboard.design_id = payload.design_id or "classic"
        for i, s in enumerate(storyboard.card_slides):
            s.slide_number = i + 1
        storyboard.shorts_scenes = []  # 쇼츠 대본은 카드 확정 후 shorts_from_cards에서 만든다
        self._ensure_emphasis(storyboard, kit)
        self._bind_assets(storyboard, payload, auto_assign, notes)
        return self._apply_guardrails(storyboard, payload, source, notes, kit, pack)

    def _ensure_emphasis(self, sb: EditableStoryboard, kit: bk.BrandKit):
        """씬별 강조어 검증·보완: 문구에 실제로 있는 단어만 남기고, 없으면 숫자 → 브랜드 강조 단어 → 가장 긴 단어"""
        for scene in sb.shorts_scenes:
            caption = scene.display_caption or ""
            words = [w for w in (scene.emphasis_words or []) if w and w.strip() and w.strip() in caption]
            if not words:
                words = [m.group(0).strip() for m in self.EMPHASIS_NUMBER.finditer(caption)][:2]
            if not words:
                words = [k for k in kit.emphasis_keywords if k and k in caption][:1]
            if not words and caption.strip():
                tokens = [t.strip(".,!?·…") for t in caption.split()]
                words = [max(tokens, key=len)] if tokens else []
            scene.emphasis_words = words[:2]

    # ------------------------------------------------------------------
    # 에셋 배치 (LLM 결과 검증 / 규칙 기반 자동 배치)
    # ------------------------------------------------------------------
    def _bind_assets(self, sb: EditableStoryboard, payload: UserRequestPayload, auto: bool, notes: List[str]):
        assets = list(payload.assets)
        sb.assets = assets
        by_id = {a.id: a for a in assets}
        for item in list(sb.card_slides) + list(sb.shorts_scenes):
            if item.asset_id and item.asset_id not in by_id:
                item.asset_id = None
        if not assets:
            return

        if auto or not any(s.asset_id for s in sb.card_slides):
            # 카드: 표·대형 숫자 카드를 제외하고 첨부 순서대로 1회씩 배치
            eligible = [s for s in sb.card_slides if s.slide_type not in ("comparison_table", "big_number", "event_info")]
            for slide, asset in zip(eligible, assets):
                slide.asset_id = asset.id
        if auto or not any(sc.asset_id for sc in sb.shorts_scenes):
            # 쇼츠: 동영상을 우선 배치하고, 남는 씬은 동영상의 다른 구간을 재사용
            ordered = [a for a in assets if a.kind == "video"] + [a for a in assets if a.kind == "image"]
            videos = [a for a in assets if a.kind == "video"]
            for i, scene in enumerate(sb.shorts_scenes):
                if i < len(ordered):
                    scene.asset_id = ordered[i].id
                elif videos:
                    scene.asset_id = videos[i % len(videos)].id

        # 동영상 사용 구간: 같은 영상이 여러 씬에 쓰이면 서로 다른 구간에서 시작
        usage: Dict[str, int] = {}
        for scene in sb.shorts_scenes:
            asset = by_id.get(scene.asset_id or "")
            if not asset or asset.kind != "video":
                scene.clip_start = 0.0
                continue
            k = usage.get(asset.id, 0)
            usage[asset.id] = k + 1
            if auto or scene.clip_start <= 0 and k > 0:
                times = asset.keyframe_times or [0.0]
                scene.clip_start = max(0.0, times[k % len(times)] - 0.5)
            scene.clip_start = round(min(max(0.0, scene.clip_start), max(0.0, asset.duration - 1.0)), 2)

        used = {i.asset_id for i in list(sb.card_slides) + list(sb.shorts_scenes) if i.asset_id}
        unused = [a.id for a in assets if a.id not in used]
        notes.append(f"🖼️ 첨부 에셋 {len(assets)}개 배치 완료" + (f" (미사용: {', '.join(unused)})" if unused else ""))

    # ------------------------------------------------------------------
    # 원천 텍스트 해석 (예시 시나리오 / 사용자가 편집한 텍스트 / 자유 입력)
    # ------------------------------------------------------------------
    def _resolve_source(self, payload: UserRequestPayload, kit: bk.BrandKit) -> Dict[str, Any]:
        scenario = None
        if payload.scenario_id:
            scenario = next((s for s in self.scenarios_data if s.get("id") == payload.scenario_id), None)

        text = (payload.raw_text or "").strip()
        title_match = re.match(r"^\[(.+?)\]\s*$", text.splitlines()[0]) if text else None
        sections = self._parse_sections(text)
        edited = False

        if sections:
            situation, development, result = sections.get("상황", ""), sections.get("전개", ""), sections.get("결과", "")
            title = title_match.group(1) if title_match else (scenario.get("title") if scenario else "")
            if scenario and not self._same_as_preset(scenario, situation, development, result):
                edited = True  # 원문을 고쳤다면 예시의 팩트 대신 편집된 텍스트의 수치로 검증
        elif scenario and (not text or text == scenario.get("situation", "").strip()):
            situation, development, result = scenario.get("situation", ""), scenario.get("development", ""), scenario.get("result", "")
            title = scenario.get("title", "")
        else:
            scenario = None
            sentences = self._split_sentences(text)
            # "~ 만들어 주세요" 같은 제작 요청 문장은 본문에서 제외 (모두 요청문이면 그대로 사용)
            sentences = [x for x in sentences if not self.REQUEST_SENTENCE.search(x)] or sentences
            n = len(sentences)
            a, b = max(1, round(n / 3)), max(1, round(n * 2 / 3))
            situation, development, result = " ".join(sentences[:a]), " ".join(sentences[a:b]), " ".join(sentences[b:])
            title = title_match.group(1) if title_match else (self._headline(sentences[0], 30) if sentences else "")

        title = title or kit.brand_name
        core_title = re.sub(r"^시나리오\s*\d+\s*:\s*", "", title).strip()
        fact_locks = scenario.get("fact_locks", {}) if scenario and not edited else {}
        source_text = " ".join([situation, development, result] + list(fact_locks.values()))
        return {
            "scenario": scenario, "title": title, "core_title": core_title,
            "situation": situation.strip(), "development": development.strip(), "result": result.strip(),
            "fact_locks": fact_locks, "facts": self._extract_facts(source_text),
        }

    @staticmethod
    def _same_as_preset(scenario: Dict[str, Any], situation: str, development: str, result: str) -> bool:
        norm = lambda t: re.sub(r"\s+", "", t or "")
        return (norm(situation) == norm(scenario.get("situation"))
                and norm(development) == norm(scenario.get("development"))
                and norm(result) == norm(scenario.get("result")))

    @staticmethod
    def _parse_sections(text: str) -> Dict[str, str]:
        sections: Dict[str, str] = {}
        current = None
        for line in text.splitlines():
            m = re.match(r"^\s*(상황|전개|결과)\s*[:：]\s*(.*)$", line)
            if m:
                current = m.group(1)
                sections[current] = m.group(2).strip()
            elif current and line.strip():
                sections[current] += " " + line.strip()
        return sections

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        text = re.sub(r"\s+", " ", text or "").strip()
        if not text:
            return []
        return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]

    @staticmethod
    def _headline(sentence: str, limit: int = 38) -> str:
        """문장을 헤드라인 길이로 다듬기 (쉼표 절 우선, 그 다음 단어 경계)"""
        s = sentence.strip().rstrip(".")
        if len(s) <= limit:
            return s
        first_clause = re.split(r"[,，](?!\d)\s*", s)[0]
        if 8 <= len(first_clause) <= limit:
            return first_clause
        cut = s[:limit]
        if " " in cut:
            cut = cut[: cut.rfind(" ")]
        return cut + "…"

    @staticmethod
    def _extract_facts(text: str) -> List[str]:
        seen, facts = set(), []
        for m in FACT_REGEX.finditer(text or ""):
            key = re.sub(r"\s+", "", m.group(0))
            if key not in seen:
                seen.add(key)
                facts.append(m.group(0).strip())
        return facts

    # ------------------------------------------------------------------
    # LLM 호출
    # ------------------------------------------------------------------
    async def _call_gemini(self, payload, source, kit, pack, api_key: str) -> Optional[EditableStoryboard]:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        model = planning_model()
        contents: List[Any] = [self._build_llm_prompt(payload, source, kit, pack)]
        for label, frames in self._asset_inputs(payload.assets):
            contents.append(label)
            for data in frames:
                contents.append(types.Part.from_bytes(data=data, mime_type="image/jpeg"))
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=LLMStoryboard, temperature=0.2),
        )
        usage_log.record_gemini("시나리오 기획", model, response)
        return self._from_llm(LLMStoryboard.model_validate_json(response.text), payload, f"gemini:{model}")

    async def _call_claude(self, payload, source, kit, pack, api_key: str) -> Optional[EditableStoryboard]:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=api_key)
        content: List[Dict[str, Any]] = [{"type": "text", "text": self._build_llm_prompt(payload, source, kit, pack)}]
        for label, frames in self._asset_inputs(payload.assets):
            content.append({"type": "text", "text": label})
            for data in frames:
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(data).decode()}})
        response = await client.beta.messages.parse(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=f"당신은 '{kit.brand_name}'의 마케팅 콘텐츠 디렉터입니다. 원문과 브랜드 킷의 수치를 바꾸거나 새로 만들지 말고, 업종 표현 규칙을 지키십시오.",
            messages=[{"role": "user", "content": content}],
            output_format=LLMStoryboard,
        )
        usage_log.record_claude("시나리오 기획", response.model, response)
        if response.stop_reason in ("refusal", "max_tokens") or response.parsed_output is None:
            raise RuntimeError(f"Claude 응답 불완전 (stop_reason={response.stop_reason})")
        return self._from_llm(response.parsed_output, payload, f"claude:{response.model}")

    @staticmethod
    def _asset_inputs(assets: List[MediaAsset]):
        """(설명 라벨, 대표 프레임 JPEG 바이트 목록) — 이미지는 1장, 동영상은 시점별 여러 장"""
        for a in assets:
            if a.kind == "video":
                times = ", ".join(f"{t:.1f}초" for t in a.keyframe_times)
                label = f"[{a.id}] 동영상 '{a.original_name}' ({a.width}x{a.height}, 길이 {a.duration:.1f}초). 아래 프레임은 각각 {times} 시점입니다."
            else:
                label = f"[{a.id}] 이미지 '{a.original_name}' ({a.width}x{a.height})"
            frames = []
            for kf in a.keyframes:
                with open(kf, "rb") as f:
                    frames.append(f.read())
            yield label, frames

    def _from_llm(self, llm_sb: LLMStoryboard, payload: UserRequestPayload, engine: str) -> EditableStoryboard:
        notes = {n.asset_id: n.description for n in llm_sb.asset_notes}
        for a in payload.assets:
            if notes.get(a.id):
                a.description = notes[a.id]
        slides = []
        nl = lambda t: (t or "").replace("\\n", "\n")   # 모델이 줄바꿈을 글자 '\n'으로 돌려줄 때가 있음
        for s in llm_sb.card_slides:
            table = {row[0]: row[1:] for row in s.comparison_rows if row} if len(s.comparison_rows) >= 2 else None
            slides.append(CardSlide(
                slide_number=s.slide_number, slide_type=s.slide_type, lead_headline=nl(s.lead_headline).replace("\n", " "),
                body_content=nl(s.body_content), takeaway=nl(s.takeaway).replace("\n", " "), image_prompt=s.image_prompt,
                asset_id=s.asset_id or None, comparison_table=table,
                badge=s.badge, stat_value=s.stat_value, stat_label=s.stat_label,
            ))
        if payload.card_count == 1:
            slides = slides[:1]
            if slides:
                slides[0].slide_type = "single"
        scenes = [ShortsScene(**{**sc.model_dump(), "asset_id": sc.asset_id or None}) for sc in llm_sb.shorts_scenes]
        return EditableStoryboard(
            title=llm_sb.title, target_audience=llm_sb.target_audience, tone_and_manner=llm_sb.tone_and_manner,
            visual_style=llm_sb.visual_style, is_anonymized=payload.anonymize, card_slides=slides,
            shorts_scenes=scenes, sns_caption=llm_sb.sns_caption, hashtags=llm_sb.hashtags, engine=engine,
        )

    def _build_llm_prompt(self, payload: UserRequestPayload, source: Dict[str, Any], kit: bk.BrandKit, pack) -> str:
        fmt = get_format(payload.card_format)
        facts = "\n".join(f"- {v}" for v in list(source["fact_locks"].values()) + kit.core_facts) or "- (원문 수치만 사용)"
        if payload.card_count == 1:
            structure = f"카드뉴스 1장: {SINGLE_CARD_STRUCTURE}"
        else:
            structure = f"카드뉴스 {payload.card_count}장, 형식 '{fmt['name']}': {fmt['llm_structure']}"
        if payload.assets:
            asset_rule = (
                f"- 첨부 에셋({', '.join(a.id for a in payload.assets)})의 실제 내용을 보고, 내용이 맞는 슬라이드·씬의 asset_id에 배치할 것\n"
                "- 동영상 에셋은 프레임 시점을 참고해 해당 장면이 나오는 clip_start(초)를 지정할 것\n"
                "- 카피는 첨부 에셋에 보이는 장면과 어긋나지 않게 쓰고, asset_notes에 각 에셋 내용을 요약할 것\n"
                "- 적합한 에셋이 없는 슬라이드·씬은 asset_id를 빈 문자열로 두고 image_prompt/visual_motion_prompt에 장면을 구체적으로 묘사할 것"
            )
        else:
            asset_rule = "- 첨부 에셋이 없으므로 모든 asset_id는 빈 문자열로 두고 image_prompt/visual_motion_prompt에 장면을 구체적으로 묘사할 것"
        mask_rule = (f"- 실명 마스킹: 켜짐 — 다음 실명은 오른쪽 표기로 바꿔 쓸 것: " + ", ".join(f"{k}→{v}" for k, v in kit.mask_map.items())
                     if payload.anonymize and kit.mask_map else "- 실명 마스킹: 해당 없음")
        contact = " / ".join(x for x in [f"전화 {kit.phone}" if kit.phone else "", kit.homepage_short] if x)
        return f"""다음은 브랜드 '{kit.brand_name}'의 카드뉴스·쇼츠 콘텐츠 생성 요청입니다.

[브랜드 킷]
- 한 줄 소개: {kit.tagline}
- 화자(페르소나): {kit.persona}
- 기본 톤앤매너: {kit.tone}
- 핵심 가치: {' / '.join(kit.value_points)}
- 마무리 행동 유도: {kit.cta_headline} ({contact})

[사용자 프롬프트 원문]
{payload.raw_text.strip()}

[원문 해석 참고]
- 제목: {source['title']}
- 상황: {source['situation']}
- 전개: {source['development']}
- 결과: {source['result']}

[근거로 쓸 수 있는 사실 — 이 밖의 수치·순위는 만들지 말 것]
{facts}

[업종·회사 표현 규칙]
{compliance.prompt_rules(pack, kit)}

[설정]
- 타겟 오디언스: {payload.target_audience}
- 톤앤매너: {payload.tone_and_manner}
- 비주얼 스타일: {payload.visual_style}
{mask_rule}
- 팩트 락: {payload.fact_lock} (True면 원문의 금액·시간·비율·점수·전화번호를 한 글자도 바꾸지 말 것)

[작성 규칙]
- {structure}
- 각 lead_headline은 40자 이내, body_content는 줄바꿈으로 구분된 2~4줄, takeaway는 30자 이내
- 무엇이 바뀌는지(이전과 이후)를 말하는 줄은 body_content에 '항목: 이전 → 이후' 또는 '이전 → 이후' 형식으로 한 줄에 쓸 것. 기간·날짜가 있는 항목은 줄 끝에 날짜를 붙일 것 (예: '동행축제: 소상공인 할인전 10.29.~11.15.')
- lead_headline에서 가장 중요한 핵심 어구 하나(2~12자)를 `*포함·불포함*`처럼 별표로 감쌀 것 (카드에서 형광펜으로 강조됨). 별표는 한 제목에 한 쌍만, 다른 필드에는 쓰지 말 것
- 원문(과 브랜드 킷 사실)에 없는 구체적 사실을 새로 만들지 말 것: 음식·장소·야경·날씨·사람(친척, 아이 등)·감정·고객 발언·경험 묘사를 지어내지 않는다. 문장을 풍성하게 하려고 내용을 보태지 말고, 원문이 짧으면 카드 장수를 줄이거나 같은 내용을 나눠 쓴다
- body_content의 각 줄은 원문 문장을 짧게 다듬은 것이어야 한다. 원문에 없는 이유·효과·장점·방법(예: '더 알뜰하다', '전담 가이드', '전용 차량')을 덧붙이지 말고, 줄을 채우려고 꾸밈말 문장을 만들지 말 것. 쓸 내용이 부족하면 줄 수를 1~2줄로 줄일 것
- 원문에 '상황·전개·결과'(또는 문제·해결·결과)가 있으면 세 부분이 모두 카드에 반영되어야 한다. 특히 '결과'를 빠뜨리지 말 것
- 비교표(comparison_table)는 원문에 비교 대상 두 가지가 직접 적혀 있을 때만 만든다. 원문에 없는 '일반 패키지·다른 업체' 쪽 내용을 지어내거나 다른 업체를 깎아내리지 말 것
- 이번 단계는 카드뉴스만 기획합니다. shorts_scenes는 빈 목록으로 둘 것 (쇼츠 대본은 카드를 확정한 뒤 카드에서 만듭니다)
{asset_rule}
- image_prompt/visual_motion_prompt에는 여권·서류·티켓·지도·간판·화면처럼 글자가 들어가기 쉬운 소재를 쓰지 말고, 사람·표정·장소·사물의 모양만 묘사할 것
- image_prompt/visual_motion_prompt는 이미지 안에 글자가 들어가지 않는 장면 묘사로 작성 (업종 맥락: {kit.visual_context})
- 연락처·홈페이지는 카드 본문에 반복해 쓰지 말 것 (브랜드 요소로 따로 표시됨). 마지막 씬 내레이션에는 연락 방법을 포함
- 모든 문장은 한국어로 작성
"""

    # ------------------------------------------------------------------
    # 규칙 기반 엔진 (API 키가 없거나 호출 실패 시) — 형식별 장 구성
    # ------------------------------------------------------------------
    def _generate_rule_based_storyboard(self, payload: UserRequestPayload, source: Dict[str, Any], kit: bk.BrandKit) -> EditableStoryboard:
        scenario = source["scenario"]
        core_title = source["core_title"]
        target = payload.target_audience if payload.target_audience and payload.target_audience != "AI 자동 추천" else (
            scenario.get("target_audience") if scenario else (kit.target_audiences[0] if kit.target_audiences else "잠재 고객"))
        tone = payload.tone_and_manner if payload.tone_and_manner and payload.tone_and_manner != "AI 자동 추천" else (
            scenario.get("default_tone") if scenario else (kit.tone or "정보 전달형"))
        style = payload.visual_style or "실사풍 / 인포그래픽"

        sit = self._split_sentences(source["situation"])
        dev = self._split_sentences(source["development"])
        res = self._split_sentences(source["result"])
        ctx = {"core_title": core_title, "cover_headline": self._headline(core_title, 40), "target": target,
               "sit": sit, "dev": dev, "res": res, "all": sit + dev + res,
               "fact_values": list(source["fact_locks"].values()), "facts": source["facts"], "kit": kit,
               "raw": payload.raw_text}

        fmt = get_format(payload.card_format)["id"]
        if fmt == "event_notice":  # 줄 단위 원고: 첫 줄이 제목, '항목: 값' 줄은 안내 항목
            lines = [l.strip() for l in (payload.raw_text or "").splitlines() if l.strip()]
            if lines and not re.search(r"[:：]", lines[0]) and len(lines[0]) <= 50:
                core_title = lines[0]
                ctx["core_title"], ctx["cover_headline"] = core_title, self._headline(core_title, 40)
            labeled = [l for l in lines if re.match(r"^[^:：]{1,10}[:：]", l)]
            ctx["sit"] = [l for l in lines[1:] if l not in labeled][:2]
            ctx["all"] = ctx["sit"] + ctx["all"][len(sit + dev + res):]
        if payload.card_count == 1:
            slides = [self._single_card(ctx)]
        else:
            builder = {"info_summary": self._slides_info_summary, "comparison": self._slides_comparison,
                       "big_number": self._slides_big_number, "event_notice": self._slides_event}.get(fmt, self._slides_case_study)
            slides = builder(ctx)
            slides.append(self._cta_slide(kit))
            slides = self._fit_card_count(slides, payload.card_count, ctx)
        for slide in slides:
            if slide.slide_type not in ("comparison_table", "event_info"):
                slide.image_prompt = self._visual_prompt(f"{slide.lead_headline}. {slide.body_content}", style, kit)

        scenes = self._rule_scenes(ctx, payload, tone, style)
        bullets = "\n".join(f"✔️ {v}" for v in ctx["fact_values"]) or \
            "\n".join(f"✔️ {self._headline(x[0], 40)}" for x in (sit, dev, res) if x)
        contact = " | ".join(x for x in [f"📞 {kit.phone}" if kit.phone else "", f"🌐 {kit.homepage_short}" if kit.homepage else ""] if x)
        sns_caption = f"[{core_title}]\n\n{source['situation']}\n\n{bullets}\n\n{res[0] if res else ''}\n\n{contact}".strip()
        return EditableStoryboard(
            title=core_title, target_audience=target, tone_and_manner=tone, visual_style=style,
            is_anonymized=payload.anonymize, card_slides=slides, shorts_scenes=scenes, sns_caption=sns_caption,
            hashtags=list(dict.fromkeys(kit.default_hashtags)), engine="rule-based",
        )

    SECTION_LABELS = {"상황": "지금 무슨 일이 일어나고 있나?", "전개": "어떻게 해결했나?", "결과": "무엇이 달라졌나?"}

    def _section_slide(self, ctx, sentences: List[str], tag: str, takeaway: str) -> CardSlide:
        if not sentences:
            sentences = [self.SECTION_LABELS[tag]]
        headline = self._headline(sentences[0])
        cover = ctx["cover_headline"]
        if headline.rstrip("…") in cover or cover.rstrip("…") in headline:
            headline, body = self.SECTION_LABELS[tag], sentences
        else:
            body = sentences[1:] if len(sentences) > 1 else sentences
        return CardSlide(slide_number=0, slide_type="body", lead_headline=headline, body_content="\n".join(body), takeaway=takeaway)

    def _cover(self, ctx, headline: Optional[str] = None, extra: Optional[List[str]] = None) -> CardSlide:
        body = [f"대상: {ctx['target']}"] + (extra or []) + ctx["sit"][:1]
        return CardSlide(slide_number=0, slide_type="cover", lead_headline=headline or ctx["cover_headline"],
                         body_content="\n".join(body), takeaway=ctx["kit"].tagline or ctx["kit"].brand_name)

    def _value_slide(self, kit: bk.BrandKit) -> Optional[CardSlide]:
        if not kit.value_points:
            return None
        return CardSlide(slide_number=0, slide_type="body", lead_headline=kit.value_headline or f"왜 {kit.brand_name}인가?",
                         body_content="\n".join(kit.value_points), takeaway=kit.tagline or kit.brand_name)

    def _cta_slide(self, kit: bk.BrandKit) -> CardSlide:
        return CardSlide(slide_number=0, slide_type="call_to_action", lead_headline=kit.cta_headline or "지금 문의하세요",
                         body_content="\n".join(kit.cta_body), takeaway=kit.cta_takeaway or kit.brand_name)

    def _slides_case_study(self, ctx) -> List[CardSlide]:
        fv = ctx["fact_values"]
        slides = [self._cover(ctx, extra=[f"핵심: {fv[0]}"] if fv else []),
                  self._section_slide(ctx, ctx["sit"], "상황", self._headline(fv[1], 30) if len(fv) > 1 else "지금 이 문제, 해결책이 있습니다"),
                  self._section_slide(ctx, ctx["dev"], "전개", self._headline(fv[2], 30) if len(fv) > 2 else "이렇게 해결했습니다")]
        if fv:
            table = {"구분": ["검증된 핵심 사실"]}
            for i, v in enumerate(fv, 1):
                table[f"핵심 {i}"] = [v]
            slides.append(CardSlide(slide_number=0, slide_type="comparison_table", lead_headline="숫자로 확인하는 핵심 사실",
                                    body_content="\n".join(fv), takeaway="모든 수치는 원문 그대로 보존됩니다", comparison_table=table))
        slides.append(self._section_slide(ctx, ctx["res"], "결과", "변화는 이미 시작되었습니다"))
        value = self._value_slide(ctx["kit"])
        return slides + ([value] if value else [])

    def _slides_info_summary(self, ctx) -> List[CardSlide]:
        points = [s for s in ctx["all"] if s] or [ctx["core_title"]]
        points = points[:6]
        slides = [self._cover(ctx, headline=self._headline(f"{ctx['core_title']} 핵심 {len(points)}가지", 40))]
        for i, p in enumerate(points, 1):
            slides.append(CardSlide(slide_number=0, slide_type="numbered", badge=f"{i:02d}",
                                    lead_headline=self._headline(p, 34), body_content=p if len(p) > 34 else "",
                                    takeaway=f"핵심 {i:02d}"))
        return slides

    def _slides_comparison(self, ctx) -> List[CardSlide]:
        before = ctx["sit"] or [""]
        after = ctx["res"] or ctx["dev"] or [""]
        rows = {"구분": ["이전", "이후"]}
        for i in range(max(1, min(4, max(len(before), len(after))))):
            rows[f"비교 {i + 1}"] = [self._headline(before[i], 22) if i < len(before) else "-",
                                     self._headline(after[i], 22) if i < len(after) else "-"]
        slides = [self._cover(ctx, headline=self._headline(f"이전 vs 이후: {ctx['core_title']}", 40)),
                  CardSlide(slide_number=0, slide_type="comparison_table", lead_headline="한눈에 보는 차이",
                            body_content="", takeaway="무엇이 달라졌는지 비교해 보세요", comparison_table=rows),
                  self._section_slide(ctx, ctx["dev"], "전개", "차이를 만든 방법"),
                  self._section_slide(ctx, ctx["res"], "결과", "달라진 결과")]
        value = self._value_slide(ctx["kit"])
        return slides + ([value] if value else [])

    def _slides_big_number(self, ctx) -> List[CardSlide]:
        facts = ctx["facts"] or ctx["fact_values"]
        slides = [self._cover(ctx)]
        for fact in facts[:6]:
            sentence = next((s for s in ctx["all"] if fact.replace(" ", "") in s.replace(" ", "")), ctx["core_title"])
            slides.append(CardSlide(slide_number=0, slide_type="big_number", stat_value=fact,
                                    stat_label=self._headline(sentence, 40), lead_headline=self._headline(sentence, 34),
                                    body_content="", takeaway="제공 자료에 나온 수치 그대로입니다"))
        if len(slides) == 1:  # 원문에 수치가 없으면 사례 연구 구성으로 대체
            return self._slides_case_study(ctx)
        return slides

    EVENT_LABELS = {
        "일정": ("일시", "일정", "날짜", "기간", "시간", "행사일", "진행"),
        "대상": ("대상", "참여대상", "참가대상", "모집대상", "인원", "모집인원"),
        "장소": ("장소", "위치", "집결"),
        "혜택": ("혜택", "경품", "선물", "할인", "내용", "프로그램"),
        "조건": ("참가비", "비용", "요금", "조건", "준비물"),
        "신청": ("신청", "접수", "참여방법", "참여 방법", "신청방법", "신청 방법", "방법"),
        "마감": ("마감", "신청마감", "접수마감"),
        "문의": ("문의", "연락처", "상담"),
        "유의": ("유의", "유의사항", "주의", "참고", "안내사항"),
    }

    def _event_fields(self, raw: str) -> Dict[str, List[Tuple[str, str]]]:
        """원고의 '항목: 값' 줄을 그룹별로 모음. 값은 한 줄 그대로(원문 보존)"""
        groups: Dict[str, List[Tuple[str, str]]] = {k: [] for k in self.EVENT_LABELS}
        for line in (raw or "").splitlines():
            m = re.match(r"^[\s\-•·▶✔✅\d.)]*\[?([가-힣A-Za-z ]{1,10})\]?\s*[:：]\s*(.+)$", line.strip())
            if not m:
                continue
            label, value = m.group(1).strip(), m.group(2).strip()
            for group, names in self.EVENT_LABELS.items():
                if any(label == n or label.replace(" ", "") == n.replace(" ", "") for n in names):
                    groups[group].append((label, value))
                    break
        return groups

    def _slides_event(self, ctx) -> List[CardSlide]:
        kit = ctx["kit"]
        g = self._event_fields(ctx["raw"])
        info_groups = ("일정", "대상", "장소", "혜택", "조건", "마감", "문의")
        info = [f"{label}: {value}" for key in info_groups for label, value in g[key]]
        steps = [s.strip() for _, v in g["신청"] for s in re.split(r"\s*(?:→|->|>|,|·|/|;)\s*", v) if s.strip()]
        notes = [v for _, v in g["유의"]]
        sentences = ctx["all"]
        if len(info) < 2:  # '항목: 값' 형식이 아니면 문장에서 가져옴 (정확도는 낮음)
            info += [f"안내: {self._headline(s, 40)}" for s in sentences[:4]]
        title = ctx["cover_headline"]
        if g["일정"] and not any(g["일정"][0][1] in x for x in [title]):
            sub = g["일정"][0][1]
        else:
            sub = ""
        slides = [CardSlide(slide_number=0, slide_type="cover", lead_headline=title,
                            body_content="\n".join(([f"일시: {sub}"] if sub else []) + ctx["sit"][:1]),
                            takeaway=kit.tagline or kit.brand_name),
                  CardSlide(slide_number=0, slide_type="event_info", lead_headline="한눈에 보는 안내",
                            body_content="\n".join(info[:6]), takeaway="신청 전에 일시와 대상을 꼭 확인하세요")]
        if steps:
            slides.append(CardSlide(slide_number=0, slide_type="body", lead_headline="이렇게 참여하세요",
                                    body_content="\n".join(f"{i}. {s}" for i, s in enumerate(steps[:5], 1)),
                                    takeaway="순서대로 따라 하면 쉬워요"))
        if notes or g["마감"]:
            slides.append(CardSlide(slide_number=0, slide_type="body", lead_headline="꼭 확인해 주세요",
                                    body_content="\n".join(notes[:4] + [f"{l}: {v}" for l, v in g["마감"]][:2]),
                                    takeaway="조건과 마감일을 확인해 주세요"))
        return slides

    def _single_card(self, ctx) -> CardSlide:
        kit = ctx["kit"]
        points = [self._headline(x[0], 36) for x in (ctx["sit"], ctx["dev"], ctx["res"]) if x][:3]
        stat = ctx["facts"][0] if ctx["facts"] else ""
        label = next((s for s in ctx["all"] if stat and stat.replace(" ", "") in s.replace(" ", "")), "")
        return CardSlide(slide_number=1, slide_type="single", lead_headline=ctx["cover_headline"],
                         body_content="\n".join(points), takeaway=kit.cta_takeaway or kit.tagline,
                         stat_value=stat, stat_label=self._headline(label, 30) if label else "")

    def _fit_card_count(self, slides: List[CardSlide], count: int, ctx) -> List[CardSlide]:
        """요청 장수(4~10장)에 맞추기: 많으면 보조 카드(가치·표)부터 빼고, 적으면 남은 문장으로 카드를 채움"""
        count = max(4, min(10, count or 7))
        kit = ctx["kit"]
        value_heads = {kit.value_headline, f"왜 {kit.brand_name}인가?"}
        for check in (lambda sl: sl.slide_type == "body" and sl.lead_headline in value_heads,
                      lambda sl: sl.slide_type == "comparison_table" and sl.lead_headline == "숫자로 확인하는 핵심 사실"):
            if len(slides) > count:
                slides = [sl for sl in slides if not check(sl)]
        if len(slides) > count:
            slides = slides[:count - 1] + [slides[-1]]  # CTA는 항상 마지막에 유지
        used = " ".join(s.lead_headline + s.body_content for s in slides)
        fillers = [CardSlide(slide_number=0, slide_type="body", lead_headline=self._headline(sent, 34),
                             body_content=sent if len(sent) > 34 else "", takeaway="핵심 포인트")
                   for sent in ctx["all"] if sent[:12] not in used]
        # 남은 문장이 부족하면: 핵심 요약 카드 → 브랜드 킷의 핵심 사실 순으로 채움 (같은 내용 반복 방지)
        heads = [s.lead_headline for s in slides[1:-1] if s.lead_headline][:4]
        if heads:
            fillers.append(CardSlide(slide_number=0, slide_type="body", lead_headline="한눈에 보는 핵심 정리",
                                     body_content="\n".join(heads), takeaway=kit.tagline or kit.brand_name))
        fillers += [CardSlide(slide_number=0, slide_type="body", lead_headline=self._headline(f, 34),
                              body_content=f if len(f) > 34 else "", takeaway=kit.brand_name) for f in kit.core_facts]
        i = 0
        while len(slides) < count:
            if i < len(fillers):
                filler = fillers[i]
            else:  # 그래도 모자라면 마지막 수단으로 원문 문장을 다시 사용
                sent = (ctx["all"] or [ctx["core_title"]])[i % max(1, len(ctx["all"]))]
                filler = CardSlide(slide_number=0, slide_type="body", lead_headline=self._headline(sent, 34),
                                   body_content="", takeaway="핵심 포인트")
            slides.insert(len(slides) - 1, filler)
            i += 1
        return slides

    def _rule_scenes(self, ctx, payload: UserRequestPayload, tone: str, style: str) -> List[ShortsScene]:
        kit = ctx["kit"]
        cta_narr = kit.cta_narration or f"자세한 내용은 {kit.brand_name}에 문의하세요."
        budget = max(60, int(payload.shorts_seconds * 6.5))
        body_budget = max(20, (budget - len(ctx["core_title"]) - len(cta_narr) - 10) // 3)

        def within(sentences: List[str]) -> str:
            out = ""
            for sent in sentences:
                if out and len(out) + len(sent) + 1 > body_budget:
                    break
                out = f"{out} {sent}".strip()
            return out

        def head(sentences: List[str], tag: str) -> str:
            h = self._headline(sentences[0]) if sentences else self.SECTION_LABELS[tag]
            return self.SECTION_LABELS[tag] if h.rstrip("…") in ctx["cover_headline"] else h

        def scene(no, section, narration, caption, motion):
            narration = narration.strip() or caption
            return ShortsScene(scene_number=no, section_type=section, narration_script=narration,
                               display_caption=self._headline(caption, 34),
                               visual_motion_prompt=self._visual_prompt(f"{caption}. {narration}", style, kit, motion),
                               duration_seconds=round(max(3.0, len(narration) / 7.0), 1))

        cta_caption = f"상담 {kit.phone}" if kit.phone else (kit.homepage_short or kit.cta_headline or kit.brand_name)
        return [
            scene(1, "hook", f"{self._tone_intro(tone)} {ctx['core_title']}.".strip(), ctx["core_title"], "Fast zoom-in hook"),
            scene(2, "body", within(ctx["sit"]), head(ctx["sit"], "상황"), "problem situation"),
            scene(3, "body", within(ctx["dev"]), head(ctx["dev"], "전개"), "solution in action"),
            scene(4, "body", within(ctx["res"]), head(ctx["res"], "결과"), "positive outcome"),
            scene(5, "conclusion", cta_narr, cta_caption, "Call-to-action end card"),
        ]

    @staticmethod
    def _visual_prompt(text: str, style: str, kit: bk.BrandKit, extra: str = "") -> str:
        """AI 이미지 생성용 장면 묘사 (브랜드 킷의 업종 맥락 + 글자 없는 이미지)"""
        style_hint = "2D flat illustration, webtoon style" if any(k in style for k in ("일러스트", "툰", "만화")) else \
            "clean modern infographic-style illustration" if "다이어그램" in style else "photorealistic, natural light, documentary photo"
        return (f"장면: {text.strip()[:220]}\n"
                f"맥락: {kit.visual_context or kit.brand_name}. {extra}\n"
                f"스타일: {style_hint}, Korean people, warm and trustworthy mood.\n"
                "제약: 이미지 안에 글자·숫자·로고·워터마크를 넣지 말 것.")

    @staticmethod
    def _tone_intro(tone: str) -> str:
        if "경고" in tone or "경각심" in tone:
            return "알고 계셨습니까?"
        if "이익" in tone or "효율" in tone or "비즈니스" in tone:
            return "숫자로 확인하세요."
        if "유머" in tone or "공감" in tone or "트렌디" in tone:
            return "혹시 저만 이런가요?"
        if "감동" in tone or "가치" in tone or "역경" in tone:
            return "이 이야기를 들어보세요."
        return ""

    # ------------------------------------------------------------------
    # 가드레일: 실명 마스킹 + 컴플라이언스 + 팩트 락
    # ------------------------------------------------------------------
    def _mask_text(self, text: str, mask_map: Dict[str, str]) -> str:
        if not text:
            return text
        res = text
        for real, masked in sorted(mask_map.items(), key=lambda kv: -len(kv[0])):
            if not real:
                continue
            # "통신 대기업 K사(KT)"처럼 익명명과 실명이 병기된 경우 괄호 속 실명만 제거
            res = re.sub(r"\s*\(\s*" + re.escape(real) + r"\s*\)", "", res)
            # 영문 약어는 단어 경계에서만 치환 (예: KTX의 KT는 제외) + 뒤따르는 조사 보정
            boundary = r"(?<![A-Za-z])" if re.fullmatch(r"[A-Za-z]+", real) else ""
            tail = r"(?![A-Za-z])" if boundary else ""
            pattern = boundary + re.escape(real) + tail + r"(은|는|이|가|을|를|과|와|으로|로)?(?![가-힣])"
            res = re.sub(pattern, lambda m, mk=masked: mk + self._fix_particle(mk, m.group(1)), res)
            res = re.sub(boundary + re.escape(real) + tail, masked, res)
        return res

    @staticmethod
    def _fix_particle(word: str, particle: Optional[str]) -> str:
        if not particle:
            return ""
        last = word[-1]
        has_batchim = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
        pairs = {"은": ("은", "는"), "는": ("은", "는"), "이": ("이", "가"), "가": ("이", "가"),
                 "을": ("을", "를"), "를": ("을", "를"), "과": ("과", "와"), "와": ("과", "와"),
                 "으로": ("으로", "로"), "로": ("으로", "로")}
        with_b, without_b = pairs[particle]
        if particle in ("으로", "로") and (ord(last) - 0xAC00) % 28 == 8:  # ㄹ 받침은 '로'
            return "로"
        return with_b if has_batchim else without_b

    def _apply_guardrails(self, storyboard: EditableStoryboard, payload: UserRequestPayload, source: Dict[str, Any],
                          notes: List[str], kit: bk.BrandKit, pack) -> EditableStoryboard:
        logs: List[str] = list(notes)
        logs.append(f"🤖 생성 엔진: {storyboard.engine or 'unknown'} · 브랜드: {kit.brand_name}")
        if payload.card_count > 1:
            tip = fit_warning(storyboard.card_format or payload.card_format, source["facts"], payload.raw_text)
            if tip:
                logs.append(f"⚠️ 형식 안내: {tip}")

        if payload.anonymize and kit.mask_map:
            m = lambda t: self._mask_text(t, kit.mask_map)
            storyboard.title = m(storyboard.title)
            storyboard.sns_caption = m(storyboard.sns_caption)
            storyboard.hashtags = [m(h) for h in storyboard.hashtags]
            for slide in storyboard.card_slides:
                for field in ("lead_headline", "body_content", "takeaway", "stat_label"):
                    setattr(slide, field, m(getattr(slide, field)))
                if slide.comparison_table:
                    slide.comparison_table = {m(k): [m(str(v)) for v in (vals if isinstance(vals, list) else [vals])]
                                              for k, vals in slide.comparison_table.items()}
            for scene in storyboard.shorts_scenes:
                scene.narration_script = m(scene.narration_script)
                scene.display_caption = m(scene.display_caption)
            storyboard.is_anonymized = True
            logs.append("🔒 실명 마스킹 완료 (브랜드 킷의 마스킹 사전 기준)")

        logs += compliance.apply(storyboard, pack, kit)

        if storyboard.engine and "rule" not in storyboard.engine:   # AI가 쓴 문장만 원문과 대조 (무료)
            from services import source_check
            kit_text = [kit.brand_name, kit.tagline, kit.persona, kit.cta_headline, kit.value_headline, *kit.value_points]
            flagged = source_check.unsupported_lines(storyboard.card_slides, [payload.raw_text, *kit_text])
            for no, line in flagged[:8]:
                logs.append(f"⚠️ 원문 확인 필요 (카드 {no}): '{line[:40]}' — 원문에 없는 내용이 보태졌을 수 있습니다.")
            if not flagged:
                logs.append("✅ 원문 대조: 원문에 없는 내용으로 보이는 문장 없음")

        if payload.fact_lock:
            output_text = re.sub(r"\s+", "", self._storyboard_text(storyboard))
            facts = source["facts"]
            if not facts:
                logs.append("ℹ️ 팩트 락: 원문에서 검증할 수치형 팩트가 발견되지 않았습니다.")
            for fact in facts:
                if re.sub(r"\s+", "", fact) in output_text:
                    logs.append(f"✅ 팩트 보존 확인: {fact}")
                else:
                    logs.append(f"⚠️ 팩트 누락/변형 의심: '{fact}' 가 결과물에 없습니다. 검수가 필요합니다.")
        else:
            logs.append("ℹ️ 팩트 락 비활성화: 수치 보존 검증을 건너뛰었습니다.")
        storyboard.fact_check_log = logs
        return storyboard

    @staticmethod
    def _storyboard_text(sb: EditableStoryboard) -> str:
        parts = [sb.title, sb.sns_caption]
        for s in sb.card_slides:
            parts += [s.lead_headline, s.body_content, s.takeaway, s.stat_value, s.stat_label]
            if s.comparison_table:
                parts += [json.dumps(s.comparison_table, ensure_ascii=False)]
        for sc in sb.shorts_scenes:
            parts += [sc.narration_script, sc.display_caption]
        return " ".join(parts)

    @staticmethod
    def fact_status(storyboard: EditableStoryboard) -> Tuple[int, int]:
        """(보존된 팩트 수, 누락 의심 팩트 수)"""
        ok = sum(1 for l in storyboard.fact_check_log if l.startswith("✅ 팩트"))
        warn = sum(1 for l in storyboard.fact_check_log if l.startswith("⚠️ 팩트"))
        return ok, warn
