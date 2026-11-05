from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field

class WordTimestamp(BaseModel):
    """단어별 자막 싱크 타임스탬프 모델"""
    text: str
    start: float
    end: float

class MediaAsset(BaseModel):
    """사용자가 첨부했거나 AI가 생성한 이미지/동영상 에셋"""
    id: str = Field(description="에셋 ID (사용자 첨부: A1, A2... / AI 생성: G1, G2...)")
    kind: str = Field(description="image 또는 video")
    source: str = Field(default="user", description="user(사용자 첨부) 또는 ai(AI 생성)")
    path: str = Field(description="작업 폴더에 정규화되어 저장된 파일 경로")
    original_name: str = Field(default="", description="원본 파일명")
    width: int = 0
    height: int = 0
    duration: float = Field(default=0.0, description="동영상 길이(초), 이미지는 0")
    keyframes: List[str] = Field(default_factory=list, description="AI 분석·미리보기용 대표 프레임 JPG 경로 (이미지는 축소본 1장)")
    keyframe_times: List[float] = Field(default_factory=list, description="동영상 대표 프레임의 시각(초)")
    description: str = Field(default="", description="AI가 분석한 에셋 내용 요약")

    @property
    def preview_path(self) -> str:
        """카드뉴스 등 정지 이미지가 필요할 때 쓰는 대표 이미지 경로"""
        if self.kind == "image":
            return self.path
        if self.keyframes:
            return self.keyframes[len(self.keyframes) // 2]
        return ""

class BrandElements(BaseModel):
    """콘텐츠마다 고르는 브랜드 요소 (브랜드 킷 값을 어디에 보일지)"""
    logo: str = Field(default="all", description="all(모든 카드) | cover_cta(표지·마지막) | none")
    show_homepage: bool = True
    show_phone: bool = True
    show_sns: bool = False
    qr_url: str = Field(default="", description="QR로 만들 주소 (콘텐츠마다 입력)")
    qr_image: str = Field(default="", description="직접 올린 QR 이미지 경로 (qr_url보다 우선)")


class CardSlide(BaseModel):
    """카드뉴스 단일 슬라이드 스키마 (Smart Brevity 구조)"""
    slide_number: int = Field(description="슬라이드 순번 (1부터 시작)")
    slide_type: str = Field(default="body", description="cover | body | numbered | big_number | comparison_table | event_info | call_to_action | single")
    lead_headline: str = Field(description="Lead: 시선을 끄는 핵심 헤드라인 (1문장)")
    body_content: str = Field(description="Body: 핵심 팩트 및 상세 내용 (최대 3~4줄 또는 도표 데이터)")
    takeaway: str = Field(description="Takeaway: 하단 강조 요약 및 한 줄 핵심 (1문장)")
    image_prompt: str = Field(default="", description="배치할 에셋이 없을 때 AI 이미지 생성에 쓸 프롬프트")
    asset_id: Optional[str] = Field(default=None, description="이 슬라이드에 배치된 에셋 ID (없으면 텍스트 디자인)")
    comparison_table: Optional[Dict[str, Any]] = Field(default=None, description="비교표 데이터 (예: 직접고용 vs 연계고용, 인센티브 vs 기금)")
    badge: str = Field(default="", description="번호형 카드의 번호(예: 01) 등 짧은 표식")
    stat_value: str = Field(default="", description="빅넘버 카드의 큰 숫자 (원문 그대로, 예: 97.14점)")
    stat_label: str = Field(default="", description="빅넘버 숫자의 설명")
    text_position: str = Field(default="top", description="사진 배경 카드에서 글씨 위치: top(위) | bottom(아래)")

class ShortsScene(BaseModel):
    """쇼츠/릴스 단일 씬 스키마 (Audio-First 타임라인)"""
    scene_number: int = Field(description="씬 순번 (1부터 시작)")
    section_type: str = Field(default="body", description="구간 유형: hook(0~3초), body(3~45초), conclusion(45~60초)")
    narration_script: str = Field(description="TTS로 낭독할 실제 한국어 내레이션 대사")
    display_caption: str = Field(description="화면에 강조 표시될 짧은 번인 자막 문구")
    visual_motion_prompt: str = Field(default="", description="배치할 에셋이 없을 때 AI 이미지 생성에 쓸 장면 묘사")
    duration_seconds: float = Field(default=5.0, description="예상 구간 길이(초)")
    word_timestamps: List[WordTimestamp] = Field(default_factory=list, description="단어별 정밀 시작/종료 타임스탬프")
    asset_id: Optional[str] = Field(default=None, description="이 씬의 화면에 쓸 에셋 ID")
    clip_start: float = Field(default=0.0, description="동영상 에셋을 쓸 경우 사용 시작 시각(초)")
    emphasis_words: List[str] = Field(default_factory=list, description="키네틱 타이포에서 크게 강조할 display_caption 속 단어 (1~2개)")
    source_cards: List[int] = Field(default_factory=list, description="이 장면을 만든 확정 카드 번호 (1부터)")

class UserRequestPayload(BaseModel):
    """사용자 입력 요청 페이로드"""
    scenario_id: Optional[int] = Field(default=None, description="예시 프롬프트를 불러온 경우의 시나리오 ID")
    raw_text: str = Field(description="사용자 프롬프트 (만들고 싶은 콘텐츠 설명)")
    target_audience: str = Field(default="AI 자동 추천", description="타겟 오디언스")
    tone_and_manner: str = Field(default="AI 자동 추천", description="톤앤매너")
    visual_style: str = Field(default="실사풍 / 인포그래픽", description="비주얼 스타일")
    anonymize: bool = Field(default=True, description="기업/기관 실명 마스킹 여부 (IBM -> I사 등)")
    fact_lock: bool = Field(default=True, description="원문의 핵심 수치 왜곡 방지 팩트 락 활성화 여부")
    ai_provider: str = Field(default="gemini", description="기획 AI 공급자: 'gemini' 또는 'claude'")
    gemini_api_key: Optional[str] = Field(default=None, description="Gemini API 키 (기획 + AI 이미지 생성)")
    anthropic_api_key: Optional[str] = Field(default=None, description="Anthropic API 키 (기획)")
    card_count: int = Field(default=7, description="카드뉴스 장수")
    shorts_seconds: int = Field(default=45, description="쇼츠 목표 길이(초)")
    generate_ai_images: bool = Field(default=True, description="에셋이 부족한 슬라이드/씬에 AI 이미지 생성 여부")
    card_format: str = Field(default="case_study", description="카드뉴스 형식 ID (services/card_formats.py)")
    brand_id: str = Field(default="", description="사용할 브랜드 킷 ID (비우면 설정의 기본 브랜드)")
    brand_elements: BrandElements = Field(default_factory=BrandElements)
    design_id: str = Field(default="classic", description="카드 디자인 템플릿 ID (services/design_templates.py)")
    assets: List[MediaAsset] = Field(default_factory=list, description="첨부된 이미지/동영상 에셋")

class EditableStoryboard(BaseModel):
    """AI 디렉터 생성 및 사용자 검수용 통합 스토리보드 스키마"""
    title: str = Field(description="콘텐츠 종합 타이틀")
    target_audience: str = Field(description="확정된 타겟 오디언스")
    tone_and_manner: str = Field(description="확정된 톤앤매너")
    visual_style: str = Field(description="확정된 비주얼 스타일")
    is_anonymized: bool = Field(description="실명 마스킹 적용 여부")
    card_slides: List[CardSlide] = Field(default_factory=list, description="카드뉴스 슬라이드 목록")
    shorts_scenes: List[ShortsScene] = Field(default_factory=list, description="쇼츠 씬 목록 (0~60초 타임라인)")
    sns_caption: str = Field(description="인스타그램/유튜브 본문용 마케팅 카피")
    hashtags: List[str] = Field(default_factory=list, description="타겟 맞춤형 추천 해시태그 세트")
    fact_check_log: List[str] = Field(default_factory=list, description="보존된 핵심 수치 및 팩트 락 검증 로그")
    engine: str = Field(default="", description="실제로 스토리보드를 생성한 엔진 (예: 'claude:claude-opus-5-5', 'rule-based')")
    card_format: str = Field(default="case_study")
    brand_id: str = Field(default="")
    brand_elements: BrandElements = Field(default_factory=BrandElements)
    design_id: str = Field(default="classic")
    images_ready: bool = Field(default=False, description="카드 만들기 단계에서 AI 이미지 보충을 거쳤는지")
    shorts_images_ready: bool = Field(default=False, description="쇼츠 만들기 단계에서 AI 이미지 보충을 거쳤는지")
    shorts_source_hash: str = Field(default="", description="쇼츠 대본을 만든 시점의 카드 지문 (카드가 바뀌면 다시 변환 필요)")
    assets: List[MediaAsset] = Field(default_factory=list, description="이 스토리보드가 참조하는 에셋 (첨부 + AI 생성)")

    def asset(self, asset_id: Optional[str]) -> Optional[MediaAsset]:
        if not asset_id:
            return None
        return next((a for a in self.assets if a.id == asset_id), None)
