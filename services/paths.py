"""앱 경로 한곳 정리 — 개발 실행과 Flet 배포(flet build/pack) 모두에서 동작하도록.

- 읽기 전용 리소스(폰트 등): assets/  (배포 시 FLET_ASSETS_DIR)
- 쓰기 가능한 데이터(결과물·업로드·로그): 배포 앱은 앱 설치 폴더가 읽기 전용이므로
  FLET_APP_STORAGE_DATA(운영체제의 앱 데이터 폴더)를 사용, 개발 실행 시에는 프로젝트 폴더
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ASSETS_DIR = os.path.abspath(os.environ.get("FLET_ASSETS_DIR") or os.path.join(BASE_DIR, "assets"))
FONTS_DIR = os.path.join(ASSETS_DIR, "fonts")

def _user_data_dir() -> str:
    """설치형(flet build exe) 사용자 데이터 폴더 — 설치·재설치·제거로 지워지지 않는 곳 (Windows: %APPDATA%\\SamhoFood_OSMU)"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "SamhoFood_OSMU")


# 패키징된 앱은 설치 폴더(읽기 전용·재설치 때 비워짐)에 아무것도 쓰지 않는다.
IS_PACKAGED = bool(os.environ.get("FLET_APP_STORAGE_DATA"))

DATA_DIR = os.path.abspath(os.environ.get("OSMU_DATA_DIR") or (_user_data_dir() if IS_PACKAGED else BASE_DIR))
EXPORTS_DIR = os.path.join(DATA_DIR, "exports")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
LOG_DIR = os.path.join(DATA_DIR, "logs")

for _d in (os.path.join(EXPORTS_DIR, "cards"), os.path.join(EXPORTS_DIR, "shorts"), UPLOAD_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)


def _add_bundled_ffmpeg_to_path() -> None:
    """설치 프로그램이 exe 옆 ffmpeg 폴더에 넣어 둔 ffmpeg/ffprobe를 PATH 앞에 추가 (없으면 시스템 것을 씀)"""
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    for base in (exe_dir, os.path.dirname(BASE_DIR), os.path.dirname(os.path.dirname(BASE_DIR)), BASE_DIR):
        folder = os.path.join(base, "ffmpeg")
        if os.path.exists(os.path.join(folder, "ffmpeg.exe")) or os.path.exists(os.path.join(folder, "ffmpeg")):
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
            return


_add_bundled_ffmpeg_to_path()
