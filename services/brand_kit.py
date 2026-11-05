"""브랜드 킷 · 앱 설정 저장소.

공통 엔진(코드)은 회사와 무관하고, 회사별 차이는 브랜드 킷(데이터)과 업종 컴플라이언스 팩으로만 표현한다.
- 브랜드 킷: DATA_DIR/brand_kits/<id>.json (+ 로고 파일 <id>_logo.png)  — 회사 데이터라 저장소에 올리지 않음
- 샘플 브랜드 킷: presets/brand_kits/sample.json (저장소 포함, 처음 실행 시 복사)
- 앱 설정: DATA_DIR/settings.json (사용 중인 브랜드, 기본 카드 형식·장수)
"""
import os
import re
import json
import base64
import shutil
from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from services.paths import BASE_DIR, DATA_DIR

KITS_DIR = os.path.join(DATA_DIR, "brand_kits")
PRESETS_DIR = os.path.join(BASE_DIR, "presets", "brand_kits")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
os.makedirs(KITS_DIR, exist_ok=True)


class BrandColors(BaseModel):
    primary: str = "#1E3A8A"      # 헤더·헤드라인·요약 바
    secondary: str = "#2563EB"    # 강조 정보(연락처·수치)
    accent: str = "#FFB800"       # 라벨·형광펜
    background: str = "#F8FAFC"   # 카드 바탕


class BrandKit(BaseModel):
    id: str
    brand_name: str
    tagline: str = Field(default="", description="로고 옆 한 줄 소개")
    industry_pack: str = Field(default="general", description="업종 컴플라이언스 팩 ID")
    phone: str = ""
    homepage: str = ""
    sns_handle: str = ""
    footer_text: str = Field(default="", description="카드 하단 왼쪽 문구 (비우면 홈페이지 주소)")
    colors: BrandColors = Field(default_factory=BrandColors)
    logo_file: str = Field(default="", description="KITS_DIR 안의 로고 파일명 (투명 PNG 권장)")
    persona: str = Field(default="", description="화자 (예: 기업 복지 상담 매니저)")
    tone: str = Field(default="", description="기본 톤앤매너")
    target_audiences: List[str] = Field(default_factory=list)
    core_facts: List[str] = Field(default_factory=list, description="AI가 근거로 쓸 검증된 사실")
    value_headline: str = ""
    value_points: List[str] = Field(default_factory=list, description="'왜 우리인가' 핵심 가치 3개 내외")
    cta_headline: str = ""
    cta_body: List[str] = Field(default_factory=list)
    cta_takeaway: str = ""
    cta_narration: str = ""
    default_hashtags: List[str] = Field(default_factory=list)
    visual_context: str = Field(default="", description="AI 이미지 생성 시 업종·장면 맥락 설명")
    emphasis_keywords: List[str] = Field(default_factory=list, description="키네틱 타이포에서 크게 보일 핵심 단어 후보")
    mask_map: Dict[str, str] = Field(default_factory=dict, description="실명 → 익명 표기")
    extra_replacements: Dict[str, str] = Field(default_factory=dict, description="회사 전용 금지 표현 → 대체 표현")
    extra_warn_terms: List[str] = Field(default_factory=list, description="회사 전용 주의 단어 (경고만)")
    examples: List[Dict[str, str]] = Field(default_factory=list,
                                           description="예시 원고 [{format: 형식 ID, title: 제목, text: 원고}] — 스튜디오 '예시 불러오기'에 나옴")

    @property
    def logo_path(self) -> Optional[str]:
        if not self.logo_file:
            return None
        p = os.path.join(KITS_DIR, self.logo_file)
        return p if os.path.exists(p) else None

    @property
    def contact_line(self) -> str:
        parts = [f"상담 {self.phone}" if self.phone else "", self.homepage_short]
        return "  |  ".join(p for p in parts if p)

    @property
    def homepage_short(self) -> str:
        return re.sub(r"^https?://(www\.)?", "", self.homepage or "").rstrip("/")


class AppSettings(BaseModel):
    active_brand_id: str = "samhofood"
    default_card_format: str = "info_summary"
    default_card_count: int = 5
    ai_provider: str = "gemini"            # 기획 AI: gemini | claude
    generate_ai_images: bool = True        # 사진이 없는 카드·장면에 AI 이미지 채우기
    planning_model: str = "auto"           # 원고·시나리오·쇼츠 대본 Gemini 모델 (auto = GEMINI_MODEL 기본값)
    image_model: str = "auto"              # AI 이미지 모델 (auto = 기본 순서로 시도)
    reviewer_name: str = ""                # 카드 확정 시 마지막으로 입력한 담당자 이름
    naver_blog_id: str = ""                # 네이버 게시용 블로그 아이디 (blog.naver.com/<아이디>)


# ----------------------------------------------------------------------
# 저장 / 불러오기
# ----------------------------------------------------------------------
def _kit_path(kit_id: str) -> str:
    return os.path.join(KITS_DIR, f"{kit_id}.json")


def _ensure_sample():
    """처음 실행 시 기본 브랜드 킷(삼호푸드·삼삼이고등어)을 데이터 폴더로 복사 (이미 있으면 건드리지 않음)"""
    if not os.path.isdir(PRESETS_DIR):
        return
    for name in sorted(os.listdir(PRESETS_DIR)):
        if name.endswith(".json") and not os.path.exists(os.path.join(KITS_DIR, name)):
            shutil.copy(os.path.join(PRESETS_DIR, name), os.path.join(KITS_DIR, name))


def list_kits() -> List[BrandKit]:
    _ensure_sample()
    kits = []
    for name in sorted(os.listdir(KITS_DIR)):
        if name.endswith(".json"):
            try:
                kits.append(load_kit(name[:-5]))
            except Exception:
                continue
    return kits


def load_kit(kit_id: str) -> BrandKit:
    _ensure_sample()
    with open(_kit_path(kit_id), "r", encoding="utf-8") as f:
        return BrandKit.model_validate(json.load(f))


def save_kit(kit: BrandKit) -> None:
    if not re.fullmatch(r"[a-z0-9_\-]{2,40}", kit.id):
        raise ValueError("브랜드 ID는 영문 소문자·숫자·-·_ 2~40자로 정해 주세요.")
    tmp = _kit_path(kit.id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kit.model_dump(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, _kit_path(kit.id))


def delete_kit(kit_id: str) -> None:
    kit = load_kit(kit_id)
    if kit.logo_path:
        os.remove(kit.logo_path)
    os.remove(_kit_path(kit_id))


def save_logo(kit: BrandKit, data: bytes) -> BrandKit:
    """업로드한 로고를 PNG로 정규화해 저장 (투명 배경 유지)"""
    from io import BytesIO
    from PIL import Image
    img = Image.open(BytesIO(data)).convert("RGBA")
    img.thumbnail((1200, 600))
    name = f"{kit.id}_logo.png"
    img.save(os.path.join(KITS_DIR, name))
    kit.logo_file = name
    return kit


def load_settings() -> AppSettings:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return AppSettings.model_validate(json.load(f))
    except (OSError, ValueError):
        return AppSettings()


def save_settings(settings: AppSettings) -> None:
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings.model_dump(), f, ensure_ascii=False, indent=2)


def active_kit() -> BrandKit:
    settings = load_settings()
    try:
        return load_kit(settings.active_brand_id)
    except (OSError, ValueError):
        return load_kit("samhofood")


# ----------------------------------------------------------------------
# 다른 PC·고객사로 옮기기 (로고 포함 단일 파일)
# ----------------------------------------------------------------------
def export_kit(kit_id: str) -> bytes:
    kit = load_kit(kit_id)
    data = {"format": "osmu-brandkit/1", "kit": kit.model_dump()}
    if kit.logo_path:
        with open(kit.logo_path, "rb") as f:
            data["logo_png_base64"] = base64.b64encode(f.read()).decode()
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def import_kit(raw: bytes) -> BrandKit:
    data = json.loads(raw.decode("utf-8"))
    if data.get("format") != "osmu-brandkit/1":
        raise ValueError("OSMU 브랜드 킷 파일이 아닙니다.")
    kit = BrandKit.model_validate(data["kit"])
    kit.logo_file = ""
    if data.get("logo_png_base64"):
        kit = save_logo(kit, base64.b64decode(data["logo_png_base64"]))
    save_kit(kit)
    return kit
