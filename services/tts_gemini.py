"""Gemini TTS로 씬별 한국어 내레이션을 합성 (톤앤매너에 맞춘 말투 지시 + 목소리 선택 + 결과 캐시).

Claude API는 음성 생성을 지원하지 않으므로 내레이션은 Gemini 키가 있을 때만 이 경로를 사용하고,
없거나 실패하면 쇼츠 렌더러가 macOS Yuna → gTTS 순으로 대체한다.
"""
import os
import asyncio
import hashlib
import logging
import struct
from typing import List, Optional

logger = logging.getLogger(__name__)

GEMINI_TTS_MODELS = [m for m in [os.environ.get("GEMINI_TTS_MODEL"), "gemini-3.8-flash-tts", "gemini-2.5-flash-preview-tts"] if m]

# 화면 표시용 라벨 → Gemini 기본 제공 목소리 이름 (Google 문서의 목소리 특징 기준)
VOICE_OPTIONS = {
    "자동 (톤앤매너에 맞춤)": "auto",
    "Kore · 단단하고 신뢰감 있는": "Kore",
    "Charon · 차분한 정보 전달형": "Charon",
    "Puck · 경쾌하고 밝은": "Puck",
    "Zephyr · 밝고 산뜻한": "Zephyr",
    "Aoede · 부드럽고 편안한": "Aoede",
    "Fenrir · 활기찬": "Fenrir",
    "macOS Yuna (무료·오프라인)": "system",
}


def pick_voice(choice: str, tone: str) -> str:
    if choice and choice not in ("auto", "system"):
        return choice
    if "경고" in tone or "경각심" in tone:
        return "Kore"
    if "이익" in tone or "효율" in tone or "비즈니스" in tone:
        return "Charon"
    if "유머" in tone or "공감" in tone or "트렌디" in tone:
        return "Puck"
    if "감동" in tone or "가치" in tone or "역경" in tone:
        return "Aoede"
    return "Kore"


def style_instruction(tone: str) -> str:
    """말투 지시문 (영어 지시는 읽히지 않고 한국어 본문만 낭독됨을 실측으로 확인)"""
    if "경고" in tone or "경각심" in tone:
        style = "a serious, firm and trustworthy news-anchor tone"
    elif "이익" in tone or "효율" in tone or "비즈니스" in tone:
        style = "a clear, confident and informative business-briefing tone"
    elif "유머" in tone or "공감" in tone or "트렌디" in tone:
        style = "a bright, friendly and upbeat vlog tone"
    elif "감동" in tone or "가치" in tone or "역경" in tone:
        style = "a warm, sincere and moving documentary tone"
    else:
        style = "a natural, friendly and professional promotional tone"
    return f"Read the following Korean narration in {style}, at a lively pace suitable for a short-form video:"


def _pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm


class GeminiTTS:
    def __init__(self, api_key: str, voice: str, tone: str, cache_dir: str):
        self.api_key = api_key
        self.voice = voice
        self.instruction = style_instruction(tone)
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.model_used: Optional[str] = None

    def _cache_path(self, model: str, text: str) -> str:
        key = hashlib.sha256(f"{model}|{self.voice}|{self.instruction}|{text}".encode()).hexdigest()[:24]
        return os.path.join(self.cache_dir, f"{key}.wav")

    async def synthesize_all(self, texts: List[str], out_paths: List[str]) -> int:
        """모든 씬을 합성. 하나라도 실패하면 예외 (목소리가 섞이지 않도록 호출 측이 전체 대체). 반환: 캐시 재사용 수"""
        from google import genai
        client = genai.Client(api_key=self.api_key)
        sem = asyncio.Semaphore(3)
        reused = 0

        async def one(text: str, out: str):
            nonlocal reused
            async with sem:
                for model in ([self.model_used] if self.model_used else GEMINI_TTS_MODELS):
                    cached = self._cache_path(model, text)
                    if os.path.exists(cached):
                        with open(cached, "rb") as src, open(out, "wb") as dst:
                            dst.write(src.read())
                        self.model_used = self.model_used or model
                        reused += 1
                        return
                    for attempt in range(2):
                        try:
                            data = await self._call(client, model, text)
                            break
                        except Exception as e:
                            if getattr(e, "code", None) == 404:
                                data = None
                                break  # 모델 없음 → 다음 후보 모델
                            if attempt == 1:
                                raise
                            await asyncio.sleep(1.5)
                    if data is None:
                        continue
                    with open(cached, "wb") as f:
                        f.write(data)
                    with open(out, "wb") as f:
                        f.write(data)
                    self.model_used = self.model_used or model
                    return
                raise RuntimeError("사용 가능한 Gemini TTS 모델이 없습니다")

        await asyncio.gather(*(one(t, o) for t, o in zip(texts, out_paths)))
        return reused

    async def _call(self, client, model: str, text: str) -> bytes:
        from google.genai import types
        response = await asyncio.wait_for(client.aio.models.generate_content(
            model=model,
            contents=f"{self.instruction}\n{text}",
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice))),
            ),
        ), timeout=60)
        from services import usage_log
        usage_log.record_gemini("음성 합성", model, response)
        part = response.candidates[0].content.parts[0]
        data, mime = part.inline_data.data, (part.inline_data.mime_type or "")
        if not data:
            raise RuntimeError("Gemini TTS 응답에 오디오가 없습니다")
        # 응답이 WAV(헤더 포함)이면 그대로, 원시 PCM(audio/L16)이면 WAV로 감싸기
        if data[:4] == b"RIFF":
            return data
        rate = 24000
        if "rate=" in mime:
            try:
                rate = int(mime.split("rate=")[1].split(";")[0])
            except ValueError:
                pass
        return _pcm_to_wav(data, rate)
