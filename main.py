"""삼호푸드 OSMU 스튜디오 — 블로그 글 하나로 네이버 블로그 · 카드뉴스 · 쇼츠까지.

일반 사용자용 단계형 화면:
  ① 알릴 내용 → ② 블로그 글 → ③ 카드뉴스 → ④ 쇼츠 → ⑤ 네이버 게시
실행: ./run.sh  (데스크톱 앱)   또는  FLET_VIEW=web ./run.sh  (브라우저)
"""
import os
import sys
import shutil
import logging
import asyncio
import secrets
import threading
import subprocess
import flet as ft

from models.schema import UserRequestPayload
from services import brand_kit as bk
from services import blog_writer as bw
from services import project_store as ps
from services import usage_log
from services.ai_director import AIDirectorService
from services.card_renderer import CardNewsRendererService
from services.shorts_renderer import ShortsRendererService, ShortsOptions
from services.media_assets import MediaLibrary
from services.image_generator import AIImageGenerator
from services import shorts_from_cards
from services import naver_publish
from services.paths import IS_PACKAGED, ASSETS_DIR, FONTS_DIR, EXPORTS_DIR, UPLOAD_DIR, LOG_DIR
from ui_extra import SettingsView, CostView, card, h, note, NAVY, AMBER, MUTED

os.environ.setdefault("FLET_SECRET_KEY", secrets.token_hex(16))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.FileHandler(os.path.join(LOG_DIR, "app.log"), encoding="utf-8"), logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("samhofood")

TARGETS = ["(고르지 않음)", "육아·맞벌이 가정", "캠핑·아웃도어족", "저염·건강식 선호층 (시니어·1인 가구)",
           "외식업·생선구이 전문점 사장님", "공공기관·가치소비자 (ESG)"]
STEPS = ["알릴 내용", "블로그 글", "카드뉴스", "쇼츠", "네이버 게시"]
IMG_EXT = ["jpg", "jpeg", "png", "webp", "heic", "gif"]


def open_path(path: str):
    if sys.platform == "win32":
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


async def main(page: ft.Page):
    page.title = "삼호푸드 OSMU 스튜디오"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = "#F4F7FA"
    page.padding = 0
    page.window.width, page.window.height = 1180, 900
    page.window.min_width, page.window.min_height = 900, 700
    fonts = {}
    if os.path.exists(os.path.join(FONTS_DIR, "Pretendard-Regular.otf")):
        fonts["Pretendard"] = "/fonts/Pretendard-Regular.otf"
        page.fonts = fonts
        page.theme = ft.Theme(font_family="Pretendard")

    director = AIDirectorService()
    library = MediaLibrary(EXPORTS_DIR)
    imagegen = AIImageGenerator(library)
    card_renderer = CardNewsRendererService(export_dir=os.path.join(EXPORTS_DIR, "cards"))
    shorts_renderer = ShortsRendererService(export_dir=os.path.join(EXPORTS_DIR, "shorts"))

    S = {"step": 0, "project": None, "assets": [], "draft": None, "sb": None, "cards": [], "video": "",
         "busy": False, "form": {}, "image_mode": "mine", "naver_msg": "", "naver_running": False}
    picker = ft.FilePicker()
    page.services.append(picker)

    def toast(msg, color="#10B981"):
        page.show_dialog(ft.SnackBar(content=ft.Text(msg), bgcolor=color))

    def settings():
        return bk.load_settings()

    def keys():
        return settings_view.keys()

    def api_key_for(provider):
        return keys().get("anthropic" if provider == "claude" else "gemini")

    # ------------------------------------------------------------------
    # 공통 UI 조각
    # ------------------------------------------------------------------
    stepper = ft.Row(spacing=6, wrap=True)
    content = ft.Column(spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)
    status = ft.Text("", size=13, color=MUTED)
    busy_ring = ft.ProgressRing(width=18, height=18, stroke_width=3, visible=False)
    cost_chip = ft.Text("", size=12, color="#FFFFFF")

    def set_busy(on, msg=""):
        S["busy"] = on
        busy_ring.visible = on
        status.value = msg
        for c in (busy_ring, status):
            try:
                c.update()
            except Exception:
                pass

    def goto(step):
        S["step"] = step
        render()

    def render_stepper():
        items = []
        for i, name in enumerate(STEPS):
            active, done = i == S["step"], i < S["step"]
            items.append(ft.Container(
                content=ft.Text(f"{i + 1}. {name}", size=13, weight=ft.FontWeight.W_700 if active else ft.FontWeight.W_500,
                                color="#FFFFFF" if active else (NAVY if done else MUTED)),
                padding=ft.Padding.symmetric(horizontal=14, vertical=8), border_radius=20,
                bgcolor=NAVY if active else ("#DBE7F3" if done else "#FFFFFF"),
                border=ft.Border.all(1, "#CBD5E1"), on_click=lambda e, i=i: goto(i) if not S["busy"] else None))
        stepper.controls = items

    def update_cost_chip():
        spent, budget, pct = usage_log.budget_status()
        sm = usage_log.summarize(usage_log.current_month())
        cost_chip.value = (f"이번 달 AI 비용 {usage_log.won(spent)}" + (f" ({pct}%)" if pct >= 0 else "")
                           + (f" · 토큰 {sm['in'] + sm['out']:,}" if sm["calls"] else "")
                           + (" · 단가 미등록 있음" if sm["unpriced"] else ""))
        try:
            cost_chip.update()
        except Exception:
            pass

    def nav_row(back=None, nxt=None, nxt_label="다음", extra=None):
        controls = []
        if back is not None:
            controls.append(ft.OutlinedButton("이전", icon=ft.Icons.ARROW_BACK, on_click=lambda e: goto(back)))
        controls += extra or []
        if nxt is not None:
            controls.append(ft.Button(nxt_label, icon=ft.Icons.ARROW_FORWARD, on_click=nxt,
                                      style=ft.ButtonStyle(bgcolor=NAVY, color="#FFFFFF")))
        return ft.Row(controls, spacing=10, wrap=True)

    def primary(label, handler, icon=None):
        return ft.Button(label, icon=icon, on_click=handler, style=ft.ButtonStyle(bgcolor=AMBER, color="#FFFFFF"))

    # ------------------------------------------------------------------
    # 작업 저장
    # ------------------------------------------------------------------
    def ensure_project(title=""):
        if S["project"] is None:
            p = ps.new_project(title or "새 콘텐츠")
            S["project"] = p
        p = S["project"]
        usage_log.set_project(p.id)
        card_renderer.export_dir = p.sub("cards")
        shorts_renderer.export_dir = p.sub("shorts")
        return p

    def persist():
        p = S["project"]
        if p is None:
            return
        f, d = S["form"], S["draft"]
        p.brand_id = f.get("brand_id", p.brand_id)
        p.inputs.raw_text = bw.plain_text(d.body) if d else ""
        p.inputs.attachments = list(S["assets"])
        p.blog_request = dict(f)
        p.image_mode = S["image_mode"]
        if d:
            p.blog_title, p.blog_body, p.blog_hashtags = d.title, d.body, d.hashtags
        if S["sb"] is not None:
            p.storyboard = S["sb"]
        if p.status == "draft" and S["cards"]:
            p.status = "cards"
        ps.save(p)

    # ------------------------------------------------------------------
    # 1단계: 알릴 내용
    # ------------------------------------------------------------------
    def step_input():
        f = S["form"]
        kits = bk.list_kits()
        brand = ft.Dropdown(label="어느 브랜드로 쓸까요?", value=f.get("brand_id") or settings().active_brand_id,
                            options=[ft.dropdown.Option(k.id, f"{k.brand_name} — {k.tagline}") for k in kits], width=520)
        audience = ft.Dropdown(label="누구에게 알리나요? (타깃)", value=f.get("audience") or TARGETS[0],
                               options=[ft.dropdown.Option(t) for t in TARGETS], width=520)
        product = ft.TextField(label="어떤 상품을 알리나요?", value=f.get("product", ""), width=520,
                               hint_text="예: 특대 간고등어 2.3kg 5미")
        news = ft.TextField(label="이번 주 소식 (프로모션·나눔 이야기 등)", value=f.get("news", ""), multiline=True,
                            min_lines=3, max_lines=6, width=520)
        memo = ft.TextField(label="더 하고 싶은 말 (선택)", value=f.get("memo", ""), multiline=True, min_lines=2,
                            max_lines=5, width=520)
        length = ft.Dropdown(label="글 길이", value=f.get("length", "보통"), width=200,
                             options=[ft.dropdown.Option(x) for x in bw.LENGTHS])
        thumbs = ft.Row(spacing=8, wrap=True)
        upload_msg = ft.Text("", size=12, color=MUTED)

        def draw_thumbs():
            thumbs.controls = []
            for a in S["assets"]:
                thumbs.controls.append(ft.Stack([
                    ft.Container(ft.Image(src=a.keyframes[0] if a.keyframes else a.path, width=96, height=96, fit=ft.BoxFit.COVER),
                                 border_radius=8, tooltip=f"{a.id} · {a.original_name}"),
                    ft.Container(ft.Text(a.id, size=10, color="#FFFFFF"), bgcolor="#000000AA", padding=3, left=4, top=4, border_radius=4),
                    ft.IconButton(ft.Icons.CLOSE, icon_size=14, right=0, top=0, bgcolor="#FFFFFFCC",
                                  on_click=lambda e, a=a: remove_asset(a)),
                ], width=96, height=96))
            try:
                thumbs.update()
            except Exception:
                pass

        def remove_asset(a):
            S["assets"] = [x for x in S["assets"] if x.id != a.id]
            library.remove(a)
            draw_thumbs()

        async def pick(e):
            files = await picker.pick_files(dialog_title="사진 고르기 (3~5장 권장)", allow_multiple=True,
                                            file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=IMG_EXT)
            if not files:
                return
            if not all(x.path for x in files):
                upload_msg.value = "브라우저 모드에서는 사진 올리기를 지원하지 않습니다. 데스크톱 앱(./run.sh)으로 실행해 주세요."
                upload_msg.update()
                return
            for x in files:
                aid = MediaLibrary.next_id(S["assets"], "A")
                try:
                    S["assets"].append(await library.ingest(x.path, aid, x.name))
                except Exception as ex:  # noqa: BLE001
                    upload_msg.value = f"{x.name}: 사진을 읽지 못했습니다 ({ex})"
            upload_msg.update()
            draw_thumbs()

        draw_thumbs()

        async def go(e):
            if S["busy"]:
                return
            if not (product.value or "").strip() and not (news.value or "").strip() and not (memo.value or "").strip():
                toast("상품이나 소식 중 하나는 적어 주세요.", "#F59E0B")
                return
            S["form"] = {"brand_id": brand.value, "audience": "" if audience.value == TARGETS[0] else audience.value,
                         "product": (product.value or "").strip(), "news": (news.value or "").strip(),
                         "memo": (memo.value or "").strip(), "length": length.value}
            await make_blog()

        return [
            card(h("① 이번에 무엇을 알릴까요?"),
                 note("아래 세 가지만 적으면 블로그 글을 만들고, 같은 내용으로 카드뉴스와 쇼츠까지 이어서 만들 수 있어요."),
                 brand, audience, product, news, memo, length),
            card(h("사진 올리기 (선택, 3~5장 권장)"),
                 note("올린 사진은 블로그와 카드뉴스에 쓰입니다. 원물 컷(투명창 파우치), 조리 후 컷, 가시 제거 순살 컷이 좋아요."),
                 ft.Row([ft.OutlinedButton("사진 고르기", icon=ft.Icons.ADD_PHOTO_ALTERNATE, on_click=pick), upload_msg]), thumbs),
            ft.Row([primary("블로그 글 만들기", go, ft.Icons.AUTO_AWESOME)]),
        ]

    async def make_blog():
        f = S["form"]
        p = ensure_project(f.get("product") or f.get("news")[:20] or "새 콘텐츠")
        p.title = (f.get("product") or f.get("news") or "새 콘텐츠")[:30]
        provider = settings().ai_provider
        req = bw.BlogRequest(brand_id=f["brand_id"], audience=f["audience"], product=f["product"], news=f["news"],
                             memo=f["memo"], length=f["length"], provider=provider, assets=list(S["assets"]))
        set_busy(True, f"{'Claude' if provider == 'claude' else 'Gemini'}가 블로그 글을 쓰는 중입니다… (30초 정도)")
        try:
            S["draft"] = await bw.generate(req, keys())
            S["sb"], S["cards"], S["video"] = None, [], ""   # 새 글이면 이전 카드·쇼츠는 무효
            p.log(f"블로그 글 생성 ({S['draft'].engine or provider})")
            persist()
            set_busy(False)
            update_cost_chip()
            goto(1)
        except Exception as ex:  # noqa: BLE001
            logger.exception("블로그 생성 실패")
            set_busy(False, f"글을 만들지 못했습니다: {ex}")

    # ------------------------------------------------------------------
    # 2단계: 블로그 글
    # ------------------------------------------------------------------
    def step_blog():
        d = S["draft"]
        if d is None:
            return [card(h("아직 글이 없어요"), note("먼저 1단계에서 내용을 입력하고 [블로그 글 만들기]를 눌러 주세요."),
                         ft.Button("1단계로", on_click=lambda e: goto(0)))]
        kit = bk.load_kit(S["form"].get("brand_id") or settings().active_brand_id)
        title = ft.TextField(label="제목", value=d.title, text_size=16, width=820)
        body = ft.TextField(label="본문 (직접 고쳐 쓸 수 있어요)", value=d.body, multiline=True, min_lines=18, max_lines=40, width=820)
        tags = ft.TextField(label="해시태그", value=" ".join(d.hashtags), width=820)
        warn_box = ft.Column(spacing=4)

        def sync():
            d.title, d.body = title.value or "", body.value or ""
            d.hashtags = [t if t.startswith("#") else f"#{t}" for t in (tags.value or "").split() if t]
            persist()

        def check(e=None):
            sync()
            ws = bw.review(f"{d.title}\n{d.body}", kit)
            warn_box.controls = ([ft.Text("⚠️ 게시 전에 확인해 주세요", weight=ft.FontWeight.W_700, color="#B45309")]
                                 + [ft.Text("• " + w, size=12, color="#92400E") for w in ws]) if ws else \
                [ft.Text("✅ 눈에 띄는 주의 표현이 없습니다. (법률 검토를 대신하지는 않아요)", size=12, color="#047857")]
            warn_box.update()

        async def regen(e):
            sync()
            await make_blog()

        def to_cards(e):
            sync()
            goto(2)

        legend = ", ".join(f"[사진:{a.id}]={a.original_name}" for a in S["assets"]) or "첨부한 사진이 없습니다."
        return [
            card(h("② 블로그 글"), *[ft.Text("• " + n, size=12, color="#B45309") for n in d.notes], title, body, tags,
                 note(f"## 로 시작하는 줄은 소제목이에요. 사진 위치는 [사진:A1] 줄로 표시됩니다 — 줄을 옮기면 사진 위치가 바뀌어요.  ({legend})"),
                 ft.Row([ft.OutlinedButton("표현 점검", icon=ft.Icons.FACT_CHECK, on_click=check),
                         ft.OutlinedButton("다시 만들기", icon=ft.Icons.REFRESH, on_click=regen)]), warn_box),
            nav_row(back=0, nxt=to_cards, nxt_label="카드뉴스 만들기",
                    extra=[ft.TextButton("카드뉴스 없이 게시로", on_click=lambda e: (sync(), goto(4)))]),
        ]

    # ------------------------------------------------------------------
    # 3단계: 카드뉴스
    # ------------------------------------------------------------------
    def step_cards():
        d = S["draft"]
        if d is None:
            return [card(h("먼저 블로그 글이 필요해요"), ft.Button("1단계로", on_click=lambda e: goto(0)))]
        count = ft.Dropdown(label="카드 장수", value=str(S["form"].get("card_count", settings().default_card_count)), width=140,
                            options=[ft.dropdown.Option(str(n), f"{n}장") for n in (4, 5, 6, 7)])
        fmt = ft.Dropdown(label="구성", value=S["form"].get("card_format", "info_summary"), width=260,
                          options=[ft.dropdown.Option("info_summary", "핵심 정리 (번호 카드)"),
                                   ft.dropdown.Option("case_study", "이야기형 (문제→해결→결과)"),
                                   ft.dropdown.Option("comparison", "비교형 (A vs B)")])
        thumbs = ft.Row(spacing=10, wrap=True)

        def draw():
            thumbs.controls = [ft.Container(ft.Image(src=pth, width=230, fit=ft.BoxFit.CONTAIN), border_radius=8,
                                            border=ft.Border.all(1, "#CBD5E1")) for pth in S["cards"]]
            try:
                thumbs.update()
            except Exception:
                pass

        async def make(e):
            if S["busy"]:
                return
            S["form"]["card_count"], S["form"]["card_format"] = int(count.value), fmt.value
            await make_cards(int(count.value), fmt.value, draw)

        draw()
        has = bool(S["cards"])
        return [
            card(h("③ 카드뉴스 (인스타그램용 1080×1350)"),
                 note("블로그 글 내용으로 카드뉴스를 만들어요. 올린 사진이 있으면 카드 배경으로 쓰고, 없으면 AI 이미지 또는 브랜드 배경을 써요."),
                 ft.Row([count, fmt, primary("다시 만들기" if has else "카드뉴스 만들기", make, ft.Icons.DASHBOARD_CUSTOMIZE)], wrap=True),
                 thumbs,
                 ft.OutlinedButton("카드 폴더 열기", icon=ft.Icons.FOLDER_OPEN,
                                   on_click=lambda e: open_path(S["project"].sub("cards"))) if has else ft.Container()),
            nav_row(back=1, nxt=lambda e: goto(3), nxt_label="쇼츠 만들기",
                    extra=[ft.TextButton("쇼츠 없이 게시로", on_click=lambda e: goto(4))]),
        ]

    async def make_cards(n, fmt, draw):
        p = ensure_project()
        d, f, st = S["draft"], S["form"], settings()
        provider = st.ai_provider
        if provider == "claude" and not keys().get("anthropic") and keys().get("gemini"):
            provider = "gemini"
        payload = UserRequestPayload(
            raw_text=f"{d.title}\n{bw.plain_text(d.body)}", brand_id=f["brand_id"], card_format=fmt, card_count=n,
            ai_provider=provider, gemini_api_key=keys().get("gemini"), anthropic_api_key=keys().get("anthropic"),
            generate_ai_images=st.generate_ai_images, assets=list(S["assets"]), anonymize=False)
        set_busy(True, "카드뉴스 문구를 기획하는 중…")
        try:
            mark = usage_log.mark()
            sb = await director.generate_storyboard(payload)
            usage_log.claim(mark, p.id)
            if st.generate_ai_images and keys().get("gemini"):
                async def prog(msg):
                    set_busy(True, msg)
                await imagegen.fill_missing(sb, keys().get("gemini"), True, prog, cards=True, shorts=False)
            sb.images_ready = True
            set_busy(True, f"카드 {len(sb.card_slides)}장을 그리는 중…")
            S["sb"] = sb
            S["cards"] = await card_renderer.render_all_slides(sb)
            S["video"] = ""
            p.log(f"카드뉴스 {len(S['cards'])}장 제작")
            persist()
            set_busy(False, f"카드 {len(S['cards'])}장이 완성됐어요! 카드 이미지는 네이버 블로그에도 넣을 수 있어요.")
            update_cost_chip()
            draw()
        except Exception as ex:  # noqa: BLE001
            logger.exception("카드뉴스 실패")
            set_busy(False, f"카드뉴스를 만들지 못했습니다: {ex}")

    # ------------------------------------------------------------------
    # 4단계: 쇼츠
    # ------------------------------------------------------------------
    def step_shorts():
        if S["sb"] is None or not S["cards"]:
            return [card(h("카드뉴스를 먼저 만들어 주세요"), note("쇼츠는 카드뉴스 이미지와 문구를 그대로 이어 붙여 만들어요."),
                         ft.Button("카드뉴스 단계로", on_click=lambda e: goto(2))),
                    nav_row(back=2, nxt=lambda e: goto(4), nxt_label="게시로 건너뛰기")]
        secs = ft.Dropdown(label="길이", value="30", width=140, options=[ft.dropdown.Option(str(n), f"약 {n}초") for n in (15, 30, 45, 60)])
        style = ft.Dropdown(label="자막", value="kinetic", width=260,
                            options=[ft.dropdown.Option("kinetic", "움직이는 글자 (모션 그래픽)"), ft.dropdown.Option("basic", "기본 자막")])
        result = ft.Row(spacing=10)
        ffmpeg_ok = shutil.which("ffmpeg") is not None

        def draw():
            result.controls = ([ft.Text("✅ " + os.path.basename(S["video"]), size=13),
                                ft.Button("영상 열기", icon=ft.Icons.PLAY_CIRCLE, on_click=lambda e: open_path(S["video"])),
                                ft.OutlinedButton("폴더 열기", icon=ft.Icons.FOLDER_OPEN,
                                                  on_click=lambda e: open_path(os.path.dirname(S["video"])))] if S["video"] else [])
            try:
                result.update()
            except Exception:
                pass

        async def make(e):
            if S["busy"]:
                return
            await make_shorts(int(secs.value), style.value, draw)

        draw()
        return [
            card(h("④ 쇼츠 · 릴스 (1080×1920 세로 영상)"),
                 note("카드뉴스 사진과 문구에 AI 목소리와 움직이는 자막을 입혀 영상으로 만들어요."),
                 ft.Text("⚠️ 영상 만들기에 필요한 ffmpeg가 설치되어 있지 않습니다. 설치 방법은 README를 확인해 주세요.",
                         color="#B45309", size=12) if not ffmpeg_ok else ft.Container(),
                 ft.Row([secs, style, primary("쇼츠 만들기" if not S["video"] else "다시 만들기", make, ft.Icons.MOVIE_CREATION)], wrap=True),
                 result),
            nav_row(back=2, nxt=lambda e: goto(4), nxt_label="네이버 게시로"),
        ]

    async def make_shorts(seconds, caption_style, draw):
        p, sb, st = S["project"], S["sb"], settings()
        provider = st.ai_provider
        if provider == "claude" and not keys().get("anthropic") and keys().get("gemini"):
            provider = "gemini"
        kit = bk.load_kit(sb.brand_id)
        set_busy(True, "카드 내용을 쇼츠 대본으로 바꾸는 중…")
        try:
            await shorts_from_cards.convert(sb, kit, provider, api_key_for(provider), seconds, source_text=p.inputs.raw_text)

            async def prog(pct, msg):
                set_busy(True, f"[{int(pct * 100)}%] {msg}")
            opts = ShortsOptions(caption_style=caption_style, voice="auto", gemini_api_key=keys().get("gemini"))
            S["video"] = await shorts_renderer.render_shorts_video(sb, prog, opts)
            p.status = "shorts"
            p.log("쇼츠 제작")
            persist()
            warn = shorts_renderer.last_warnings
            set_busy(False, "쇼츠 완성!" + (" (참고: " + " / ".join(warn) + ")" if warn else ""))
            update_cost_chip()
            draw()
        except Exception as ex:  # noqa: BLE001
            logger.exception("쇼츠 실패")
            set_busy(False, f"쇼츠를 만들지 못했습니다: {ex}")

    # ------------------------------------------------------------------
    # 5단계: 네이버 게시
    # ------------------------------------------------------------------
    def step_publish():
        d = S["draft"]
        if d is None:
            return [card(h("먼저 블로그 글이 필요해요"), ft.Button("1단계로", on_click=lambda e: goto(0)))]
        has_photos, has_cards = bool(S["assets"]), bool(S["cards"])
        mode = S["image_mode"]
        if mode == "mine" and not has_photos and has_cards:
            mode = "cards"
        if mode == "cards" and not has_cards:
            mode = "mine"
        S["image_mode"] = mode
        opts = [ft.Radio(value="mine", label=bw.IMAGE_MODES["mine"] + ("" if has_photos else " (올린 사진 없음)"), disabled=not has_photos),
                ft.Radio(value="cards", label=bw.IMAGE_MODES["cards"] + ("" if has_cards else " (카드뉴스를 먼저 만들어 주세요)"), disabled=not has_cards),
                ft.Radio(value="both", label=bw.IMAGE_MODES["both"], disabled=not (has_photos and has_cards))]
        radio = ft.RadioGroup(value=mode, content=ft.Column(opts, spacing=2), on_change=lambda e: S.__setitem__("image_mode", e.control.value))
        blog_id = ft.TextField(label="네이버 블로그 아이디", value=settings().naver_blog_id, width=300)
        login_state = ft.Text("", size=12)
        log = ft.Text(S["naver_msg"], size=13, selectable=True)

        def refresh_login():
            ok = naver_publish.naver_session_exists()
            login_state.value = "🔑 로그인 정보가 저장되어 있어요" if ok else "아직 네이버 로그인 전이에요"
            login_state.color = "#047857" if ok else "#B45309"

        refresh_login()

        def on_status(msg):
            S["naver_msg"] = msg
            log.value = msg
            try:
                log.update()
            except Exception:
                pass

        async def login(e):
            on_status("Chrome 창이 열립니다. 네이버에 로그인하면 자동으로 닫혀요 (2분 안에).")
            res = await asyncio.to_thread(naver_publish.open_naver_login_session, (blog_id.value or "").strip())
            on_status(res["message"])
            refresh_login()
            login_state.update()

        async def go(e):
            if S["naver_running"]:
                return
            bid = (blog_id.value or "").strip().replace("https://blog.naver.com/", "").strip("/")
            if not bid:
                toast("네이버 블로그 아이디를 입력해 주세요.", "#F59E0B")
                return
            s = settings()
            s.naver_blog_id = bid
            bk.save_settings(s)
            kit = bk.load_kit(S["form"].get("brand_id") or s.active_brand_id)
            body = bw.arrange_images(d.body, S["image_mode"], [a.id for a in S["assets"]], len(S["cards"]))
            photos = {a.id: a.path for a in S["assets"]}
            segs = bw.to_segments(d.title, body, d.hashtags, photos, S["cards"])
            ws = bw.review(f"{d.title}\n{d.body}", kit)
            if ws:
                on_status("참고: 주의 표현이 있어요 — " + " / ".join(w.split(" — ")[0] for w in ws[:3]) + " (2단계의 [표현 점검]에서 확인)")
            S["naver_running"] = True
            persist()
            on_status("네이버 글쓰기 창을 여는 중… Chrome 창이 뜨면 그대로 두세요.")

            def work():
                try:
                    naver_publish.publish(bid, d.title, segs, on_status)
                finally:
                    S["naver_running"] = False
            threading.Thread(target=work, daemon=True).start()

        return [
            card(h("⑤ 네이버 블로그에 올리기"),
                 ft.Text("블로그에 어떤 이미지를 넣을까요?", weight=ft.FontWeight.W_600), radio,
                 note("‘카드뉴스 이미지’는 3단계에서 만든 카드가 글 사이사이에 들어가요. ‘둘 다’는 내 사진은 제자리에, 카드뉴스는 글 끝에 모아 넣어요.")),
            card(h("네이버 연결"), ft.Row([blog_id, ft.OutlinedButton("네이버 로그인", icon=ft.Icons.LOGIN, on_click=login), login_state], wrap=True),
                 note("로그인은 직접 하는 Chrome 창에서만 이루어지고, 비밀번호는 저장하지 않아요. 로그인 상태만 이 PC에 저장됩니다.")),
            card(primary("네이버에 붙여넣기", go, ft.Icons.PUBLISH),
                 note("제목·본문·이미지가 네이버 글쓰기 창에 자동으로 들어가요. 내용을 확인하고 [발행]은 직접 눌러 주세요. 창은 직접 닫을 때까지 열려 있어요."),
                 log),
            nav_row(back=3),
        ]

    # ------------------------------------------------------------------
    # 내 작업 · 설정 · 비용
    # ------------------------------------------------------------------
    def open_project(pid):
        p = ps.load(pid)
        S["project"] = p
        S["assets"] = list(p.inputs.attachments)
        S["form"] = dict(p.blog_request or {"brand_id": p.brand_id})
        S["image_mode"] = p.image_mode or "mine"
        S["draft"] = bw.BlogDraft(title=p.blog_title, body=p.blog_body, hashtags=list(p.blog_hashtags)) if p.blog_body else None
        S["sb"] = p.storyboard
        cdir = os.path.join(p.folder, "cards")
        S["cards"] = sorted(os.path.join(cdir, x) for x in os.listdir(cdir) if x.lower().endswith(".png")) if os.path.isdir(cdir) else []
        sdir = os.path.join(p.folder, "shorts")
        mp4 = sorted(os.path.join(sdir, x) for x in os.listdir(sdir) if x.lower().endswith(".mp4")) if os.path.isdir(sdir) else []
        S["video"] = mp4[-1] if mp4 else ""
        usage_log.set_project(p.id)
        card_renderer.export_dir = p.sub("cards")
        shorts_renderer.export_dir = p.sub("shorts")
        show_tab("make")
        goto(1 if S["draft"] else 0)

    def new_work():
        S.update({"project": None, "assets": [], "draft": None, "sb": None, "cards": [], "video": "", "form": {}, "naver_msg": ""})
        show_tab("make")
        goto(0)

    def page_works():
        rows = []
        for p in ps.list_projects():
            rows.append(ft.Container(
                content=ft.Row([ft.Column([ft.Text(p.title, size=15, weight=ft.FontWeight.W_700, color=NAVY),
                                           ft.Text(f"{p.status_label} · {p.updated_at}", size=12, color=MUTED)], spacing=2, expand=True),
                                ft.OutlinedButton("열기", on_click=lambda e, pid=p.id: open_project(pid)),
                                ft.IconButton(ft.Icons.FOLDER_OPEN, tooltip="폴더 열기", on_click=lambda e, pth=p.folder: open_path(pth))]),
                padding=14, bgcolor="#FFFFFF", border_radius=10, border=ft.Border.all(1, "#E2E8F0")))
        return [h("내 작업", 20)] + (rows or [note("아직 만든 작업이 없어요. [새 콘텐츠]로 시작해 보세요.")])

    settings_view = SettingsView(page, toast, lambda: (update_cost_chip(), None))
    cost_view = CostView(page, toast)
    tab = {"name": "make"}
    tab_row = ft.Row(spacing=4)

    def show_tab(name):
        tab["name"] = name
        render()

    def render():
        tab_row.controls = [ft.TextButton(label, on_click=lambda e, n=n: show_tab(n),
                                          style=ft.ButtonStyle(color="#FFFFFF" if tab["name"] == n else "#A9C1DA"))
                            for n, label in (("make", "콘텐츠 만들기"), ("works", "내 작업"), ("cost", "비용"), ("settings", "설정"))]
        if tab["name"] == "make":
            render_stepper()
            builders = [step_input, step_blog, step_cards, step_shorts, step_publish]
            content.controls = [stepper] + builders[S["step"]]()
        elif tab["name"] == "works":
            content.controls = page_works()
        elif tab["name"] == "cost":
            update_cost_chip()
            content.controls = [cost_view.control]
        else:
            content.controls = [settings_view.control]
        try:
            page.update()
        except Exception:
            pass
        if tab["name"] == "cost":
            cost_view.refresh()

    header = ft.Container(
        content=ft.Row([
            ft.Row([ft.Text("삼호푸드 OSMU 스튜디오", size=19, weight=ft.FontWeight.W_800, color="#FFFFFF"), tab_row], spacing=18),
            ft.Row([cost_chip, ft.Button("새 콘텐츠", icon=ft.Icons.ADD, on_click=lambda e: new_work(),
                                         style=ft.ButtonStyle(bgcolor=AMBER, color="#FFFFFF"))], spacing=12)],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True),
        bgcolor=NAVY, padding=ft.Padding.symmetric(horizontal=24, vertical=12))
    footer = ft.Container(ft.Row([busy_ring, status], spacing=10), padding=ft.Padding.symmetric(horizontal=24, vertical=8),
                          bgcolor="#FFFFFF", border=ft.Border.only(top=ft.BorderSide(1, "#E2E8F0")))
    page.add(ft.Column([header, ft.Container(content, padding=ft.Padding.symmetric(horizontal=24, vertical=16), expand=True), footer],
                       spacing=0, expand=True))
    update_cost_chip()
    render()

    k = keys()
    if not k:
        status.value = "처음이시군요! 위쪽 [설정]에서 Gemini 또는 Claude API 키를 먼저 등록해 주세요. (키가 없어도 기본 초안은 만들 수 있어요)"
        status.update()


def create_web_app():
    return ft.run(main, export_asgi_app=True, assets_dir=ASSETS_DIR, upload_dir=UPLOAD_DIR)


if __name__ == "__main__":
    if IS_PACKAGED:
        ft.run(main)   # flet build로 만든 설치형: Flet 템플릿이 창을 띄움
    elif os.environ.get("FLET_VIEW", "app").lower() == "web":
        import uvicorn
        uvicorn.run(create_web_app(), host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8550")), log_level="info")
    else:
        ft.run(main, view=ft.AppView.FLET_APP, assets_dir=ASSETS_DIR, upload_dir=UPLOAD_DIR)
