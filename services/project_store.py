"""작업(프로젝트) 저장소 — 콘텐츠 하나 = 폴더 하나.

홍보 담당자가 탐색기에서 바로 찾을 수 있도록 기본 위치는 '문서/OSMU Media Studio/작업' 이다.
(OSMU_PROJECTS_DIR 환경변수로 바꿀 수 있음)

<작업 폴더>/
  project.json     작업 정보 · 상태 · 원고/옵션 · 스토리보드(카드: 배경 에셋 + 글자 + 레이아웃) · 확정 기록
  assets/          첨부 사진·영상, AI 이미지, QR 이미지 (카드·쇼츠가 함께 씀)
  cards/           카드 PNG (결과물, 스토리보드에서 언제든 다시 그림)
  shorts/          쇼츠 MP4
  export/          검토 시안 등 내보내기

에셋 경로는 project.json 안에 작업 폴더 기준 상대경로로 저장해, 폴더를 통째로 옮겨도 열린다.
"""
import os
import re
import json
import shutil
import filecmp
import hashlib
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field
from models.schema import EditableStoryboard, MediaAsset, BrandElements

PROJECTS_DIR = os.path.abspath(os.environ.get("OSMU_PROJECTS_DIR") or
                               os.path.join(os.path.expanduser("~"), "Documents", "삼호푸드 OSMU", "작업"))

STATUS_LABELS = {
    "draft": "원고 작성 중",
    "scenario": "시나리오",
    "cards": "카드 제작 중",
    "confirmed": "카드 확정",
    "shorts": "쇼츠 완료",
}


class Confirmation(BaseModel):
    """카드 확정 기록 (사내 승인·감사 근거)"""
    by: str
    note: str = ""
    at: str
    card_hash: str
    warnings: List[str] = Field(default_factory=list, description="확정 당시 남아 있던 점검 경고 (확인 후 확정)")


class ProjectInputs(BaseModel):
    """원고 단계 입력값 (다시 열었을 때 왼쪽 패널 복원용)"""
    raw_text: str = ""
    card_format: str = "case_study"
    card_count: int = 7
    shorts_seconds: int = 45
    target_audience: str = "AI 자동 추천"
    tone_and_manner: str = "AI 자동 추천"
    visual_style: str = "실사풍 / 인포그래픽"
    brand_elements: BrandElements = Field(default_factory=BrandElements)
    design_id: str = "classic"
    attachments: List[MediaAsset] = Field(default_factory=list)


class Project(BaseModel):
    id: str                                  # 폴더 이름 (예: 2026-10-29_신메뉴-출시)
    title: str
    created_at: str
    updated_at: str
    status: str = "draft"
    brand_id: str = ""
    inputs: ProjectInputs = Field(default_factory=ProjectInputs)
    storyboard: Optional[EditableStoryboard] = None
    confirmation: Optional[Confirmation] = None
    history: List[str] = Field(default_factory=list, description="주요 작업 기록 (시각 · 내용)")
    blog_title: str = ""
    blog_body: str = Field(default="", description="블로그 원고 평문 (## 소제목, [사진:A1], [카드:1] 표시 포함)")
    blog_hashtags: List[str] = Field(default_factory=list)
    blog_request: dict = Field(default_factory=dict, description="입력 폼 값 (타깃·상품·소식·메모·분량)")
    image_mode: str = Field(default="mine", description="블로그 이미지: mine | cards | both")

    @property
    def folder(self) -> str:
        return os.path.join(PROJECTS_DIR, self.id)

    def sub(self, name: str) -> str:
        path = os.path.join(self.folder, name)
        os.makedirs(path, exist_ok=True)
        return path

    @property
    def status_label(self) -> str:
        if self.status == "confirmed" and self.cards_changed_since_confirm():
            return "확정 후 수정됨"
        return STATUS_LABELS.get(self.status, self.status)

    def log(self, message: str):
        self.history.append(f"{_now()} · {message}")
        self.history = self.history[-200:]

    # ------------------------------------------------------------------
    # 카드 확정 · 변경 감지
    # ------------------------------------------------------------------
    def cards_changed_since_confirm(self) -> bool:
        return bool(self.confirmation and self.storyboard and card_hash(self.storyboard) != self.confirmation.card_hash)

    def confirm(self, by: str, note: str, warnings: List[str]):
        self.confirmation = Confirmation(by=by.strip(), note=note.strip(), at=_now(),
                                         card_hash=card_hash(self.storyboard), warnings=warnings)
        self.status = "confirmed"
        self.log(f"카드 확정 ({by.strip()})" + (f" — 경고 {len(warnings)}건 확인" if warnings else ""))


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def card_hash(sb: EditableStoryboard) -> str:
    """카드 내용(문구·배치 이미지·형식·브랜드 요소) 지문 — 확정 후 바뀌었는지 판단"""
    data = {
        "slides": [s.model_dump() for s in sb.card_slides],
        "brand": sb.brand_id,
        "elements": sb.brand_elements.model_dump(),
        "design": sb.design_id,
        "assets": sorted((a.id, os.path.basename(a.path)) for a in sb.assets),
    }
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def _slug(title: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|…,\s]+", "-", (title or "").strip())  # 윈도우 파일명 금지 문자·말줄임표 제거
    s = re.sub(r"-+", "-", s).strip("-.")
    return s[:30] or "새-콘텐츠"


def new_project(title: str) -> Project:
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    base = f"{datetime.now().strftime('%Y-%m-%d')}_{_slug(title)}"
    pid, n = base, 2
    while os.path.exists(os.path.join(PROJECTS_DIR, pid)):
        pid, n = f"{base}-{n}", n + 1
    now = _now()
    p = Project(id=pid, title=title.strip() or "새 콘텐츠", created_at=now, updated_at=now)
    os.makedirs(p.folder)
    p.log("작업 생성")
    return p


# ----------------------------------------------------------------------
# 저장 / 불러오기
# ----------------------------------------------------------------------
def _adopt_asset(p: Project, asset: MediaAsset) -> MediaAsset:
    """작업 폴더 밖에 있는 에셋 파일을 assets/로 복사하고, 저장용으로 상대경로를 가진 복사본 반환"""
    assets_dir = p.sub("assets")
    out = asset.model_copy(deep=True)

    def rel(path: str) -> str:
        if not path:
            return path
        ap = os.path.abspath(path)
        if not ap.startswith(os.path.abspath(p.folder) + os.sep):
            dest = os.path.join(assets_dir, os.path.basename(ap))
            if os.path.exists(ap) and (not os.path.exists(dest) or not filecmp.cmp(ap, dest, shallow=False)):
                shutil.copy2(ap, dest)
            ap = dest
        return os.path.relpath(ap, p.folder).replace(os.sep, "/")

    out.path = rel(asset.path)
    out.keyframes = [rel(k) for k in asset.keyframes]
    return out


def _adopt_file(p: Project, path: str) -> str:
    if not path:
        return path
    return _adopt_asset(p, MediaAsset(id="_", kind="image", path=path)).path


def _absolute(p: Project, asset: MediaAsset) -> MediaAsset:
    absp = lambda x: x if (not x or os.path.isabs(x)) else os.path.join(p.folder, x.replace("/", os.sep))
    asset.path = absp(asset.path)
    asset.keyframes = [absp(k) for k in asset.keyframes]
    return asset


def save(p: Project) -> None:
    """작업 저장. 메모리의 경로는 그대로 두고, 파일에는 작업 폴더 기준 상대경로로 기록"""
    p.updated_at = _now()
    data = p.model_copy(deep=True)
    data.inputs.attachments = [_adopt_asset(p, a) for a in p.inputs.attachments]
    data.inputs.brand_elements.qr_image = _adopt_file(p, p.inputs.brand_elements.qr_image)
    if data.storyboard:
        data.storyboard.assets = [_adopt_asset(p, a) for a in p.storyboard.assets]
        data.storyboard.brand_elements.qr_image = _adopt_file(p, p.storyboard.brand_elements.qr_image)
    tmp = os.path.join(p.folder, "project.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data.model_dump_json(indent=2))
    os.replace(tmp, os.path.join(p.folder, "project.json"))
    # 이후 작업은 작업 폴더 안의 복사본을 쓰도록 메모리 경로도 바꿈 (같은 객체를 쓰는 화면에도 반영)
    absf = lambda x: x if (not x or os.path.isabs(x)) else os.path.join(p.folder, x.replace("/", os.sep))
    for live, saved in zip(p.inputs.attachments, data.inputs.attachments):
        live.path, live.keyframes = absf(saved.path), [absf(k) for k in saved.keyframes]
    p.inputs.brand_elements.qr_image = absf(data.inputs.brand_elements.qr_image)
    if p.storyboard:
        for live, saved in zip(p.storyboard.assets, data.storyboard.assets):
            live.path, live.keyframes = absf(saved.path), [absf(k) for k in saved.keyframes]
        p.storyboard.brand_elements.qr_image = absf(data.storyboard.brand_elements.qr_image)


def load(project_id: str) -> Project:
    with open(os.path.join(PROJECTS_DIR, project_id, "project.json"), encoding="utf-8") as f:
        p = Project.model_validate_json(f.read())
    p.id = project_id  # 폴더 이름이 바뀌었어도 열리도록
    for a in p.inputs.attachments:
        _absolute(p, a)
    absf = lambda x: x if (not x or os.path.isabs(x)) else os.path.join(p.folder, x.replace("/", os.sep))
    p.inputs.brand_elements.qr_image = absf(p.inputs.brand_elements.qr_image)
    if p.storyboard:
        for a in p.storyboard.assets:
            _absolute(p, a)
        p.storyboard.brand_elements.qr_image = absf(p.storyboard.brand_elements.qr_image)
    return p


def list_projects() -> List[Project]:
    """최근 수정 순 작업 목록 (깨진 폴더는 건너뜀)"""
    if not os.path.isdir(PROJECTS_DIR):
        return []
    out = []
    for name in os.listdir(PROJECTS_DIR):
        if os.path.exists(os.path.join(PROJECTS_DIR, name, "project.json")):
            try:
                out.append(load(name))
            except Exception:
                continue
    return sorted(out, key=lambda p: p.updated_at, reverse=True)


def delete_project(project_id: str) -> None:
    folder = os.path.join(PROJECTS_DIR, project_id)
    if os.path.abspath(folder).startswith(PROJECTS_DIR + os.sep) and os.path.isdir(folder):
        shutil.rmtree(folder)
