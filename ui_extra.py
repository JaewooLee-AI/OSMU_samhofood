"""설정 화면 · 비용 화면 (일반 사용자용으로 단순하게)"""
import asyncio
import flet as ft

from services import brand_kit as bk
from services import key_store
from services import usage_log
from services import formatting
from services.ai_director import CLAUDE_MODEL

NAVY = "#0F3A68"
AMBER = "#E66A2C"
MUTED = "#64748B"


def card(*controls, padding=20):
    return ft.Container(content=ft.Column(list(controls), spacing=12), padding=padding, bgcolor="#FFFFFF",
                        border_radius=12, border=ft.Border.all(1, "#E2E8F0"))


def h(text, size=16):
    return ft.Text(text, size=size, weight=ft.FontWeight.W_700, color=NAVY)


def note(text):
    return ft.Text(text, size=12, color=MUTED)


# ----------------------------------------------------------------------
class SettingsView:
    def __init__(self, page: ft.Page, toast, on_change):
        self.page, self.toast, self.on_change = page, toast, on_change
        s = bk.load_settings()
        saved = key_store.load_keys()
        self.provider = ft.RadioGroup(
            value=s.ai_provider,
            content=ft.Row([ft.Radio(value="gemini", label="Gemini (글·이미지·음성 모두 가능)"),
                            ft.Radio(value="claude", label="Claude (글 작성 품질 우선)")], wrap=True))
        self.gemini = ft.TextField(label="Gemini API 키", password=True, can_reveal_password=True,
                                   hint_text="저장됨" if saved.get("gemini") else "AIza로 시작하는 키", expand=True)
        self.claude = ft.TextField(label="Claude API 키", password=True, can_reveal_password=True,
                                   hint_text="저장됨" if saved.get("anthropic") else "sk-ant로 시작하는 키", expand=True)
        self.key_status = ft.Text("", size=12)
        self.blog_id = ft.TextField(label="네이버 블로그 아이디", value=s.naver_blog_id, width=320,
                                    hint_text="blog.naver.com/ 뒤에 오는 아이디")
        self.paste_mode = ft.Dropdown(
            label="네이버 서식 붙여넣기 방식", value=s.naver_format_mode, width=320,
            options=[ft.dropdown.Option(k, v) for k, v in formatting.NAVER_FORMAT_MODES.items()])
        self.ai_images = ft.Switch(label="사진이 없는 카드는 AI 이미지로 채우기 (Gemini 키 필요, 이미지마다 비용 발생)",
                                   value=s.generate_ai_images)
        self.control = ft.Column([
            card(h("① 글 쓰는 AI 선택"), self.provider,
                 note("이미지 만들기와 쇼츠 음성은 항상 Gemini를 씁니다. Claude만 쓰면 글과 카드 문구만 Claude가 쓰고, AI 이미지는 만들지 않습니다.")),
            card(h("② API 키 (내 컴퓨터에만 저장됩니다)"),
                 ft.Row([self.gemini]), ft.Row([self.claude]),
                 ft.Row([ft.Button("키 저장하고 연결 확인", icon=ft.Icons.KEY, on_click=self.save_keys),
                         ft.TextButton("저장된 키 지우기", on_click=self.clear_keys)]),
                 self.key_status,
                 note("키는 이 PC의 사용자 폴더(~/.osmu_media_studio)에만 저장되고 화면·로그에 표시되지 않습니다. 사용한 만큼 각 회사에 요금이 청구되며, '비용' 화면에서 확인할 수 있습니다.")),
            card(h("③ 네이버 블로그"), self.blog_id, self.paste_mode,
                 note("‘에디터 서식 우선’은 인용구·구분선·요약 박스를 네이버 에디터 서식으로 바꿔 붙이고, 잘 안 바뀌면 ‘기호로 대체’(❝ ❞ ────)를 고르세요."),
                 self.ai_images,
                 ft.Button("설정 저장", icon=ft.Icons.SAVE, on_click=self.save_settings)),
        ], spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)

    def keys(self):
        typed = {"gemini": self.gemini.value or "", "anthropic": self.claude.value or ""}
        return key_store.resolve(typed)[0]

    async def save_keys(self, e):
        typed = {"gemini": (self.gemini.value or "").strip(), "anthropic": (self.claude.value or "").strip()}
        merged = {**key_store.load_keys(), **{k: v for k, v in typed.items() if v}}
        key_store.save_keys(merged)
        self.gemini.value = self.claude.value = ""
        self.key_status.value = "확인 중…"
        self.key_status.color = MUTED
        self.control.update()
        msgs, ok_all = [], True
        if merged.get("gemini"):
            ok, m = await key_store.test_gemini(merged["gemini"])
            msgs.append(m)
            ok_all = ok_all and ok
        if merged.get("anthropic"):
            ok, m = await key_store.test_anthropic(merged["anthropic"])
            msgs.append(m)
            ok_all = ok_all and ok
        self.key_status.value = " / ".join(msgs) or "저장할 키를 입력해 주세요."
        self.key_status.color = "#10B981" if msgs and ok_all else "#EF4444"
        self.gemini.hint_text = "저장됨" if merged.get("gemini") else self.gemini.hint_text
        self.claude.hint_text = "저장됨" if merged.get("anthropic") else self.claude.hint_text
        self.control.update()
        self.on_change()

    def clear_keys(self, e):
        key_store.delete_keys()
        self.key_status.value = "저장된 키를 지웠습니다."
        self.key_status.color = MUTED
        self.control.update()
        self.on_change()

    def save_settings(self, e):
        s = bk.load_settings()
        s.ai_provider = self.provider.value or "gemini"
        s.naver_blog_id = (self.blog_id.value or "").strip().replace("https://blog.naver.com/", "").strip("/")
        s.naver_format_mode = self.paste_mode.value or "editor"
        s.generate_ai_images = bool(self.ai_images.value)
        bk.save_settings(s)
        self.toast("설정을 저장했습니다.")
        self.on_change()


# ----------------------------------------------------------------------
class CostView:
    """토큰·이미지 사용량 기반 비용 화면 (금액은 설정한 단가·환율로 계산한 추정치)"""

    def __init__(self, page: ft.Page, toast):
        self.page, self.toast = page, toast
        self.body = ft.Column(spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)
        self.control = self.body
        self.price_fields = {}
        self.rate = ft.TextField(label="환율 (1달러=원)", width=170, dense=True)
        self.budget = ft.TextField(label="월 예산 (원, 0=알림 없음)", width=220, dense=True)

    def refresh(self):
        prices = usage_log.load_prices()
        month = usage_log.current_month()
        s = usage_log.summarize(month)
        spent, budget, pct = usage_log.budget_status()
        self.rate.value = f"{prices['usd_krw']:g}"
        self.budget.value = f"{prices['budget_krw']:g}"

        bar = []
        if pct >= 0:
            color = "#10B981" if pct < 70 else ("#F59E0B" if pct < 90 else "#EF4444")
            bar = [ft.ProgressBar(value=min(1.0, pct / 100), color=color, bgcolor="#E2E8F0", height=10),
                   note(f"월 예산 {usage_log.won(budget)} 중 {pct}% 사용")]
        head = card(
            h(f"{month[:4]}년 {int(month[5:])}월 AI 사용 비용 (추정)"),
            ft.Text(usage_log.won(s["krw"]), size=34, weight=ft.FontWeight.W_800, color=AMBER),
            ft.Text(f"호출 {s['calls']}회 · 입력 {s['in']:,} 토큰 · 출력 {s['out']:,} 토큰 · 이미지 {s['images']}장"
                    + (f" · 영상 {s['seconds']:.0f}초" if s["seconds"] else ""), size=13, color="#334155"),
            *bar,
            *([ft.Text("⚠️ 단가가 없는 모델이 있어 금액이 0원으로 보일 수 있어요. 아래 단가표에 요금을 입력하면 계산됩니다: " + ", ".join(s["unpriced"]),
                       size=12, color="#B45309")] if s["unpriced"] else []),
            note("금액은 아래 단가표와 환율로 계산한 추정치입니다. 실제 청구액은 Google·Anthropic 결제 화면에서 확인하세요."),
        )

        rows = []
        for feat, b in sorted(s["by_feature"].items(), key=lambda kv: -kv[1]["usd"]):
            rows.append(ft.DataRow(cells=[
                ft.DataCell(ft.Text(feat)), ft.DataCell(ft.Text(str(b["calls"]))),
                ft.DataCell(ft.Text(f"{b['in']:,}")), ft.DataCell(ft.Text(f"{b['out']:,}")),
                ft.DataCell(ft.Text(str(b["images"]))), ft.DataCell(ft.Text(usage_log.won(b["krw"])))]))
        table = card(h("무엇에 썼나요? (이번 달)"), ft.DataTable(
            columns=[ft.DataColumn(ft.Text(t)) for t in ("기능", "횟수", "입력 토큰", "출력 토큰", "이미지", "금액")],
            rows=rows, heading_row_height=36, data_row_min_height=34, data_row_max_height=34)
            if rows else note("이번 달 사용 기록이 아직 없습니다."))

        # 작업별 비용 (최근 작업 순)
        from services import project_store as ps
        prow = []
        for p in ps.list_projects()[:8]:
            ps_sum = usage_log.summarize(None, p.id)
            if ps_sum["calls"]:
                prow.append(ft.Row([ft.Text(p.title, expand=True, size=13),
                                    ft.Text(usage_log.won(ps_sum["krw"]), size=13, weight=ft.FontWeight.W_600)]))
        per_project = card(h("글(작업)별 비용"), *(prow or [note("작업별 기록이 아직 없습니다.")]))

        # 단가표: 사용된 모델 중 단가가 없는 것은 입력하도록
        self.price_fields = {}
        used = list(s["by_model"].keys())
        price_rows = []
        for key in used:
            p = prices["models"].get(key, {})
            per_image = "per_image" in p
            f_in = ft.TextField(label="입력 $/100만토큰", value=str(p.get("in", "")), width=170, dense=True)
            f_out = ft.TextField(label="출력 $/100만토큰", value=str(p.get("out", "")), width=170, dense=True)
            f_img = ft.TextField(label="이미지 1장 $", value=str(p.get("per_image", "")), width=140, dense=True) if (
                per_image or s["by_model"][key]["images"]) else None
            self.price_fields[key] = (f_in, f_out, f_img)
            tag = "" if p else "  ⚠️ 단가 미등록"
            price_rows.append(ft.Column([ft.Text(key + tag, size=13, weight=ft.FontWeight.W_600,
                                                 color="#B45309" if not p else "#334155"),
                                         ft.Row([f_in, f_out] + ([f_img] if f_img else []), wrap=True)], spacing=6))
        pricing = card(
            h("단가표 · 환율 · 예산"),
            note("사용한 모델의 요금을 직접 입력하면 금액이 계산됩니다. 요금은 회사 공식 요금표를 확인해 입력하세요 (이 앱은 요금을 임의로 채우지 않습니다)."),
            *(price_rows or [note("AI를 사용하면 모델별 단가 입력란이 여기에 나타납니다.")]),
            ft.Row([self.rate, self.budget], wrap=True),
            ft.Button("저장", icon=ft.Icons.SAVE, on_click=self.save),
        )
        self.body.controls = [head, table, per_project, pricing]
        try:
            self.body.update()
        except Exception:
            pass

    @staticmethod
    def _num(field):
        try:
            return float(str(field.value).replace(",", "").strip()) if field is not None and str(field.value).strip() else None
        except ValueError:
            return None

    def save(self, e):
        models = {}
        for key, (f_in, f_out, f_img) in self.price_fields.items():
            vals = {"in": self._num(f_in), "out": self._num(f_out), "per_image": self._num(f_img)}
            vals = {k: v for k, v in vals.items() if v is not None}
            if vals:
                models[key] = vals
        usage_log.save_prices(self._num(self.rate), self._num(self.budget), models)
        self.toast("단가를 저장했습니다.")
        self.refresh()
