# 삼호푸드 OSMU 스튜디오

블로그 글 하나로 **네이버 블로그 · 카드뉴스(1080×1350) · 쇼츠(1080×1920)** 를 만드는 일반 사용자용 앱입니다.
(ammasarang_card_shorts의 엔진 + OSMU_cjc의 네이버 게시 자동화를 합친 Flet 앱)

## 사용 순서
1. **알릴 내용** — 브랜드(삼호푸드 / 삼삼이고등어), 타깃, 상품, 이번 주 소식, 사진 입력
2. **블로그 글** — AI(Gemini 또는 Claude)가 초안 작성 → 직접 수정, 표현 점검
3. **카드뉴스** — 블로그 글로 카드 생성 (내 사진이 있으면 배경으로 사용)
4. **쇼츠** — 카드로 TTS + 움직이는 자막 영상 생성
5. **네이버 게시** — 이미지 선택(내 사진 / 카드뉴스 / 둘 다) 후 스마트에디터에 자동 붙여넣기, [발행]은 직접

상단 **비용** 탭에서 토큰·이미지 사용량과 추정 금액, 월 예산을 확인합니다. 단가는 공식 요금표를 보고 직접 입력합니다.

## 실행
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install ffmpeg        # 쇼츠용
./run.sh                   # 데스크톱 앱  (브라우저: FLET_VIEW=web ./run.sh)
```
첫 실행 후 **설정**에서 API 키를 등록하세요 (키는 `~/.osmu_media_studio/keys.json`에만 저장). 네이버 게시는 Chrome이 필요합니다.

## 구조
- `main.py` 단계형 화면, `ui_extra.py` 설정·비용 화면
- `services/blog_writer.py` 블로그 원고·이미지 배치, `services/naver_publish.py` 네이버 게시
- `presets/brand_kits/` 삼호푸드·삼삼이고등어 브랜드 킷, `compliance_packs/food_seafood.json` 식품 표시광고 규칙(법률 검토 대체 아님)
- 나머지 `services/`는 카드·쇼츠 엔진 (ammasarang 기반)
