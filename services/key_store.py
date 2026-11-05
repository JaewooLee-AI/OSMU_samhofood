"""API 키 저장·불러오기·연결 테스트.

키는 프로젝트 폴더가 아닌 사용자 홈(~/.osmu_media_studio/keys.json)에 소유자 전용 권한(600)으로 저장한다.
웹 모드에서는 서버(이 PC)에 저장되므로 같은 서버에 접속하는 모든 브라우저가 같은 키를 사용한다.

키는 설정 화면에서 입력해 저장한 것 하나만 쓴다 (환경변수·.env 는 읽지 않는다). 키 값은 화면·로그에 전체를 출력하지 않는다.
"""
import os
import json
import asyncio
from typing import Dict, Tuple

KEY_DIR = os.path.join(os.path.expanduser("~"), ".osmu_media_studio")
KEY_FILE = os.path.join(KEY_DIR, "keys.json")
_OLD_KEY_FILE = os.path.join(os.path.expanduser("~"), ".anmasarang_studio", "keys.json")  # 이전 이름 폴더
KEY_NAMES = ("gemini", "anthropic")
SOURCE_LABELS = {"input": "입력한 키", "saved": "저장된 키"}


def load_keys() -> Dict[str, str]:
    try:
        with open(KEY_FILE if os.path.exists(KEY_FILE) or not os.path.exists(_OLD_KEY_FILE) else _OLD_KEY_FILE,
                  "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if k in KEY_NAMES and isinstance(v, str) and v}
    except (OSError, ValueError):
        return {}


def save_keys(keys: Dict[str, str]) -> None:
    os.makedirs(KEY_DIR, mode=0o700, exist_ok=True)
    data = {k: v.strip() for k, v in keys.items() if k in KEY_NAMES and v and v.strip()}
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(KEY_FILE, 0o600)


def delete_keys() -> None:
    for path in (KEY_FILE, _OLD_KEY_FILE):
        try:
            os.remove(path)
        except OSError:
            pass


def resolve(typed: Dict[str, str] = None) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(키, 출처) — 입력란에 방금 입력한 값이 있으면 그것, 없으면 저장된 키. 출처는 input | saved"""
    typed = typed or {}
    saved = load_keys()
    keys, sources = {}, {}
    for k in KEY_NAMES:
        for src, val in (("input", (typed.get(k) or "").strip()), ("saved", saved.get(k, ""))):
            if val:
                keys[k], sources[k] = val, src
                break
    return keys, sources


def mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if key and len(key) > 10 else "설정됨"


async def test_gemini(api_key: str) -> Tuple[bool, str]:
    """키 유효성 + 설정에서 고른 기획·이미지 모델 사용 가능 여부 확인 (토큰을 소모하지 않는 모델 조회만 사용)"""
    from google import genai
    from services.ai_director import planning_model
    from services.image_generator import GEMINI_IMAGE_MODELS
    from services import brand_kit as bk

    client = genai.Client(api_key=api_key)
    plan = planning_model()
    try:
        await client.aio.models.get(model=plan)
    except Exception as e:
        return False, f"Gemini 연결 실패 (기획 모델 {plan}): {_short_error(e)}"

    chosen = bk.load_settings().image_model
    candidates = [chosen] if chosen and chosen != "auto" else GEMINI_IMAGE_MODELS
    image_model = None
    for model in candidates:
        try:
            await client.aio.models.get(model=model)
            image_model = model
            break
        except Exception:
            continue
    if image_model:
        return True, f"Gemini 연결 성공 · 기획 {plan} · 이미지 {image_model}"
    return True, f"Gemini 연결 성공 · 기획 {plan} (⚠️ 이미지 모델 {', '.join(candidates)} 을(를) 찾지 못함)"


async def test_anthropic(api_key: str) -> Tuple[bool, str]:
    from anthropic import AsyncAnthropic
    from services.ai_director import CLAUDE_MODEL

    client = AsyncAnthropic(api_key=api_key)
    try:
        model = await asyncio.wait_for(client.models.retrieve(CLAUDE_MODEL), timeout=20)
        return True, f"Claude 연결 성공 · {model.id}"
    except Exception as e:
        return False, f"Claude 연결 실패: {_short_error(e)}"


def _short_error(e: Exception) -> str:
    text = str(e)
    for hint, msg in (("API key not valid", "API 키가 올바르지 않습니다"), ("invalid x-api-key", "API 키가 올바르지 않습니다"),
                      ("PERMISSION_DENIED", "권한이 없습니다"), ("not found", "모델을 찾을 수 없습니다")):
        if hint.lower() in text.lower():
            return msg
    return f"{type(e).__name__}: {text[:100]}"
