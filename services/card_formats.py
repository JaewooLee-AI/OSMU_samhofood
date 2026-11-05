"""카드뉴스 형식 목록. status가 planned인 형식은 화면에 '준비 중'으로만 표시한다.

형식을 새로 구현할 때: 여기 status를 available로 바꾸고, ai_director의 _rule_based_<id> 와 llm_structure 문구,
필요하면 card_renderer의 슬라이드 레이아웃을 추가한다.
"""
import re
from typing import Dict, List

FORMATS: List[Dict[str, str]] = [
    {"id": "case_study", "name": "사례 연구형", "status": "available",
     "desc": "문제 → 해결 → 결과 구조로 실제 사례를 전달",
     "template": '[【사례 제목 한 줄 — 예: 60대 부모님과 다낭 단독여행】]\n상황: 【누가, 어떤 고민이나 문제가 있었는지 1~2문장】\n전개: 【무엇을 어떻게 했는지 1~2문장】\n결과: 【달라진 점 1~2문장 — 수치가 있으면 단위와 함께 (예: 만족도 92%)】',
     "tips": ['제목은 [대괄호]로 한 줄', "'상황 → 전개 → 결과' 순서로 쓰면 카드 구성이 가장 정확합니다", '수치는 원문 그대로 단위와 함께, 실제 있는 것만 쓰세요', '고객 실명·얼굴이 드러나는 내용은 피하세요'],
     "fit": "경험담·도입 사례·후기 등 이야기가 있는 원고 (가장 무난한 기본 형식)",
     "llm_structure": "1장 cover, 상황(문제)·해결 과정·결과를 body로, 원문에 비교 대상 두 가지가 직접 있을 때만 comparison_table 1장(없으면 만들지 말 것), 브랜드 가치 설명이 필요하면 '왜 우리인가' body 1장, 마지막 call_to_action"},
    {"id": "info_summary", "name": "정보 요약형", "status": "available",
     "desc": "'핵심 N가지'처럼 번호를 매긴 리스트로 정리",
     "template": '【주제 한 줄 — 예: 처음 이용하기 전 확인할 4가지】\n1. 【첫 번째 항목과 한 줄 설명】\n2. 【두 번째 항목과 한 줄 설명】\n3. 【세 번째 항목과 한 줄 설명】\n4. 【네 번째 항목과 한 줄 설명】',
     "tips": ['항목은 3~5개가 보기 좋습니다 (카드 한 장에 항목 하나)', "항목마다 '무엇 + 왜/어떻게'를 한 줄로", "제목에 개수를 넣으면 표지가 '핵심 N가지'로 나옵니다"],
     "fit": "팁·체크 포인트·특징처럼 나열할 항목이 여러 개인 원고",
     "llm_structure": "1장 cover(제목에 '핵심 N가지'처럼 개수를 명시), 이어서 numbered 카드를 개수만큼(badge에 01, 02 …), 마지막 call_to_action"},
    {"id": "comparison", "name": "비교형 (VS)", "status": "available",
     "desc": "도입 전/후, A안 vs B안을 표와 대비로 비교",
     "template": '【무엇과 무엇을 비교하는지 한 줄 — 예: 단독 패키지 vs 조인(모객) 상품】\n비교 대상 A(【이름】): 【특징 2~3가지】\n비교 대상 B(【이름】): 【특징 2~3가지】\n차이: 【핵심 차이 1~2문장 — 어느 쪽이 어떤 경우에 맞는지】',
     "tips": ['같은 기준(가격·일정·인원 등)으로 A와 B를 맞춰 쓰세요', '특정 업체나 상품을 깎아내리지 말고 사실만 비교하세요', "'전/후', 'vs', '차이' 같은 말이 들어가면 비교형으로 인식이 잘 됩니다"],
     "fit": "전/후 또는 두 대상을 견줄 수 있는 원고 (예: 일반 패키지 vs 단독 패키지)",
     "llm_structure": "1장 cover(무엇과 무엇을 비교하는지), 2장 comparison_table(comparison_rows 첫 행은 ['구분','비교 대상 A','비교 대상 B']), 이후 비교 항목별 body, 결론 body, 마지막 call_to_action"},
    {"id": "big_number", "name": "빅넘버·인포그래픽형", "status": "available",
     "desc": "핵심 수치를 크게 보여 주는 데이터 중심 구성",
     "template": '【주제 한 줄 — 예: 가족여행 혜택을 숫자로 보기】\n핵심 수치 1: 【숫자+단위】 — 【무엇의 수치인지 한 줄】\n핵심 수치 2: 【숫자+단위】 — 【무엇의 수치인지 한 줄】\n핵심 수치 3: 【숫자+단위】 — 【무엇의 수치인지 한 줄】\n참고: 【수치의 기간·범위·조건 — 예: 2026년 상반기 기준】',
     "tips": ['수치는 2~4개, 반드시 단위(%, 원, 명, 점 등)와 함께', '원문에 실제 있는 수치만 쓰세요 (없는 숫자는 만들지 않습니다)', "기준 기간·조건을 '참고'에 적으면 근거가 분명해집니다"],
     "fit": "만족도 92%, 1,200만원 절감처럼 성과 수치가 2개 이상 있는 원고",
     "llm_structure": "1장 cover, 원문에 있는 수치마다 big_number 카드(stat_value에 원문 수치 그대로, stat_label에 한 줄 설명), 마지막 call_to_action. 원문에 없는 숫자는 만들지 말 것"},
    {"id": "event_notice", "name": "공지·이벤트 안내형", "status": "available",
     "desc": "일시·대상·혜택·신청 방법을 한눈에 정리해 알리는 안내 구성",
     "template": '【이벤트·행사·공지 제목 한 줄】\n일시: 【날짜·요일·시간】\n대상: 【누가 참여할 수 있는지】\n장소: 【오프라인 장소 또는 온라인 링크 안내】\n혜택: 【참가자가 받는 것 — 원문에 있는 것만】\n참가비: 【무료 또는 금액, 인원 제한이 있으면 함께】\n신청 방법: 【1단계 → 2단계 → 3단계】\n마감: 【신청 마감 일시】\n유의사항: 【조건·제외 대상·중복 불가 등】',
     "tips": ["첫 줄이 제목이고, 나머지는 '항목: 값' 줄로 쓰면 가장 정확합니다", '혜택에는 조건(기간·대상·수량)을 함께 적어야 표시광고 점검에 걸리지 않습니다', "신청 방법은 '→'로 단계를 이으면 번호 카드로 나옵니다", '필요 없는 줄은 지워도 됩니다'],
     "fit": "일시·대상·혜택·신청 방법이 있는 이벤트·행사·프로모션·공지 원고 ('항목: 값' 줄로 적으면 가장 정확)",
     "llm_structure": "1장 cover(이벤트·공지 제목과 한 줄 요약), 2장 event_info(body_content는 한 줄에 '라벨: 값' — 일시/기간, 대상, 장소, 혜택, 참가비·조건 등 원문에 있는 항목만), 3장 body(참여·신청 방법을 1. 2. 3. 순서로), 4장 body(유의사항·조건·마감), 마지막 call_to_action(신청·문의). 원문에 없는 날짜·금액·혜택·프로그램(예: 1:1 상담, 선물, 특강)은 절대 만들지 말고, 혜택·참가비·마감 조건은 원문 문구 그대로 쓸 것. 장수가 남으면 원문 내용을 나눠 쓰고 회사 소개로 채우지 말 것"},
    {"id": "qna", "name": "Q&A·FAQ형", "status": "planned", "desc": "자주 묻는 질문과 답을 한 장씩 정리"},
    {"id": "checklist", "name": "체크리스트·자가진단형", "status": "planned", "desc": "체크 항목과 결과·해결책 제시"},
    {"id": "storytelling", "name": "스토리텔링형", "status": "planned", "desc": "사연·대화로 공감을 이끄는 기승전결 구성"},
    {"id": "text_minimal", "name": "텍스트 위주형", "status": "planned", "desc": "그래픽 없이 굵은 타이포만으로 전달"},
    {"id": "howto", "name": "단계별 안내형", "status": "planned", "desc": "신청 절차·이용 방법을 Step 1, 2, 3 순서로 안내"},
    {"id": "quiz", "name": "퀴즈·참여형", "status": "planned", "desc": "문제와 '정답은 다음 장에' 구성"},
    {"id": "messenger", "name": "메신저 화면형", "status": "planned", "desc": "카톡·DM 화면을 빌려 대화로 전개"},
    {"id": "panorama", "name": "파노라마형", "status": "planned", "desc": "넘길 때 이어지는 한 장의 큰 그림"},
]

SINGLE_CARD_STRUCTURE = ("카드 1장(slide_type single)에 제목·핵심 내용 2~3줄·요약을 모두 담을 것. "
                         "원문에 핵심 수치가 있으면 stat_value/stat_label에 하나만 넣을 것")


def get_format(format_id: str) -> Dict[str, str]:
    fmt = next((f for f in FORMATS if f["id"] == format_id and f["status"] == "available"), None)
    return fmt or FORMATS[0]


EVENT_WORDS = re.compile(r"이벤트|행사|프로모션|공지|안내|신청|접수|모집|혜택|일시|기간|마감|\d+월\s*\d+일|\d+일까지")
COMPARE_WORDS = re.compile(r"전후|전/후|도입 전|도입 후|비교|대비|보다|반면|vs|VS|차이")


def fit_warning(format_id: str, numbers: List[str], text: str) -> str:
    """원고가 선택한 형식에 맞는지 간단히 점검. 맞지 않으면 안내 문구, 괜찮으면 빈 문자열"""
    if format_id == "big_number" and len(numbers) < 2:
        return (f"이 원고에는 성과 수치가 {len(numbers)}개뿐이라 빅넘버형에 잘 맞지 않습니다. "
                "경험담이면 '사례 연구형'이나 '정보 요약형'을 권합니다.")
    if format_id == "comparison" and not COMPARE_WORDS.search(text or ""):
        return "이 원고에는 비교할 두 대상(전/후, A vs B)이 보이지 않아 비교형에 잘 맞지 않습니다. '사례 연구형'을 권합니다."
    if format_id == "event_notice" and len(EVENT_WORDS.findall(text or "")) < 2:
        return "이 원고에는 일시·대상·신청 방법 같은 행사 정보가 거의 보이지 않아 이벤트 안내형에 잘 맞지 않습니다. 원고에 '일시: …' '대상: …' '신청: …'을 적어 보세요."
    return ""


def get_template(format_id: str, brand_name: str = "") -> str:
    """형식별 작성 틀 (【 】 안을 채워 쓰는 원고 양식). {브랜드}는 선택한 브랜드 이름으로 바뀜"""
    fmt = next((f for f in FORMATS if f["id"] == format_id), None)
    return (fmt.get("template", "") if fmt else "").replace("{브랜드}", brand_name or "우리 회사")


PLACEHOLDER_RE = re.compile(r"【[^】]*】")


def has_placeholder(text: str) -> bool:
    """작성 틀의 【 】 칸이 채워지지 않고 남아 있는지"""
    return bool(PLACEHOLDER_RE.search(text or ""))


def available() -> List[Dict[str, str]]:
    return [f for f in FORMATS if f["status"] == "available"]
