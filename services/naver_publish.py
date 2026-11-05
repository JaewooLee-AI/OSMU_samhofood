"""네이버 블로그 반자동 게시 (Playwright).

OSMU_cjc의 검증된 게시 자동화를 이 앱에 맞게 옮긴 것입니다.
- 로그인: 사용자가 보이는 Chrome 창에서 직접 로그인하면 세션만 저장합니다 (비밀번호는 저장하지 않음).
- 게시: 제목·본문·사진을 스마트에디터 ONE에 차례로 붙여넣고, [발행]은 사용자가 직접 누릅니다.
  (네이버 봇 탐지와 최종 확인을 위해 창을 숨기지 않습니다.)
- 세션 파일(naver_state.json)은 로그인 권한과 같으므로 데이터 폴더 한 곳에만 둡니다.
"""
from __future__ import annotations

import re
import sys
import time
import threading
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.paths import DATA_DIR
from services.clipboard_utils import copy_html_to_clipboard

NAVER_STATE_FILE = Path(DATA_DIR) / "naver_state.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HUMAN_DELAY = (0.15, 0.35)


# Publishing runs as browser automation, so its failures arrive as Playwright
# and OS exceptions — "net::ERR_NAME_NOT_RESOLVED", multi-line "Call log:"
# dumps, "Target page, context or browser has been closed". Those are useless
# to the marketer using this page, but they're the only diagnostic if the
# automation breaks. `format_publish_error` therefore stores a plain sentence
# for the UI with the raw text kept after DETAIL_SEPARATOR, and the view shows
# only the sentence unless someone opens the details.
DETAIL_SEPARATOR = "\n\n[기술 상세]\n"

_ERROR_HINTS = (
    (("팝업", "이어서 작성"), "네이버가 '이어서 작성하시겠습니까?' 같은 팝업을 띄웠는데 자동으로 닫지 못했습니다. [다시 게시]로 한 번 더 시도해주세요. 계속 실패하면 네이버 블로그에 직접 로그인해 임시저장된 글이 있는지 확인해주세요."),
    (("nidlogin", "login", "로그인"), "네이버 로그인이 풀렸습니다. 위에서 다시 로그인한 뒤 게시해주세요."),
    (("timeout", "timed out"), "네이버 편집기가 제때 열리지 않았습니다. 잠시 후 다시 시도해주세요."),
    (("net::err", "econnrefused", "dns", "connection"), "네이버에 연결하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도해주세요."),
    (("has been closed", "is closed", "closed"), "게시 도중 Chrome 창이 닫혔습니다. 창을 닫지 말고 다시 시도해주세요."),
    (("executable doesn't exist", "browsertype.launch", "chromium"), "Chrome을 실행하지 못했습니다. Chrome이 설치되어 있는지 확인해주세요."),
)


def format_publish_error(exc: Exception) -> str:
    """Plain-Korean sentence for the UI, raw exception kept for diagnosis."""
    raw = str(exc).strip()
    haystack = raw.lower()
    summary = "게시 중 문제가 발생해 중단되었습니다. 다시 시도해주세요."
    for needles, message in _ERROR_HINTS:
        if any(needle in haystack for needle in needles):
            summary = message
            break
    return f"{summary}{DETAIL_SEPARATOR}{raw}" if raw else summary


def split_publish_error(message: str):
    """Returns (사용자용 문장, 기술 상세 or '')."""
    if not message:
        return "", ""
    summary, _, detail = message.partition(DETAIL_SEPARATOR)
    return summary.strip(), detail.strip()


def naver_session_exists() -> bool:
    return NAVER_STATE_FILE.exists()


def clear_naver_session() -> None:
    NAVER_STATE_FILE.unlink(missing_ok=True)


def launch_browser(p, headless: bool = False):
    """Prefers the system-installed Chrome (more convincing to Naver's bot
    detection than bundled Chromium) with automation-tell flags stripped."""
    args = ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"]
    # Container-only flags, and only on Linux. On a Windows/macOS desktop
    # `--no-sandbox` just disables Chrome's sandbox for a window browsing the
    # live web — and makes Chrome show an "unsupported command-line flag"
    # warning bar, which is itself an automation tell.
    if sys.platform.startswith("linux"):
        args += ["--no-sandbox", "--disable-dev-shm-usage"]

    chrome_app_mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if sys.platform == "darwin" and Path(chrome_app_mac).exists():
        try:
            return p.chromium.launch(headless=headless, executable_path=chrome_app_mac, args=args)
        except Exception as exc:  # noqa: BLE001
            print(f"[naver_publisher] system Chrome launch failed, trying channel='chrome': {exc}")

    try:
        return p.chromium.launch(headless=headless, channel="chrome", args=args)
    except Exception as exc:  # noqa: BLE001
        print(f"[naver_publisher] channel='chrome' failed, falling back to bundled Chromium: {exc}")

    return p.chromium.launch(headless=headless, args=args)


def open_naver_login_session(blog_id: str = "") -> Dict[str, Any]:
    """Blocking — opens a visible Chrome window and waits up to 2 minutes for
    the admin to finish logging in, then saves the session."""
    from playwright.sync_api import sync_playwright

    NAVER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    clean_id = blog_id.strip()
    target_url = (
        f"https://nid.naver.com/nidlogin.login?url=https%3A%2F%2Fblog.naver.com%2F{clean_id}%3FRedirect%3DWrite"
        if clean_id
        else "https://nid.naver.com/nidlogin.login?url=https%3A%2F%2Fsection.blog.naver.com"
    )

    with sync_playwright() as p:
        browser = launch_browser(p, headless=False)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.goto(target_url, wait_until="domcontentloaded")

        logged_in = False
        for _ in range(120):
            try:
                if page.is_closed():
                    break
                cookies = context.cookies()
                if any(c.get("name") in ("NID_AUT", "NID_SES") for c in cookies):
                    time.sleep(2.0)  # let Naver finish setting all auth cookies before snapshotting
                    context.storage_state(path=str(NAVER_STATE_FILE))
                    logged_in = True
                    break
            except Exception:
                break
            time.sleep(1)

        browser.close()

    if logged_in and NAVER_STATE_FILE.exists():
        return {"success": True, "message": "🔑 네이버 로그인 세션이 저장되었습니다. 이제 게시를 진행할 수 있습니다."}

    clear_naver_session()
    return {"success": False, "message": "⚠️ 로그인이 완료되지 않은 상태에서 창이 닫혔습니다. 다시 시도해주세요."}


def extract_post_url(url: str, blog_id: str) -> Optional[str]:
    """발행이 끝난 뒤 Chrome이 옮겨 가는 '글 보기' 주소이면 정리한 주소를, 아니면 None.

    네이버는 발행하면 `blog.naver.com/{아이디}/{글번호}`(또는 같은 글의
    `PostView.naver?blogId=…&logNo=…`)로 이동합니다. 글쓰기 화면(`Redirect=Write`,
    `PostWriteForm`)이나 블로그 첫 화면은 글 번호가 없어 걸리지 않습니다.
    """
    if not url or not blog_id:
        return None
    bid = re.escape(blog_id)
    m = re.match(rf"https?://(?:m\.)?blog\.naver\.com/{bid}/(\d{{6,}})(?:[/?#]|$)", url)
    if m:
        return f"https://blog.naver.com/{blog_id}/{m.group(1)}"
    if re.search(r"PostView\.(?:naver|nhn)", url) and re.search(rf"[?&]blogId={bid}(?:&|$)", url):
        log = re.search(r"[?&]logNo=(\d{6,})", url)
        if log:
            return f"https://blog.naver.com/{blog_id}/{log.group(1)}"
    return None


def _find_published_url(context, blog_id: str) -> Optional[str]:
    """열린 탭과 그 안의 프레임(네이버 블로그 본문은 mainFrame 안)에서 글 보기 주소를 찾습니다."""
    for pg in list(context.pages):
        try:
            urls = [pg.url] + [fr.url for fr in pg.frames]
        except Exception:  # noqa: BLE001 — 닫히는 중인 탭
            continue
        for u in urls:
            found = extract_post_url(u, blog_id)
            if found:
                return found
    return None


def _human_delay(lo: float = _HUMAN_DELAY[0], hi: float = _HUMAN_DELAY[1]) -> None:
    import random

    time.sleep(random.uniform(lo, hi))


def _stage_images(paths: list, dest_dir: Path) -> list:
    """사진을 임시 폴더로 복사해 업로드 창에 줄 안정적인 경로를 만든다."""
    import shutil
    out = []
    for i, src in enumerate(paths):
        try:
            dest = dest_dir / f"photo_{i}{Path(src).suffix or '.jpg'}"
            shutil.copyfile(src, dest)
            out.append(dest)
        except Exception as exc:  # noqa: BLE001
            print(f"[naver_publish] failed to stage {src}: {exc}")
            out.append(None)
    return out


def _find_editor_frame(page):
    try:
        frame = page.frame(name="mainFrame")
        if not frame:
            page.wait_for_selector("#mainFrame", timeout=4000)
            frame = page.frame(name="mainFrame")
        return frame or page
    except Exception:
        return page


_CANCEL_SELECTORS = [
    ".se-popup-button-cancel",
    "button.se-popup-button-cancel",
    ".se-popup-button.se-popup-button-cancel",
    ".se-help-panel-close-button",
    ".se-popup-container button:nth-child(1)",
    # 페이지 전체에서 "취소"/"아니오" 텍스트를 찾던 이전 버전은 팝업과 무관한
    # 버튼까지 잡는 문제가 있었다(바로 아래 설명). 그렇다고 완전히 빼버리면
    # .se-popup-button-cancel 계열 클래스가 이번 팝업의 실제 버튼과 안
    # 맞을 때 아예 못 닫는 문제가 생긴다 — 그래서 .se-popup-container /
    # .se-popup-dim 안으로 범위를 좁혀서 텍스트 매칭을 다시 넣는다: 팝업
    # 컨테이너 밖의 동명 버튼은 여전히 안 걸리면서, 팝업 자체의 버튼
    # 클래스가 예상과 달라도 문구로 찾아낼 수 있다.
    ".se-popup-container button:has-text('취소')",
    ".se-popup-container button:has-text('아니오')",
    ".se-popup-dim button:has-text('취소')",
    # 2026-09 라이브 계정에서 브라우저 콘솔로 직접 확인: "이어서
    # 작성하시겠습니까?" 팝업이 se-popup-* 클래스가 아니라
    # layer_popup__<해시> 형태의 CSS 모듈 클래스로 렌더링되고 있었다(예:
    # layer_popup__MFPwH). 위 se-popup-* 셀렉터들은 이 팝업을 전혀 못
    # 찾아서, _popup_still_visible()이 "팝업 없음"으로 오판 → 아래
    # _dismiss_draft_restore_popup()의 재시도 루프(Escape 포함)가 아예
    # 한 번도 실행되지 않고 바로 제목 입력으로 넘어가 버렸다 — 그 결과
    # 사람이 수동으로 취소를 눌러야 했고, 그 사이 제목 입력이 날아갔다.
    # 해시 부분은 배포마다 바뀔 수 있어 접두사(layer_popup__)만으로 찾는다.
    "[class*='layer_popup__'] button:has-text('취소')",
    "[class*='layer_popup__'] button:has-text('아니오')",
    "[class*='layer_popup__'] button:has-text('닫기')",
    "[class*='layer_popup__'] button:has-text('괜찮')",
    "[class*='layer_popup__'] button:nth-child(1)",
]

# _CANCEL_SELECTORS의 텍스트/위치 기반 셀렉터가 이번에도 안 맞을 경우를
# 대비해, 팝업 컨테이너 자체는 이 목록으로 따로 감지한다 — 못 찾은 버튼을
# 추측해서 누르는 대신, 컨테이너 안의 실제 버튼 HTML을 로그로 남겨서
# 다음 실패 시 정확한 클래스명을 바로 알 수 있게 한다(더 이상 브라우저
# 콘솔을 직접 열어 확인할 필요 없이).
_POPUP_CONTAINER_SELECTORS = [
    ".se-popup-container",
    ".se-popup-dim",
    "[class*='layer_popup__']",
]

# 예전엔 텍스트 매칭이 .se-popup-container 범위 없이 페이지 전체
# (button:has-text('취소'))였다 — 실제 라이브 네이버 글쓰기 페이지에서
# 테스트해보니(자매 저장소 OSMU_thestitch에서 먼저 재현됨), 이어서
# 작성하시겠습니까 팝업은 .se-popup-button-cancel로 attempt 1에서 바로
# 닫혔는데, 그 뒤로도 범위 없는 button:has-text('취소')가 페이지 어딘가의
# *다른*(팝업과 무관한) "취소" 버튼을 계속 찾아내는 바람에 20번을 다 돌
# 때까지 "아직 팝업이 있다"고 오판했다 — 결국 타임아웃되거나 사용자가
# 아무 반응이 없다고 여겨 Chrome 창을 닫아버려 TargetClosedError로 게시가
# 죽었다(로그로 확인됨). 그렇다고 텍스트 매칭을 완전히 빼버렸더니 이번엔
# 반대로 "팝업이 전혀 안 닫힌다"는 문제가 났다 — 클래스만으로는 이번
# 팝업의 실제 버튼을 못 찾은 것으로 보인다. 그래서 위처럼
# .se-popup-container/.se-popup-dim으로 범위를 좁힌 텍스트 매칭을
# 다시 넣었다: 페이지 전체가 아니라 팝업 컨테이너 안에서만 찾는다.


def _popup_still_visible(page, editor_frame) -> bool:
    """True if a known cancel button OR the popup container itself is visible.

    Started out checking only _CANCEL_SELECTORS (deliberately narrow,
    Naver-specific button selectors — a broader check like generic
    dialog-role containers or free-text "취소" matches page-wide used to
    match ordinary Smart Editor chrome and make every publish fail/loop with
    a false "there's a popup" reading). That was too narrow the other way:
    when the popup's actual button text doesn't match any of our guesses
    (confirmed live — a layer_popup__<hash> container was visible but none
    of its buttons said 취소/아니오/닫기/괜찮), this returned False even
    though a popup really was blocking the page, so the retry loop below
    (Escape presses included) never even ran — the popup was just left
    sitting there. Now also treats the container itself
    (_POPUP_CONTAINER_SELECTORS) as "a popup is showing", independent of
    whether we can identify its button yet.
    """
    for context in (page, editor_frame):
        for sel in _CANCEL_SELECTORS + _POPUP_CONTAINER_SELECTORS:
            try:
                el = context.query_selector(sel)
                if el and el.is_visible():
                    return True
            except Exception:
                pass
    return False


def _log_popup_html(page, editor_frame, when: str) -> None:
    """Dump the popup container's outerHTML so a real selector can be added
    later without needing another round of manual browser-console digging."""
    for context in (page, editor_frame):
        for sel in _POPUP_CONTAINER_SELECTORS:
            try:
                html = context.eval_on_selector(
                    sel, "(el) => el.outerHTML && el.outerHTML.slice(0, 1500)"
                )
                if html:
                    print(f"[naver_paste_worker] popup HTML ({when}, {sel}): {html}")
            except Exception:
                pass


def _dismiss_draft_restore_popup(page, editor_frame, timeout_s: float = 120.0) -> None:
    """Wait for the '이어서 작성하시겠습니까?' resume-draft popup to be gone,
    instead of guessing which button closes it.

    This used to try to click the right button itself — Escape, known
    selectors, then a generic "click whatever button is in the popup
    container" fallback. Live testing kept finding new cases: the popup's
    real button classes turned out to differ between accounts/deploys
    (.se-popup-button-cancel sometimes, a hashed layer_popup__<hash> other
    times), and an automated click racing a human's own manual click on the
    same popup repeatedly left the editor in a half-focused state (title or
    the first paragraph of body missing). None of that is fixable by adding
    more selectors — the popup a human sees is ground truth, this script
    isn't. So this now just waits: if nothing is showing, proceed
    immediately (unchanged fast path); if something is showing, do nothing
    to it and poll until the person closes it themselves (however they
    close it — cancel or confirm, script doesn't care), same principle as
    never clicking Naver's real [발행] button on the human's behalf.
    """
    if not _popup_still_visible(page, editor_frame):
        return

    _log_popup_html(page, editor_frame, "waiting-for-human")
    print(
        "[naver_paste_worker] 네이버가 팝업을 띄웠습니다 — 자동으로 누르지 않습니다. "
        "Chrome 창에서 직접 닫아주세요(취소든 확인이든 상관없습니다). "
        "닫으시면 이어서 진행합니다."
    )

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _popup_still_visible(page, editor_frame):
            print("[naver_paste_worker] popup cleared by hand — continuing")
            return
        time.sleep(0.5)

    _log_popup_html(page, editor_frame, "still-open-after-timeout")
    raise RuntimeError(
        f"네이버가 띄운 팝업이 {int(timeout_s)}초가 지나도 닫히지 않았습니다. "
        "Chrome 창에서 팝업을 직접 닫아주신 뒤 [다시 게시]로 다시 시도해주세요."
    )


def _query_selector_retry(editor_frame, selectors: list[str], timeout_s: float = 8.0):
    """Poll for a visible match instead of a single query_selector call.

    Right after _dismiss_draft_restore_popup() clicks 취소, Naver's editor is
    still re-initializing for a moment (confirmed live: a run right after a
    popup dismissal logged "no title element matched any selector" / "no
    body element found" even though the selectors are correct and normally
    match) — a single immediate query_selector can catch it mid-redraw and
    find nothing. Retrying for a few seconds rides out that transition
    instead of giving up on the first empty result.

    8s (not the original 3s): live testing showed the *slower* the person is
    to dismiss the popup, the longer this re-initialization drags on — a
    popup left open a while before being cancelled seems to make Naver do a
    heavier reload/cleanup than an immediate cancel does. 3s wasn't enough
    margin for that case.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for sel in selectors:
            try:
                el = editor_frame.query_selector(sel)
                if el and el.is_visible():
                    return el
            except Exception:
                pass
        time.sleep(0.3)
    return None


def _click_until_focused(editor_frame, el, when: str, attempts: int = 15) -> bool:
    """click(force=True) "succeeds" (no exception) even when the real
    editing surface — a nested iframe inside editor_frame — hasn't finished
    initializing yet, in which case focus silently stays on document.body
    and the keystrokes/paste that follow go nowhere. Confirmed live: the
    same click, at the same selector, reached the nested iframe on a later
    attempt in the same run but not an earlier one — a readiness/timing gap,
    not a wrong selector. So retry the click for a couple of seconds,
    checking that focus actually reached the iframe, instead of clicking
    once and assuming it landed."""
    for attempt in range(attempts):
        try:
            el.click(force=True)
        except Exception:
            pass
        time.sleep(0.3)
        try:
            tag = editor_frame.evaluate("() => document.activeElement && document.activeElement.tagName")
        except Exception:
            tag = None
        if tag == "IFRAME":
            print(f"[naver_paste_worker] click reached the editor ({when}, attempt {attempt + 1})")
            return True
    print(f"[naver_paste_worker] click never reached the nested editor after {attempts} attempts ({when}) — proceeding anyway")
    return False


def _strip_strikethrough(editor_frame) -> None:
    """Naver's editor sometimes auto-applies strikethrough on paste — force it off."""
    try:
        editor_frame.evaluate(
            """() => {
                const editor = document.querySelector('.se-main-container, .se-content, body');
                if (!editor) return;
                editor.querySelectorAll('s, del, strike, [style*="line-through"]').forEach((el) => {
                    el.style.textDecoration = 'none';
                    const parent = el.parentNode;
                    if (parent) {
                        while (el.firstChild) parent.insertBefore(el.firstChild, el);
                        parent.removeChild(el);
                    }
                });
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[naver_paste_worker] strikethrough cleanup note: {exc}")


def _upload_photo_at_cursor(page, editor_frame, file_path: Path) -> bool:
    # button.se-image-toolbar-button는 실제 계정의 에디터 DOM에서 확인된
    # 진짜 클래스명이다(브라우저 콘솔로 라이브 검증함). 그런데도 계속
    # 업로드가 실패했던 건 클래스가 틀려서가 아니라 아래의 is_visible()
    # 체크 때문이었을 가능성이 높다 — Playwright의 is_visible()은 툴바가
    # 스크롤 밖에 있거나 전환 애니메이션 중이면 실제로 클릭 가능한
    # 버튼도 False로 판정할 수 있다. 그래서 is_visible() 대신 스크롤 후
    # bounding box 크기로 "화면에 실제로 그려졌는가"만 확인하고, 어떤
    # 선택자가 왜 실패했는지 로그로 남겨 다음 실패 시 바로 원인을 알 수
    # 있게 한다.
    photo_selectors = [
        "button.se-image-toolbar-button",
        ".se-insert-menu-button-image",
        "button.se-document-toolbar-image-button",
        "button[data-name='image']",
        ".se-toolbar-button-image",
    ]
    for sel in photo_selectors:
        try:
            btn = editor_frame.query_selector(sel)
            if not btn:
                continue
            try:
                btn.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            box = btn.bounding_box()
            if not box or box["width"] == 0 or box["height"] == 0:
                print(f"[naver_paste_worker] photo selector '{sel}' matched but has no visible box — skipping")
                continue
            # force=True로 클릭해도 파일 선택창이 안 뜨는 게 라이브로 확인됐다
            # (버튼은 진짜로 보이고 클래스도 맞는데 filechooser 이벤트가 안
            # 옴 — 클릭 전후 툴바 목록도 동일해서 드롭다운이 뜨는 구조도
            # 아니었다). force=True는 Playwright의 정상 actionability 검사를
            # 건너뛰는데, 네이티브 파일 선택창을 여는 데 필요한 "신뢰할 수
            # 있는 사용자 클릭"으로 브라우저가 인정하지 않는 것으로 보인다.
            # 그래서 먼저 일반 클릭(정상 actionability 검사 포함)을 시도하고,
            # 그게 실패할 때만 force=True로 넘어간다.
            opened = False
            for use_force in (False, True):
                try:
                    with page.expect_file_chooser(timeout=3000) as fc_info:
                        btn.click(force=use_force, timeout=2000)
                    fc_info.value.set_files([str(file_path)])
                    _human_delay(1.5, 2.5)
                    opened = True
                    break
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[naver_paste_worker] photo selector '{sel}' "
                        f"({'force' if use_force else 'normal'} click) did not open a file chooser: {exc}"
                    )
            if opened:
                return True
            _log_visible_buttons(editor_frame, page, f"after clicking '{sel}'")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[naver_paste_worker] photo selector '{sel}' failed: {exc}")
            continue

    try:
        file_inputs = editor_frame.query_selector_all('input[type="file"]')
        if file_inputs:
            file_inputs[0].set_input_files([str(file_path)])
            _human_delay(1.5, 2.5)
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"[naver_paste_worker] direct file input upload failed: {exc}")
    return False


def _log_visible_buttons(editor_frame, page, when: str) -> None:
    """List currently visible buttons/menu items so a follow-up submenu
    selector can be added without another round of manual console digging."""
    for context in (editor_frame, page):
        try:
            items = context.query_selector_all("button, [role='menuitem'], li")
            seen = []
            for el in items:
                try:
                    if el.is_visible():
                        text = (el.inner_text() or "").strip()[:20]
                        cls = (el.get_attribute("class") or "")[:60]
                        seen.append(f"'{text}'|{cls}")
                except Exception:
                    pass
            if seen:
                print(f"[naver_paste_worker] visible buttons ({when}): {seen[:25]}")
        except Exception:
            pass


# Campaigns with a publish run in progress (from the paste through the human
# review window). The publish button can be clicked again while a run is
# going, and the view is rebuilt on navigation, so the guard lives here.
_IN_FLIGHT: set = set()
_IN_FLIGHT_LOCK = threading.Lock()

# The paste phase goes through the one OS clipboard. Two runs pasting at once
# would interleave each other's paragraphs, so only the paste phase is
# serialized — a second post can start pasting while the first one's window
# is merely waiting for the marketer to review it.
_PASTE_LOCK = threading.Lock()




def publish(blog_id: str, title: str, segments: List[Tuple[str, str]],
            on_status: Optional[Callable[[str], None]] = None) -> bool:
    """제목·본문 조각을 새 스마트에디터 창에 붙여넣고, 사용자가 창을 닫을 때까지 기다립니다.

    segments: [("html", "<p>...</p>"), ("image", "/절대/경로.jpg"), ...] — 문서 순서대로.
    발행 후 글 주소가 확인되면 on_status로 알리고 주소를 돌려주는 대신 True를 반환합니다.
    같은 블로그로 이미 진행 중이면 아무것도 하지 않고 False.
    """
    on_status = on_status or (lambda _m: None)
    with _IN_FLIGHT_LOCK:
        if blog_id in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(blog_id)
    try:
        _run(blog_id, title, segments, on_status)
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(blog_id)
    return True


def _run(blog_id: str, title: str, segments: List[Tuple[str, str]], on_status: Callable[[str], None]) -> None:
    from playwright.sync_api import sync_playwright

    blog_id = (blog_id or "").strip()
    if not blog_id:
        on_status("❌ 네이버 블로그 아이디가 없습니다. 설정에서 먼저 입력해 주세요.")
        return
    title = (title or "").strip() or "(제목 없음)"
    image_paths = [v for k, v in segments if k == "image"]

    with TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        local_images = _stage_images(image_paths, tmp_dir) if image_paths else []
        local_by_tag = dict(zip(image_paths, local_images))
        state_option = {"storage_state": str(NAVER_STATE_FILE)} if NAVER_STATE_FILE.exists() else {}
        target_url = f"https://blog.naver.com/{blog_id}?Redirect=Write"

        browser = None
        paste_lock_held = False
        try:
            with sync_playwright() as p:
                browser = launch_browser(p, headless=False)
                context = browser.new_context(viewport={"width": 1280, "height": 900}, **state_option)
                page = context.new_page()
                page.on("dialog", lambda dialog: dialog.accept())
                page.goto(target_url, wait_until="domcontentloaded")
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                _human_delay(1.0, 1.5)

                if "nidlogin" in page.url:
                    on_status("❌ 네이버 로그인이 풀렸습니다. 다시 로그인한 뒤 게시해 주세요.")
                    browser.close()
                    return

                editor_frame = _find_editor_frame(page)
                _dismiss_draft_restore_popup(page, editor_frame)
                modifier = "Meta" if sys.platform == "darwin" else "Control"

                if not _PASTE_LOCK.acquire(blocking=False):
                    on_status("⏳ 다른 글을 붙여넣는 중이라, 끝나면 이어서 붙여넣습니다…")
                    _PASTE_LOCK.acquire()
                paste_lock_held = True
                failed_photos: list = []
                title_ok = True

                body_sel = [".se-main-container", ".se-component-text", ".se-content"]
                body_el = _query_selector_retry(editor_frame, body_sel)
                if body_el:
                    _click_until_focused(editor_frame, body_el, "body (clear step)")
                    _human_delay(0.3, 0.5)
                    page.keyboard.press(f"{modifier}+a")
                    page.keyboard.press("Delete")
                    _human_delay(0.5, 0.8)

                title_selectors = [".se-component-title .se-text-paragraph", ".se-document-title .se-text-paragraph",
                                   ".se-title-text", "div.se-component-title p"]
                title_el = _query_selector_retry(editor_frame, title_selectors)
                if title_el:
                    _click_until_focused(editor_frame, title_el, "title")
                    _human_delay(0.2, 0.3)
                    page.keyboard.press(f"{modifier}+a")
                    page.keyboard.press("Backspace")
                    page.keyboard.type(title, delay=15)
                    _human_delay(0.4, 0.6)

                body_el_refocus = _query_selector_retry(editor_frame, body_sel, timeout_s=4.0) or body_el
                if body_el_refocus:
                    _click_until_focused(editor_frame, body_el_refocus, "body refocus (before paste loop)")
                    _human_delay(0.2, 0.3)

                for kind, value in segments:
                    if kind == "html":
                        if not value:
                            continue
                        copy_html_to_clipboard(value)
                        page.keyboard.press(f"{modifier}+v")
                        _human_delay(1.5, 2.0)
                        _strip_strikethrough(editor_frame)
                    else:
                        local_path = local_by_tag.get(value)
                        if local_path is None or not _upload_photo_at_cursor(page, editor_frame, local_path):
                            failed_photos.append(value)
                        _human_delay(1.5, 2.0)

                if title.strip():
                    fresh = _query_selector_retry(editor_frame, title_selectors, timeout_s=4.0) or title_el
                    current = ""
                    if fresh:
                        try:
                            current = (fresh.inner_text() or "").strip()
                        except Exception:  # noqa: BLE001
                            current = ""
                    if fresh and not current:
                        fresh.click(force=True)
                        _human_delay(0.3, 0.5)
                        page.keyboard.press(f"{modifier}+a")
                        page.keyboard.press("Backspace")
                        page.keyboard.type(title, delay=15)
                        _human_delay(0.4, 0.6)
                        check = _query_selector_retry(editor_frame, title_selectors, timeout_s=2.0)
                        try:
                            title_ok = bool(check and (check.inner_text() or "").strip())
                        except Exception:  # noqa: BLE001
                            title_ok = False
                    elif not fresh:
                        title_ok = False

                _PASTE_LOCK.release()
                paste_lock_held = False

                warnings = []
                if failed_photos:
                    warnings.append(f"사진 {len(failed_photos)}장을 자동으로 넣지 못했습니다. Chrome 창에서 직접 추가해 주세요: "
                                    + ", ".join(Path(v).name for v in failed_photos))
                if not title_ok:
                    warnings.append("제목을 자동으로 입력하지 못했습니다. Chrome 창에서 직접 확인해 주세요.")
                if warnings:
                    on_status("⚠️ 붙여넣기는 끝났지만 확인이 필요합니다 — " + " / ".join(warnings))
                else:
                    on_status("✅ 붙여넣기 완료 — Chrome 창에서 내용을 확인하고 네이버 [발행]을 눌러 주세요. 창은 직접 닫을 때까지 열려 있습니다.")

                recorded_url = None
                while True:
                    try:
                        if page.is_closed() or not browser.is_connected() or len(context.pages) == 0:
                            break
                        if recorded_url is None:
                            found = _find_published_url(context, blog_id)
                            if found:
                                recorded_url = found
                                on_status(f"🎉 발행을 확인했습니다: {found}")
                        page.wait_for_timeout(1000)
                    except Exception:
                        break
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            on_status(f"❌ {split_publish_error(format_publish_error(exc))[0]}")
        finally:
            if paste_lock_held:
                _PASTE_LOCK.release()
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
