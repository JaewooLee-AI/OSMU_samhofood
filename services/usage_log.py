"""AI 사용량·비용 기록 (고객이 직접 API 비용을 부담하므로 어디에 얼마나 썼는지 보여 준다).

- 호출마다 한 줄(JSON)씩 DATA_DIR/usage.jsonl 에 기록: 시각·작업·기능·모델·입력/출력 토큰·이미지 수·영상 초
- 단가는 DATA_DIR/prices.json — 설정 화면에서 고칠 수 있고, 금액은 '지금 단가'로 계산한다(단가를 고치면 과거 기록 금액도 다시 계산됨).
  단가를 모르는 모델은 토큰만 세고 금액은 '단가 미등록'으로 표시한다. (요금은 바뀌므로 공식 요금표를 확인해 고쳐 쓰세요)
- 기본 단가는 Google 공식 문서·AI Studio 화면에서 확인한 값만 넣는다: Veo(초당), 나노바나나 계열(이미지당), Omni(영상 초당 어림).
"""
import os
import json
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from services.paths import DATA_DIR

USAGE_PATH = os.path.join(DATA_DIR, "usage.jsonl")
PRICES_PATH = os.path.join(DATA_DIR, "prices.json")
DEFAULT_USD_KRW = 1400.0  # 시작값일 뿐 실제 환율이 아님 — 설정에서 고쳐 쓰기
_LOCK = threading.Lock()
_CONTEXT = {"project": ""}

# 키: "벤더/모델". in/out = 100만 토큰당 USD, per_image = 이미지 1장당 USD, per_second = 영상 1초당 USD
DEFAULT_PRICES: Dict[str, Dict[str, float]] = {
    "gemini/gemini-2.5-flash-image": {"in": 0.30, "per_image": 0.039},
    # 아래 3개는 사용자가 AI Studio 모델 선택 화면에서 확인해 준 가격 (2026-10-30). Omni 영상 초당 단가는 출력 $17.50/100만토큰 ÷ 약 5,792토큰/초 ≈ $0.10 어림값
    "gemini/gemini-3.1-flash-lite-image": {"in": 0.25, "out": 1.50, "per_image": 0.0336},
    "gemini/gemini-nano-banana-2.1": {"in": 1.50, "out": 7.50, "per_image": 0.0336},
    "gemini/gemini-omni-1.1-flash": {"in": 1.50, "out": 9.00, "per_second": 0.10},
    # AI Studio 모델 선택 화면 값 (2026-10-30): 글자 입출력 $/100만토큰, 이미지는 출력 장당
    "gemini/gemini-3.1-flash-image": {"in": 0.50, "out": 3.00, "per_image": 0.0672},
    "gemini/gemini-3-pro-image": {"in": 2.00, "out": 12.00, "per_image": 0.134},
    "gemini/gemini-3.8-flash": {"in": 0.75, "out": 3.75},   # 2026-12-31까지 가격, 이후는 요금표 확인
    "gemini/gemini-3.6-flash": {"in": 0.75, "out": 3.75},   # 2026-12-31까지 가격
    "gemini/gemini-3.5-flash-lite": {"in": 0.30, "out": 2.50},
    "gemini/gemini-3.1-flash-lite": {"in": 0.25, "out": 1.50},
    "gemini/veo-3.1-lite-generate-preview": {"per_second": 0.08},
    "gemini/veo-3.1-fast-generate-preview": {"per_second": 0.12},
    "gemini/veo-3.1-generate-preview": {"per_second": 0.40},
}


def set_project(project_id: str) -> None:
    """이후 기록을 어느 작업의 비용으로 묶을지 (작업을 열거나 만들 때 지정)"""
    _CONTEXT["project"] = project_id or ""


def _load_prices_file() -> dict:
    try:
        with open(PRICES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_prices() -> dict:
    """{"usd_krw": 환율, "budget_krw": 월 예산(0=경고 끔), "models": {키: 단가}} — 기본 단가에 사용자가 고친 값을 덮어 씀"""
    data = _load_prices_file()
    models = {k: dict(v) for k, v in DEFAULT_PRICES.items()}
    for k, v in (data.get("models") or {}).items():
        models[k] = {**models.get(k, {}), **v}
    return {"usd_krw": float(data.get("usd_krw") or DEFAULT_USD_KRW), "budget_krw": float(data.get("budget_krw") or 0), "models": models}


def save_prices(usd_krw: Optional[float] = None, budget_krw: Optional[float] = None,
                models: Optional[Dict[str, Dict[str, float]]] = None) -> None:
    data = load_prices()
    if usd_krw is not None and usd_krw > 0:
        data["usd_krw"] = float(usd_krw)
    if budget_krw is not None:
        data["budget_krw"] = max(0.0, float(budget_krw))
    for k, v in (models or {}).items():
        data["models"][k] = {kk: float(vv) for kk, vv in v.items() if vv is not None}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PRICES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record(feature: str, vendor: str, model: str, in_tokens: int = 0, out_tokens: int = 0,
           images: int = 0, seconds: float = 0.0, note: str = "") -> None:
    """AI 호출 1건 기록. 기록 실패가 작업을 막지 않도록 오류는 무시한다."""
    entry = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "project": _CONTEXT["project"], "feature": feature,
             "vendor": vendor, "model": model, "in": int(in_tokens or 0), "out": int(out_tokens or 0),
             "images": int(images or 0), "seconds": round(float(seconds or 0), 1), "note": note}
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with _LOCK, open(USAGE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def record_gemini(feature: str, model: str, response, images: int = 0) -> None:
    """google-genai 응답의 usage_metadata 로 기록 (생각 토큰은 출력 요금에 포함)"""
    u = getattr(response, "usage_metadata", None)
    out = (getattr(u, "candidates_token_count", 0) or 0) + (getattr(u, "thoughts_token_count", 0) or 0)
    record(feature, "gemini", model, getattr(u, "prompt_token_count", 0) or 0, out, images=images)


def record_claude(feature: str, model: str, response) -> None:
    u = getattr(response, "usage", None)
    record(feature, "claude", model, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0)


def entry_cost_usd(e: dict, prices: dict) -> Optional[float]:
    """기록 1건의 금액(USD). 쓰인 항목의 단가가 하나라도 없으면 None(단가 미등록)"""
    p = prices["models"].get(f"{e['vendor']}/{e['model']}")
    if not p:
        return None
    cost, missing = 0.0, False
    for used, key, per in ((e["in"], "in", 1e6), (e["out"], "out", 1e6), (e["images"], "per_image", 1), (e["seconds"], "per_second", 1)):
        if not used:
            continue
        if key not in p:
            if key == "out" and e["images"] and "per_image" in p:
                continue  # 이미지 출력은 이미지당 단가로 이미 계산
            missing = True
            continue
        cost += used * p[key] / per
    return None if missing else cost


def read_entries(month: Optional[str] = None, project: Optional[str] = None) -> List[dict]:
    """month: 'YYYY-MM' (None이면 전체), project: 작업 ID (None이면 전체)"""
    out = []
    try:
        with open(USAGE_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                e.setdefault("in", 0), e.setdefault("out", 0), e.setdefault("images", 0), e.setdefault("seconds", 0)
                if (month and not e.get("ts", "").startswith(month)) or (project is not None and e.get("project") != project):
                    continue
                out.append(e)
    except OSError:
        pass
    return out


def summarize(month: Optional[str] = None, project: Optional[str] = None) -> dict:
    """{'usd','krw','calls','in','out','images','seconds','unpriced':[모델…],'by_feature':{기능:{…}},'by_model':{…}}"""
    prices = load_prices()
    rate = prices["usd_krw"]
    total = {"usd": 0.0, "calls": 0, "in": 0, "out": 0, "images": 0, "seconds": 0.0}
    by_feature: Dict[str, dict] = {}
    by_model: Dict[str, dict] = {}
    unpriced = set()
    for e in read_entries(month, project):
        cost = entry_cost_usd(e, prices)
        if cost is None:
            unpriced.add(f"{e['vendor']}/{e['model']}")
        for bucket in (total, by_feature.setdefault(e["feature"], {"usd": 0.0, "calls": 0, "in": 0, "out": 0, "images": 0, "seconds": 0.0}),
                       by_model.setdefault(f"{e['vendor']}/{e['model']}", {"usd": 0.0, "calls": 0, "in": 0, "out": 0, "images": 0, "seconds": 0.0})):
            bucket["usd"] += cost or 0.0
            bucket["calls"] += 1
            for k in ("in", "out", "images", "seconds"):
                bucket[k] += e[k]
    for b in [total, *by_feature.values(), *by_model.values()]:
        b["krw"] = b["usd"] * rate
    return {**total, "unpriced": sorted(unpriced), "by_feature": by_feature, "by_model": by_model, "usd_krw": rate}


def current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def budget_status() -> Tuple[float, float, int]:
    """(이번 달 사용 원화, 월 예산 원화, 사용률 %) — 예산이 0이면 사용률 -1"""
    s = summarize(current_month())
    budget = load_prices()["budget_krw"]
    return s["krw"], budget, (int(s["krw"] / budget * 100) if budget else -1)


def won(krw: float) -> str:
    return f"{krw:,.0f}원" if krw >= 1 else ("1원 미만" if krw > 0 else "0원")


def mark() -> int:
    """기록 파일의 현재 줄 수 (이후 기록을 작업에 귀속시킬 때 기준)"""
    try:
        with open(USAGE_PATH, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def claim(since_line: int, project_id: str) -> None:
    """since_line 이후 작업이 비어 있는 기록을 이 작업의 것으로 (작업이 AI 호출 뒤에 만들어지는 경우)"""
    try:
        with _LOCK:
            with open(USAGE_PATH, encoding="utf-8") as f:
                lines = f.readlines()
            for i in range(since_line, len(lines)):
                try:
                    e = json.loads(lines[i])
                except ValueError:
                    continue
                if not e.get("project"):
                    e["project"] = project_id
                    lines[i] = json.dumps(e, ensure_ascii=False) + "\n"
            with open(USAGE_PATH, "w", encoding="utf-8") as f:
                f.writelines(lines)
    except OSError:
        pass
