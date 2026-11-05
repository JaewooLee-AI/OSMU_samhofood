"""첨부 에셋이 없는 슬라이드/씬에 Gemini 이미지 모델로 AI 이미지를 생성해 채우는 서비스.

Claude API는 이미지 생성을 지원하지 않으므로 이미지 생성은 Gemini 키가 있을 때만 동작한다.
"""
import os
import logging
import asyncio
import tempfile
from typing import Awaitable, Callable, List, Optional, Tuple
from models.schema import EditableStoryboard, MediaAsset
from services.media_assets import MediaLibrary
from services import usage_log
from services import design_templates

# 앞에서부터 시도하고, 모델을 찾을 수 없으면 다음 모델로 대체
GEMINI_IMAGE_MODELS = [m for m in [os.environ.get("GEMINI_IMAGE_MODEL"), "gemini-3.1-flash-image", "gemini-2.5-flash-image"] if m]
MAX_CONCURRENCY = 3
NO_PHOTO_TYPES = ("comparison_table", "big_number", "event_info")  # 사진 없이 표·숫자로 그리는 카드

ProgressFn = Optional[Callable[[str], Awaitable[None]]]


class AIImageGenerator:
    def __init__(self, library: MediaLibrary):
        self.library = library
        self._working_model: Optional[str] = None
        self.model_override: str = "auto"  # 설정에서 고른 이미지 모델 (auto면 기본 순서로 시도)

    async def fill_missing(self, sb: EditableStoryboard, api_key: Optional[str], enabled: bool = True,
                           progress: ProgressFn = None, cards: bool = True, shorts: bool = True) -> List[str]:
        """비어 있는 슬라이드·씬에 이미지를 생성해 배치하고, 결과 로그를 반환.

        카드 만들기 단계에서는 cards만, 쇼츠 만들기 단계에서는 shorts만 채운다 (시나리오 단계는 텍스트만).
        """
        if not enabled:
            return ["ℹ️ AI 이미지 생성 꺼짐: 에셋이 없는 장면은 브랜드 디자인 배경으로 렌더링합니다."]

        targets: List[Tuple[object, str, str]] = []  # (슬라이드 또는 씬, 프롬프트, 화면비)
        if cards:
            for slide in sb.card_slides:
                if not slide.asset_id and slide.slide_type not in NO_PHOTO_TYPES and design_templates.get(sb.design_id).uses_photo(slide.slide_type):
                    # 카드 디자인 템플릿의 그림 스타일(글자·로고 없는 일러스트 등)을 프롬프트에 덧붙임
                    suffix = design_templates.get(sb.design_id).image_suffix()
                    prompt = slide.image_prompt or slide.lead_headline
                    if suffix:  # 다른 스타일 지시(예: 실사풍)와 충돌하지 않도록 템플릿 스타일을 우선
                        prompt = "\n".join(l for l in prompt.splitlines() if not l.startswith("스타일:"))
                        prompt = f"그림 스타일(반드시 따를 것, 다른 스타일 지시보다 우선): {suffix}\n{prompt}"
                    targets.append((slide, prompt, design_templates.get(sb.design_id).image_aspect))
        if shorts:
            for scene in sb.shorts_scenes:
                if not scene.asset_id:
                    targets.append((scene, scene.visual_motion_prompt or scene.display_caption, "9:16"))
        if not targets:
            return []

        if not api_key:
            return [f"ℹ️ Gemini API 키가 없어 AI 이미지 {len(targets)}장을 생성하지 않았습니다 (Claude API는 이미지 생성 미지원). 해당 장면은 브랜드 디자인 배경으로 렌더링합니다."]

        from google import genai
        client = genai.Client(api_key=api_key)
        first = self._next_g(sb)
        ids = [f"G{first + i}" for i in range(len(targets))]

        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        no_text = bool(design_templates.get(sb.design_id).style_prompt)
        text_leaks: List[str] = []
        done = 0
        errors: List[str] = []
        aborted = False

        async def run(target, prompt: str, aspect: str, asset_id: str):
            nonlocal done, aborted
            async with sem:
                if aborted:
                    errors.append("이전 인증 오류로 건너뜀")
                    return
                try:
                    data, ext = await self._generate(client, prompt, aspect)
                    if no_text:  # 글자 없는 그림이어야 하는 디자인: 글자가 섞였으면 한 번 다시 생성
                        if await self.has_text(client, data, ext):
                            data2, ext2 = await self._generate(client, prompt, aspect)
                            if await self.has_text(client, data2, ext2):
                                text_leaks.append(asset_id)
                            data, ext = data2, ext2
                    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                        tmp.write(data)
                    try:
                        asset = await self.library.ingest(tmp.name, asset_id, original_name=f"AI_{asset_id}{ext}", source="ai")
                    finally:
                        os.remove(tmp.name)
                    asset.description = prompt.splitlines()[0][:120]
                    sb.assets.append(asset)
                    target.asset_id = asset.id
                except Exception as e:
                    logging.getLogger(__name__).warning(f"AI 이미지 생성 실패 ({asset_id}): {e}")
                    errors.append(f"{type(e).__name__}: {str(e)[:100]}")
                    # 키 오류·권한 오류는 나머지 요청도 실패하므로 즉시 중단 (불필요한 과금/대기 방지)
                    if getattr(e, "code", None) in (400, 401, 403) and "key" in str(e).lower():
                        aborted = True
                done += 1
                if progress:
                    await progress(f"AI 이미지 생성 중... ({done}/{len(targets)})")

        await asyncio.gather(*(run(t, p, a, i) for (t, p, a), i in zip(targets, ids)))
        sb.assets.sort(key=lambda a: (a.source != "user", len(a.id), a.id))

        made = len(targets) - len(errors)
        logging.getLogger(__name__).info(f"AI 이미지 생성 결과: 성공 {made} / 실패 {len(errors)}")
        logs = [f"🎨 AI 이미지 {made}장 생성 완료 ({self._working_model or GEMINI_IMAGE_MODELS[0]})"] if made else []
        if text_leaks:
            logs.append(f"⚠️ AI 그림 {len(text_leaks)}장에 글자가 섞였을 수 있습니다 ({', '.join(text_leaks)}) — 카드에서 [이미지 다시 만들기]로 바꿔 주세요.")
        if errors:
            logs.append(f"⚠️ AI 이미지 {len(errors)}장 생성 실패 → 디자인 배경 사용 ({errors[0]})")
        return logs

    async def has_text(self, client, data: bytes, ext: str) -> bool:
        """그림 안에 읽을 수 있는 글자(영문·한글·숫자)가 보이는지 저렴한 모델로 확인 (이미지 1장당 아주 소액)"""
        from google.genai import types
        from services.ai_director import planning_model
        model = "gemini-3.1-flash-lite"
        try:
            resp = await asyncio.to_thread(
                client.models.generate_content, model=model,
                contents=[types.Part.from_bytes(data=data, mime_type="image/png" if ext == ".png" else "image/jpeg"),
                          "Does this image contain any readable text, letters, words or numbers (including on signs, labels, captions, documents)? "
                          "Answer with exactly one word: YES or NO."],
                config=types.GenerateContentConfig(temperature=0))
            usage_log.record_gemini("그림 글자 검사", model, resp)
            return (resp.text or "").strip().upper().startswith("YES")
        except Exception as e:
            logging.getLogger(__name__).warning(f"글자 검사 실패(무시): {e}")
            return False

    def _next_g(self, sb: EditableStoryboard) -> int:
        """이전 생성분 파일을 덮어쓰지 않도록 폴더에 이미 있는 G번호 다음 번호"""
        existing = [f.split(".")[0] for f in os.listdir(self.library.assets_dir) if f.startswith("G")]
        used = [int(x[1:]) for x in existing if x[1:].isdigit()]
        return max(int(MediaLibrary.next_id(sb.assets, "G")[1:]), max(used, default=0) + 1)

    async def edit_image(self, sb: EditableStoryboard, api_key: Optional[str], instruction: str, aspect: str,
                         base: Optional[MediaAsset] = None, references: Optional[List[MediaAsset]] = None,
                         fallback_prompt: str = "") -> MediaAsset:
        """카드 한 장의 이미지를 수정 지시로 다시 만들기.

        base(현재 이미지)가 있으면 그 이미지를 고치고, references(참조 이미지)는 스타일·구도·인물 참고용으로 함께 보낸다.
        base가 없으면 지시 + 장면 묘사(fallback_prompt)로 새로 만든다. 새 에셋을 sb.assets에 추가해 반환.
        """
        if not api_key:
            raise RuntimeError("Gemini API 키가 필요합니다 (설정 화면에서 입력).")
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)

        def part(asset: MediaAsset):
            path = asset.keyframes[len(asset.keyframes) // 2] if asset.kind == "video" and asset.keyframes else asset.path
            with open(path, "rb") as f:
                data = f.read()
            return types.Part.from_bytes(data=data, mime_type="image/png" if path.lower().endswith(".png") else "image/jpeg")

        contents: list = []
        if base is not None:
            contents.append(part(base))
            text = f"첫 번째 이미지를 다음 지시대로 수정하세요. 지시: {instruction.strip()}"
        else:
            suffix = design_templates.get(sb.design_id).image_suffix()
            text = f"다음 장면을 그리세요: {fallback_prompt.strip()}\n추가 지시: {instruction.strip()}" + (f"\n그림 스타일: {suffix}" if suffix else "")
        refs = [r for r in (references or []) if r is not None]
        for r in refs:
            contents.append(part(r))
        if refs:
            text += f"\n함께 보낸 참조 이미지 {len(refs)}장의 분위기·구도·대상을 참고하세요."
        text += "\n이미지 안에 글자·로고·워터마크를 넣지 마세요."
        contents.append(text)

        data, ext = await self._generate(client, contents, aspect, "이미지 수정")
        asset_id = f"G{self._next_g(sb)}"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(data)
        try:
            asset = await self.library.ingest(tmp.name, asset_id, original_name=f"AI_{asset_id}{ext}", source="ai")
        finally:
            os.remove(tmp.name)
        asset.description = ("수정: " + instruction.strip())[:120]
        sb.assets.append(asset)
        return asset

    async def _generate(self, client, prompt, aspect: str, feature: str = "카드 이미지") -> Tuple[bytes, str]:
        from google.genai import types, errors as genai_errors

        if self.model_override and self.model_override != "auto":
            models = [self.model_override]  # 직접 고른 모델은 대체 없이 그 모델만 (테스트 결과가 정직하도록)
        else:
            models = [self._working_model] if self._working_model else GEMINI_IMAGE_MODELS
        last_error: Optional[Exception] = None
        for model in models:
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                        image_config=types.ImageConfig(aspect_ratio=aspect),
                    ),
                )
            except genai_errors.ClientError as e:
                # 모델 미존재(404)면 다음 후보 모델로, 그 외 오류는 그대로 전달
                if getattr(e, "code", None) == 404 and not self._working_model:
                    last_error = e
                    continue
                raise
            for part in response.parts or []:
                if part.inline_data and part.inline_data.data:
                    self._working_model = model
                    usage_log.record_gemini(feature, model, response, images=1)
                    mime = part.inline_data.mime_type or "image/png"
                    return part.inline_data.data, ".jpg" if "jpeg" in mime else ".png"
            raise RuntimeError("응답에 이미지가 없습니다 (안전 필터 또는 프롬프트 문제)")
        raise last_error or RuntimeError("사용 가능한 Gemini 이미지 모델이 없습니다")
