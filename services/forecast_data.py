"""주간 50주 예측 JSON(9개 자산 · 1개 파일) 읽기·점검.

형식 정의: new_brand/FORECAST_JSON_SPEC.md (schemaVersion 1.0)
점검 결과는 (수준, 자산, 필드, 메시지) 목록: 수준은 error(카드를 만들 수 없음) / warn(확인 필요).
"""
import re
import json
from typing import Any, Dict, List, Optional, Tuple

ASSET_IDS = ["NASDAQ", "SP500", "KOSPI", "GOLD", "10YUSB", "DXY", "USDKRW", "USDJPY", "WTI"]
ASSET_TYPES = {"equity", "bond_yield", "fx", "commodity"}
UNITS = {"pt", "%", "원", "엔", "$"}
DIRECTIONS = {"up", "down", "range"}
SCENARIO_ORDER = ["Base", "Tail A", "Tail B"]
SCENARIO_KEY = {"Base": "base", "Tail A": "tailA", "Tail B": "tailB"}
KEYPOINT_TYPES = {"low", "high", "turn"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
YM_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
MONTH_RE = re.compile(r"^\d{2}\.\d{2}$")
PERIOD_RE = re.compile(r"^\d{4}-\d{2}-\d{2} ~ \d{4}-\d{2}-\d{2}$")
MONTH_PERIOD_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})? ~ \d{4}-\d{2}(-\d{2})?$")   # 시점 표시는 월 단위 기간도 허용
BANNED = re.compile(r"매수하세요|매도하세요|사세요|파세요|보장|(?<!불)확실|무조건|반드시")
TRADE_WORDS = re.compile(r"매수|매도|청산|숏|롱|Long|Short|SHORT|LONG|BUY|SELL|EXIT")
# 단위별로 말이 안 되는 값 범위 (예: 금리에 105가 들어가면 채권가격 지수를 넣은 것)
UNIT_RANGE = {"%": (-5, 30)}

Issue = Tuple[str, str, str, str]


def load(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate(data: Dict[str, Any]) -> List[Issue]:
    out: List[Issue] = []
    err = lambda a, f, m: out.append(("error", a, f, m))
    warn = lambda a, f, m: out.append(("warn", a, f, m))

    if data.get("schemaVersion") != "1.0":
        warn("-", "schemaVersion", f"'1.0' 이어야 합니다 (현재 {data.get('schemaVersion')!r})")
    meta = data.get("meta") or {}
    for k in ("asOf", "week", "forecastStart", "forecastEnd", "modelName"):
        if meta.get(k) in (None, ""):
            err("meta", k, "필수 값이 없습니다")
    for k in ("asOf", "forecastStart", "forecastEnd"):
        if meta.get(k) and not DATE_RE.match(str(meta[k])):
            err("meta", k, f"날짜는 YYYY-MM-DD 형식이어야 합니다 ({meta[k]!r})")
    if meta.get("week") is not None and not isinstance(meta.get("week"), int):
        err("meta", "week", "정수여야 합니다")

    assets = data.get("assets") or {}
    for aid in ASSET_IDS:
        if aid not in assets:
            err(aid, "assets", "자산이 없습니다")
    for aid in assets:
        if aid not in ASSET_IDS:
            warn(aid, "assets", "정의서에 없는 자산 ID입니다")

    for aid, a in assets.items():
        _check_asset(aid, a, meta, err, warn)
    return out


def _check_asset(aid, a, meta, err, warn):
    if a.get("id") != aid:
        err(aid, "id", f"키({aid})와 id({a.get('id')!r})가 다릅니다")
    for k in ("name", "shortName", "valueMeaning", "characteristics", "lastCloseDate"):
        if not a.get(k):
            err(aid, k, "필수 값이 없습니다")
    if a.get("assetType") not in ASSET_TYPES:
        err(aid, "assetType", f"{sorted(ASSET_TYPES)} 중 하나여야 합니다 ({a.get('assetType')!r})")
    unit = a.get("unit")
    if unit not in UNITS:
        err(aid, "unit", f"{sorted(UNITS)} 중 하나여야 합니다 ({unit!r})")
    if not isinstance(a.get("decimals"), int):
        err(aid, "decimals", "정수여야 합니다")
    if not _num(a.get("lastClose")):
        err(aid, "lastClose", f"숫자여야 합니다 ({a.get('lastClose')!r})")
    if a.get("lastCloseDate") and not DATE_RE.match(str(a["lastCloseDate"])):
        err(aid, "lastCloseDate", f"YYYY-MM-DD 형식이어야 합니다 ({a['lastCloseDate']!r})")
    if a.get("prevWeekClose") is not None and not _num(a.get("prevWeekClose")):
        err(aid, "prevWeekClose", "숫자여야 합니다")
    if aid == "10YUSB" and (unit != "%" or a.get("assetType") != "bond_yield"):
        err(aid, "unit", "10년물은 unit '%', assetType 'bond_yield' 여야 합니다")

    values: List[Tuple[str, float]] = []   # 단위 범위 점검용
    last = a.get("lastClose")
    if _num(last):
        values.append(("lastClose", last))

    # history
    hist = a.get("history") or []
    if len(hist) != 12:
        err(aid, "history", f"12개여야 합니다 (현재 {len(hist)}개)")
    for i, h in enumerate(hist):
        if not MONTH_RE.match(str(h.get("month", ""))):
            err(aid, f"history[{i}].month", f"YY.MM 형식이어야 합니다 ({h.get('month')!r})")
        if not _num(h.get("close")):
            err(aid, f"history[{i}].close", f"숫자여야 합니다 ({h.get('close')!r})")
        else:
            values.append((f"history[{i}]", h["close"]))
    if hist and _num(last) and _num(hist[-1].get("close")) and abs(hist[-1]["close"] - last) > 1e-9:
        warn(aid, "history", f"마지막 종가({hist[-1]['close']})가 lastClose({last})와 다릅니다")

    # chartData
    cd = a.get("chartData") or []
    if len(cd) != 12:
        err(aid, "chartData", f"12개여야 합니다 (현재 {len(cd)}개)")
    phase_ids = [p.get("phase") for p in (a.get("phases") or [])]
    for i, c in enumerate(cd):
        if not MONTH_RE.match(str(c.get("month", ""))):
            err(aid, f"chartData[{i}].month", f"YY.MM 형식이어야 합니다 ({c.get('month')!r})")
        for k in ("base", "tailA", "tailB"):
            if not _num(c.get(k)):
                err(aid, f"chartData[{i}].{k}", f"숫자여야 합니다 ({c.get(k)!r})")
            else:
                values.append((f"chartData[{i}].{k}", c[k]))
        if c.get("phase") not in phase_ids:
            err(aid, f"chartData[{i}].phase", f"phases 의 phase 값과 같아야 합니다 ({c.get('phase')!r})")

    # scenarios
    sc = a.get("scenarios") or []
    if [s.get("type") for s in sc] != SCENARIO_ORDER:
        err(aid, "scenarios", f"Base → Tail A → Tail B 3개여야 합니다 (현재 {[s.get('type') for s in sc]})")
    probs = [s.get("prob") for s in sc]
    if all(isinstance(p, int) for p in probs) and sum(probs) != 100:
        err(aid, "scenarios.prob", f"확률 합이 100이 아닙니다 ({probs} = {sum(probs)})")
    for s in sc:
        t = s.get("type", "?")
        f = lambda k: f"scenarios[{t}].{k}"
        if not isinstance(s.get("prob"), int):
            err(aid, f("prob"), "정수여야 합니다")
        if s.get("direction") not in DIRECTIONS:
            err(aid, f("direction"), f"up/down/range 중 하나여야 합니다 ({s.get('direction')!r})")
        for k in ("low", "high", "end"):
            if not _num(s.get(k)):
                err(aid, f(k), f"숫자여야 합니다 ({s.get(k)!r})")
            else:
                values.append((f(k), s[k]))
        for k in ("lowDate", "highDate"):
            if not YM_RE.match(str(s.get(k, ""))):
                err(aid, f(k), f"YYYY-MM 또는 YYYY-MM-DD 형식이어야 합니다 ({s.get(k)!r})")
        if not s.get("desc"):
            err(aid, f("desc"), "필수 값이 없습니다")
        elif BANNED.search(s["desc"]):
            warn(aid, f("desc"), f"권유·단정 표현이 있습니다: '{BANNED.search(s['desc']).group(0)}'")
        lo, hi, end = s.get("low"), s.get("high"), s.get("end")
        if all(_num(x) for x in (lo, hi, end)) and not (lo <= end <= hi):
            err(aid, f("low/end/high"), f"low ≤ end ≤ high 가 아닙니다 ({lo} / {end} / {hi})")
        key = SCENARIO_KEY.get(t)
        path = [c.get(key) for c in cd] if key else []
        if path and all(_num(x) for x in path):
            if _num(end) and abs(path[-1] - end) > 1e-6:
                err(aid, f("end"), f"chartData 마지막 달 {key}({path[-1]})와 다릅니다 ({end})")
            if _num(lo) and min(path) < lo - 1e-6:
                warn(aid, f("low"), f"chartData 경로 최저({min(path)})가 low({lo})보다 낮습니다")
            if _num(hi) and max(path) > hi + 1e-6:
                warn(aid, f("high"), f"chartData 경로 최고({max(path)})가 high({hi})보다 높습니다")
        name, d = s.get("name", ""), s.get("direction")
        if ("상승" in name and d == "down") or ("하락" in name and d == "up"):
            err(aid, f("name/direction"), f"이름('{name}')과 direction('{d}')이 반대입니다")
        if _num(end) and _num(last) and d in ("up", "down"):
            moved = end - last
            if (d == "up" and moved < 0) or (d == "down" and moved > 0):
                warn(aid, f("direction"), f"direction '{d}' 인데 끝값({end})이 마지막 종가({last})보다 {'낮습니다' if d == 'up' else '높습니다'}")
    # 꼬리 시나리오 위아래 관계
    if len(sc) == 3 and cd and all(_num(c.get("tailA")) and _num(c.get("tailB")) for c in cd):
        da, db = sc[1].get("direction"), sc[2].get("direction")
        a_above = sum(c["tailA"] > c["tailB"] for c in cd) > len(cd) / 2
        if da == "down" and db == "up" and a_above:
            err(aid, "chartData.tailA/tailB", "Tail A(down)가 Tail B(up)보다 대부분 위에 있습니다 — 경로가 뒤바뀐 것 같습니다")
        if da == "up" and db == "down" and not a_above:
            err(aid, "chartData.tailA/tailB", "Tail A(up)가 Tail B(down)보다 대부분 아래에 있습니다 — 경로가 뒤바뀐 것 같습니다")

    # phases
    ph = a.get("phases") or []
    if len(ph) != 5:
        err(aid, "phases", f"5개여야 합니다 (현재 {len(ph)}개)")
    prev_end = None
    for i, p in enumerate(ph):
        f = lambda k: f"phases[{i}].{k}"
        if p.get("phase") != f"Phase {i + 1}":
            err(aid, f("phase"), f"'Phase {i + 1}' 이어야 합니다 ({p.get('phase')!r})")
        for k in ("title", "summary"):
            if not p.get(k):
                err(aid, f(k), "필수 값이 없습니다")
            elif BANNED.search(p[k]):   # '매도세·청산' 같은 시장 설명은 허용, 직접 권유·단정만 확인
                warn(aid, f(k), f"카드용 문장에 권유·단정 표현이 있습니다: '{BANNED.search(p[k]).group(0)}'")
        if p.get("trend") not in DIRECTIONS:
            err(aid, f("trend"), f"up/down/range 중 하나여야 합니다 ({p.get('trend')!r})")
        if not PERIOD_RE.match(str(p.get("date", ""))):
            err(aid, f("date"), f"'YYYY-MM-DD ~ YYYY-MM-DD' 형식이어야 합니다 ({p.get('date')!r})")
        else:
            s0, s1 = p["date"].split(" ~ ")
            if s0 > s1:
                err(aid, f("date"), "시작일이 끝일보다 늦습니다")
            if prev_end and s0 <= prev_end:
                err(aid, f("date"), f"앞 국면 끝({prev_end})과 겹칩니다")
            prev_end = s1
            if i == 0 and meta.get("forecastStart") and s0 != meta["forecastStart"]:
                warn(aid, f("date"), f"첫 국면 시작({s0})이 meta.forecastStart({meta['forecastStart']})와 다릅니다")
            if i == len(ph) - 1 and meta.get("forecastEnd") and s1 != meta["forecastEnd"]:
                warn(aid, f("date"), f"마지막 국면 끝({s1})이 meta.forecastEnd({meta['forecastEnd']})와 다릅니다")

    # keyPoints
    kp = a.get("keyPoints") or []
    if not 1 <= len(kp) <= 6:
        err(aid, "keyPoints", f"1~6개여야 합니다 (현재 {len(kp)}개)")
    for i, k in enumerate(kp):
        f = lambda x: f"keyPoints[{i}].{x}"
        if k.get("type") not in KEYPOINT_TYPES:
            err(aid, f("type"), f"low/high/turn 중 하나여야 합니다 ({k.get('type')!r})")
        if not k.get("label"):
            err(aid, f("label"), "필수 값이 없습니다")
        elif TRADE_WORDS.search(k["label"]) or BANNED.search(k["label"]):
            warn(aid, f("label"), f"'모델상 저점/고점 구간' 처럼 정보 전달형으로 바꿔 주세요 ('{k['label']}')")
        if not (MONTH_PERIOD_RE.match(str(k.get("date", ""))) or YM_RE.match(str(k.get("date", "")))):
            err(aid, f("date"), f"날짜 또는 기간 형식이어야 합니다 ({k.get('date')!r})")
        if not _num(k.get("value")):
            err(aid, f("value"), f"숫자여야 합니다 ({k.get('value')!r})")
        else:
            values.append((f("value"), k["value"]))

    # 카드용 선택 문장
    for k in ("headline", "weeklyChange"):
        if a.get(k) and BANNED.search(a[k]):
            warn(aid, k, f"권유·단정 표현이 있습니다: '{BANNED.search(a[k]).group(0)}'")

    # 단위 범위 (예: 금리 자리에 채권가격 지수)
    if unit in UNIT_RANGE:
        lo_ok, hi_ok = UNIT_RANGE[unit]
        bad = [(n, v) for n, v in values if not lo_ok <= v <= hi_ok]
        if bad:
            err(aid, "unit", f"단위 {unit} 에 맞지 않는 값 {len(bad)}개 (예: {bad[0][0]} = {bad[0][1]}) — 금리(%)로 통일해 주세요")
    # 같은 자산 안에서 숫자 크기가 크게 다르면 단위가 섞였을 가능성
    if _num(last) and last:
        odd = [(n, v) for n, v in values if v and (v / last > 3 or v / last < 1 / 3)]
        if odd:
            warn(aid, "unit", f"마지막 종가({last})와 자릿수가 크게 다른 값 {len(odd)}개 (예: {odd[0][0]} = {odd[0][1]}) — 단위가 섞였는지 확인해 주세요")


def report(issues: List[Issue]) -> str:
    if not issues:
        return "✅ 문제 없음"
    errs = [i for i in issues if i[0] == "error"]
    warns = [i for i in issues if i[0] == "warn"]
    lines = [f"오류 {len(errs)}건 · 확인 필요 {len(warns)}건"]
    for lvl, a, f, m in errs + warns:
        lines.append(f"{'❌' if lvl == 'error' else '⚠️'} [{a}] {f}: {m}")
    return "\n".join(lines)
