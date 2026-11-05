"""업종 컴플라이언스 팩 적용 엔진 (공통 코드).

업종별 규칙은 compliance_packs/*.json 데이터로만 표현하고, 이 엔진은 규칙 '유형'만 안다.
  - replacements      : 금지 표현 → 대체 표현 (자동 치환)
  - warn_patterns     : 위험 표현 정규식 → 경고 (사람이 확인)
  - conditional       : A를 말하면 B도 표기해야 함 (예: 혜택 → 조건)
  - required_notices  : 반드시 넣을 고지 문구 (마지막 카드·캡션에 자동 추가)
  - audit_checklist   : AI 기획 단계에 주는 점검 지침
팩은 inherits로 다른 팩(예: general)을 이어받고, 브랜드 킷의 회사 전용 치환·주의 단어가 마지막에 더해진다.
"""
import os
import re
import json
from typing import Dict, List, Tuple
from pydantic import BaseModel, Field
from services.paths import BASE_DIR

PACKS_DIR = os.path.join(BASE_DIR, "compliance_packs")


class WarnPattern(BaseModel):
    pattern: str
    reason: str


class ConditionalRule(BaseModel):
    if_pattern: str
    require_pattern: str
    message: str


class RequiredNotice(BaseModel):
    text: str
    placement: str = "last_card"   # last_card | caption


class CompliancePack(BaseModel):
    id: str
    name: str
    description: str = ""
    inherits: List[str] = Field(default_factory=list)
    laws: List[str] = Field(default_factory=list)
    replacements: Dict[str, str] = Field(default_factory=dict)
    warn_patterns: List[WarnPattern] = Field(default_factory=list)
    conditional: List[ConditionalRule] = Field(default_factory=list)
    required_notices: List[RequiredNotice] = Field(default_factory=list)
    audit_checklist: List[str] = Field(default_factory=list)


def list_packs() -> List[CompliancePack]:
    packs = []
    for name in sorted(os.listdir(PACKS_DIR)):
        if name.endswith(".json"):
            with open(os.path.join(PACKS_DIR, name), encoding="utf-8") as f:
                packs.append(CompliancePack.model_validate(json.load(f)))
    return packs


def load_pack(pack_id: str, _seen=None) -> CompliancePack:
    """상속(inherits)을 펼친 최종 팩"""
    _seen = _seen or set()
    path = os.path.join(PACKS_DIR, f"{pack_id}.json")
    if not os.path.exists(path):
        path = os.path.join(PACKS_DIR, "general.json")
    with open(path, encoding="utf-8") as f:
        pack = CompliancePack.model_validate(json.load(f))
    merged = CompliancePack(id=pack.id, name=pack.name, description=pack.description)
    for parent in pack.inherits:
        if parent in _seen:
            continue
        base = load_pack(parent, _seen | {pack.id})
        merged.laws += base.laws
        merged.replacements.update(base.replacements)
        merged.warn_patterns += base.warn_patterns
        merged.conditional += base.conditional
        merged.required_notices += base.required_notices
        merged.audit_checklist += base.audit_checklist
    merged.laws += pack.laws
    merged.replacements.update(pack.replacements)
    merged.warn_patterns += pack.warn_patterns
    merged.conditional += pack.conditional
    merged.required_notices += pack.required_notices
    merged.audit_checklist += pack.audit_checklist
    return merged


def prompt_rules(pack: CompliancePack, kit) -> str:
    """AI 기획 프롬프트에 넣을 업종·회사 준수 지침"""
    lines = [f"- 적용 기준: {pack.name}"]
    lines += [f"- 관련 법령: {law}" for law in pack.laws]
    lines += [f"- {item}" for item in pack.audit_checklist]
    avoid = list(pack.replacements) + list(kit.extra_replacements) + list(kit.extra_warn_terms)
    if avoid:
        lines.append(f"- 쓰지 말 표현: {', '.join(a.strip() for a in avoid if a.strip())}")
    return "\n".join(lines)


def _texts(sb) -> List[Tuple[str, object, str]]:
    """(위치 라벨, 객체, 필드명) — 검사·치환 대상 문구"""
    out = [("제목", sb, "title"), ("SNS 캡션", sb, "sns_caption")]
    for s in sb.card_slides:
        for field, label in (("lead_headline", "제목"), ("body_content", "본문"), ("takeaway", "요약")):
            out.append((f"카드 {s.slide_number} {label}", s, field))
    for sc in sb.shorts_scenes:
        out.append((f"쇼츠 {sc.scene_number} 내레이션", sc, "narration_script"))
        out.append((f"쇼츠 {sc.scene_number} 문구", sc, "display_caption"))
    return out


def apply(sb, pack: CompliancePack, kit) -> List[str]:
    """스토리보드에 팩 + 회사 규칙 적용. 반환: 검증 로그(✅ 정보 / 🔁 치환 / ⚠️ 경고)"""
    logs: List[str] = [f"🛡️ 컴플라이언스: {pack.name}"]
    replacements = {**pack.replacements, **kit.extra_replacements}
    replaced = 0
    for _, obj, field in _texts(sb):
        text = getattr(obj, field) or ""
        new = text
        for bad, good in replacements.items():
            if bad and bad in new:
                new = new.replace(bad, good)
        if new != text:
            setattr(obj, field, new)
            replaced += 1
    if replaced:
        logs.append(f"🔁 금지 표현 자동 치환: {replaced}곳")

    warns = list(pack.warn_patterns) + [WarnPattern(pattern=re.escape(t), reason="브랜드 킷에 등록된 회사 주의 단어입니다.")
                                         for t in kit.extra_warn_terms if t.strip()]
    seen = set()
    for label, obj, field in _texts(sb):
        text = getattr(obj, field) or ""
        for w in warns:
            m = re.search(w.pattern, text)
            if m and (label, w.pattern) not in seen:
                seen.add((label, w.pattern))
                logs.append(f"⚠️ 표현 확인 필요 · {label}: '{m.group(0)}' — {w.reason}")

    # 조건부 규칙은 '같은 문장' 안에서 확인 (다른 문장의 '까지' 등으로 잘못 충족되지 않도록)
    for rule in pack.conditional:
        for label, obj, field in _texts(sb):
            for sentence in re.split(r"(?<=[.!?\n])\s*", getattr(obj, field) or ""):
                if re.search(rule.if_pattern, sentence) and not re.search(rule.require_pattern, sentence):
                    logs.append(f"⚠️ 표기 누락 가능성 · {label}: '{sentence.strip()[:30]}' — {rule.message}")
                    break
            else:
                continue
            break
    full = " ".join(getattr(o, f) or "" for _, o, f in _texts(sb))

    for notice in pack.required_notices:
        if notice.text in full:
            continue
        if notice.placement == "caption":
            sb.sns_caption = (sb.sns_caption or "").rstrip() + "\n\n" + notice.text
        elif sb.card_slides:
            last = sb.card_slides[-1]
            last.body_content = (last.body_content or "").rstrip() + "\n" + notice.text
        logs.append(f"📌 필수 고지 추가: {notice.text}")

    if len(logs) == 1 + (1 if replaced else 0):
        logs.append("✅ 표현 점검: 경고 없음")
    return logs


def review(sb, pack: CompliancePack, kit) -> List[str]:
    """카드 확정 전 점검: 문구를 바꾸지 않고 경고·치환 대상만 알려 준다 (사람이 확인 후 확정)"""
    copy = sb.model_copy(deep=True)
    copy.shorts_scenes = []  # 확정 대상은 카드
    found = []
    for line in apply(copy, pack, kit):
        if line.startswith("🔁"):
            found.append("⚠️ 금지 표현이 남아 있습니다 (" + line.split(":", 1)[-1].strip() + ") — 문구를 고쳐 주세요.")
        elif line.startswith("⚠️"):
            found.append(line)
        elif line.startswith("📌"):
            found.append("⚠️ 필수 고지 문구가 빠졌습니다: " + line.split(":", 1)[-1].strip())
    return found
