"""Gemini Omni 1.1 Flash 로 쇼츠 일부 장면을 실제로 움직이는 AI 영상으로 만드는 선택 기능.

(예전 Veo 3.1 Fast 는 '더 이상 제공되지 않는 모델'로 거절되어 Omni 로 교체했다. 2026-10-30 확인)

방식 (설정에서 선택)
- image : 장면에 배치된 사진 1장을 자연스럽게 움직이는 영상으로
- link  : 이 장면의 사진 → 다음 장면의 사진으로 이어지는 영상 (장면 전환까지 AI가 움직임으로 연결).
          다음 장면에 사진이 없거나 마지막 장면이면 image 방식
- 이미 첨부 동영상이 배치된 장면은 건너뜀 (원래 움직이는 영상이므로 비용을 쓰지 않음)
- 결과는 캐시: 같은 사진·프롬프트·모델·방식이면 다시 생성(과금)하지 않음
- 실패한 장면은 기존 카메라 무브(줌·팬)로 그대로 렌더링
- 영상에는 소리가 함께 만들어지지만 쓰지 않는다(쇼츠는 TTS 내레이션으로 대체)
요금: 출력 영상 $17.50/100만 토큰 ≈ 초당 약 $0.10 (AI Studio 모델 화면 기준, 생성 길이 3~10초)
"""
import os
import base64
import asyncio
import hashlib
import logging
import subprocess
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from models.schema import EditableStoryboard, MediaAsset
from services.media_assets import MediaLibrary
from services import usage_log

logger = logging.getLogger(__name__)

OMNI_MODEL = "gemini-omni-1.1-flash"
PRICE_PER_SEC = 0.10
CLIP_MIN, CLIP_MAX = 3, 10  # Omni 가 만드는 영상 길이(초)
MODES = {
    "image": "사진 1장을 움직이게 (장면마다 영상 1개)",
    "link": "다음 장면 사진까지 이어서 움직이게 (장면 전환 포함)",
}
SCOPES = {
    "none": "사용 안 함 (현재 방식: 이미지 줌·팬)",
    "opening": "오프닝 1장면",
    "key2": "핵심 2장면 (오프닝 + 핵심 장면)",
    "all": "전체 장면",
}
MAX_PARALLEL = 2

ProgressFn = Optional[Callable[[str], Awaitable[None]]]


def clip_seconds(scene_seconds: float) -> int:
    """장면 길이에 맞는 클립 길이 (3~10초, 장면이 더 길면 10초 영상을 반복 재생)"""
    return int(max(CLIP_MIN, min(CLIP_MAX, round(scene_seconds + 0.49))))


def pick_scenes(sb: EditableStoryboard, scope: str) -> List[int]:
    """옵션에 따른 대상 장면 인덱스 (첨부 동영상이 이미 있는 장면은 제외)"""
    n = len(sb.shorts_scenes)
    if scope == "none" or n == 0:
        return []
    if scope == "opening":
        idx = [0]
    elif scope == "key2":
        key = next((i for i, sc in enumerate(sb.shorts_scenes[1:], 1)
                    if any(ch.isdigit() for w in sc.emphasis_words for ch in w) and sc.section_type != "conclusion"), 1 if n > 1 else None)
        idx = [0] + ([key] if key is not None else [])
    else:
        idx = list(range(n))
    return [i for i in idx if not ((a := sb.asset(sb.shorts_scenes[i].asset_id)) and a.kind == "video" and a.source == "user")]


def estimate(sb: Optional[EditableStoryboard], scope: str, mode: str = "image", shorts_seconds: int = 45) -> Tuple[int, float]:
    """(생성 초 수, 예상 비용 USD). 스토리보드가 있으면 장면별 예상 길이로, 없으면 쇼츠 길이로 추정"""
    if scope == "none":
        return 0, 0.0
    if sb and sb.shorts_scenes:
        secs = sum(clip_seconds(sb.shorts_scenes[i].duration_seconds) for i in pick_scenes(sb, scope))
    else:
        per = CLIP_MAX
        secs = {"opening": per, "key2": per * 2}.get(scope, max(per, int(round(shorts_seconds / 5.0) * per)))
    return secs, round(secs * PRICE_PER_SEC, 2)


def _duration(path: str) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return float(out)
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0.0


def _image_bytes(asset: Optional[MediaAsset]) -> Optional[Tuple[bytes, str]]:
    path = asset.preview_path if asset else ""
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read(), "image/png" if path.lower().endswith(".png") else "image/jpeg"


class OmniGenerator:
    def __init__(self, library: MediaLibrary, cache_dir: str):
        self.library = library
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    async def generate(self, sb: EditableStoryboard, scope: str, mode: str, api_key: str,
                       scene_seconds: Dict[int, float], progress: ProgressFn = None) -> Tuple[Dict[int, MediaAsset], List[str]]:
        """대상 장면별 Omni 클립 생성 → {장면 인덱스: 동영상 에셋}, 로그"""
        targets = pick_scenes(sb, scope)
        if not targets:
            return {}, []
        if not api_key:
            return {}, ["ℹ️ Gemini 키가 없어 AI 영상화를 건너뛰었습니다."]
        from google import genai
        client = genai.Client(api_key=api_key)
        results: Dict[int, MediaAsset] = {}
        errors: List[str] = []
        billed = 0.0
        done = 0
        sem = asyncio.Semaphore(MAX_PARALLEL)

        async def one(i: int):
            nonlocal billed, done
            scene = sb.shorts_scenes[i]
            seconds = clip_seconds(scene_seconds.get(i, scene.duration_seconds))
            first = _image_bytes(sb.asset(scene.asset_id))
            nxt = _image_bytes(sb.asset(sb.shorts_scenes[i + 1].asset_id)) if (mode == "link" and i + 1 < len(sb.shorts_scenes)) else None
            link = bool(first and nxt)
            scene_desc = scene.visual_motion_prompt or scene.display_caption
            if link:
                prompt = (f"A smooth, natural cinematic transition from the first image to the second image. {scene_desc}\n"
                          f"About {seconds} seconds. Vertical 9:16. No text, no captions, no logos.")
            else:
                prompt = (f"{scene_desc}\nGentle, natural cinematic motion. About {seconds} seconds. "
                          "Vertical 9:16 short-form video. No text, no captions, no logos.")
            key_src = (first[0] if first else b"") + (nxt[0] if link else b"") + f"|{prompt}|{OMNI_MODEL}|{link}".encode()
            cached = os.path.join(self.cache_dir, hashlib.sha256(key_src).hexdigest()[:24] + ".mp4")
            async with sem:
                try:
                    if not os.path.exists(cached):
                        await asyncio.to_thread(self._create, client, prompt, first, nxt if link else None, cached)
                        got = _duration(cached)
                        billed += got
                        usage_log.record("AI 영상(Omni)", "gemini", OMNI_MODEL, seconds=got, note="link" if link else "image")
                    vid_id = f"V{i + 1}"
                    results[i] = await self.library.ingest(cached, vid_id, original_name=f"Omni_{vid_id}.mp4", source="ai")
                except Exception as e:
                    logger.warning(f"Omni 생성 실패 (장면 {i + 1}): {e}")
                    errors.append(f"장면 {i + 1}: {type(e).__name__}: {str(e)[:90]}")
            done += 1
            if progress:
                await progress(f"AI 영상 생성 중... ({done}/{len(targets)}) — 클립당 1분 안팎 소요")

        if progress:
            await progress(f"AI 영상 생성 시작 ({len(targets)}개 장면, {'이어 주기' if mode == 'link' else '사진 움직이기'})")
        await asyncio.gather(*(one(i) for i in targets))

        logs = []
        if results:
            cost = f" · 이번 과금 약 ${billed * PRICE_PER_SEC:.2f} ({billed:.0f}초)" if billed else " · 캐시 재사용(추가 과금 없음)"
            logs.append(f"🎞️ AI 영상 {len(results)}개 적용 (Omni){cost}")
        if errors:
            logs.append(f"⚠️ AI 영상 {len(errors)}개 생성 실패 → 해당 장면은 기존 줌·팬 효과로 렌더링 ({errors[0]})")
        return results, logs

    @staticmethod
    def _create(client, prompt: str, first, second, out_path: str):
        content = []
        for img in (first, second):
            if img:
                content.append({"type": "image", "data": base64.b64encode(img[0]).decode(), "mime_type": img[1]})
        content.append({"type": "text", "text": prompt})
        it = client.interactions.create(model=OMNI_MODEL, input=content,
                                        response_format={"type": "video", "aspect_ratio": "9:16"})
        tmp = out_path + ".part"
        with open(tmp, "wb") as f:
            f.write(base64.b64decode(it.output_video.data))
        os.replace(tmp, out_path)
