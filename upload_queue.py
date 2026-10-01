"""アップロードキュー。

YouTube API の quota(1日10000、1upload≈150 = videos.insert 100 + thumbnails.set 50、改定後の現行値)とチャンネル運用ペースを守るための
キュー機構。submit→worker分離、レンダリング用のキュー(非同梱)とは独立(用途と cadence が違う)。

ルール:
  - 1企画 = 本編1本 + 付随の短尺(SHORT_VARIANTS 環境変数で定義。既定は無し)
  - submit は全動画を private・予約なし(publish_at=None)で投入する。公開予約は人が YouTube Studio で設定する
  - 空きスロット計算(`_next_publish_slot_yt_aware`)は現行の submit では使わない(関数は残置)
  - 予約公開したい場合はジョブ JSON の publish_at を使う(または upload_post_youtube.py の --publish-at)(PUBLISH_HOURS の時刻に合うかを検査。下記)
  - 公開スロットの時刻は環境変数 PUBLISH_HOURS(JST)。必須設定で既定値なし(--publish-at の検査でのみ参照)
  - 1日上限: なし(quota超過はYouTube API 403で自然停止。DAILY_PROJECT_LIMIT撤廃)
  - submit 時に fcntl lockfile 二重起動防止済みの worker を Popen(start_new_session=True) で即起動

ジョブファイル: videos/_upload_queue/<job_id>.json
状態:           queued → uploading → scheduled | failed | timeout

CLI:
  submit:   python upload_queue.py submit --slug <topic> [--channel <ch>]
  list:     python upload_queue.py list
  status:   python upload_queue.py status <job_id>
  worker:   python upload_queue.py worker
  once:     python upload_queue.py once  (test)
  schedule: python upload_queue.py schedule
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
# 公開版の配置(スクリプトと src/ が同階層)でも、ホストの src/ 直下に置く配置でも動くように ROOT を決める
ROOT = _HERE if (_HERE / "src" / "channel_resolver.py").exists() else _HERE.parent
import sys as _sys
_sys.path.insert(0, str(ROOT / "src"))
from channel_resolver import videos_root as _videos_root  # noqa: E402

# Phase B: queue dir はチャンネル別解決。default は legacy fallback で ./videos/_upload_queue を継続。
QUEUE_DIR = _videos_root() / "_upload_queue"
LOG_DIR = QUEUE_DIR / "_logs"
WORKER_LOCK = QUEUE_DIR / ".worker.lock"
PY = Path(sys.executable)  # 現在の Python 環境をそのまま使う

JST = ZoneInfo("Asia/Tokyo")
STAGE4_MARKER_FILENAME = ".stage4_approved.yaml"
STAGE4_MARKER_TTL_HOURS = int(os.environ.get("STAGE4_MARKER_TTL_HOURS", "24"))  # 承認マーカーの有効時間(設定値)
# 公開スロット時刻 (JST)。既定値なし(必須設定)。環境変数 PUBLISH_HOURS="<h1>,<h2>" で渡す。
PUBLISH_HOURS_JST = tuple(int(h) for h in os.environ.get("PUBLISH_HOURS", "").split(",") if h.strip())
# 本編に対する短尺の公開オフセット(分)。設定値(既定 0)。
SHORTS_OFFSET_MINUTES = int(os.environ.get("SHORTS_OFFSET_MINUTES", "0"))
# DAILY_PROJECT_LIMIT 撤廃: quota超過はYouTube API 403で自然停止
POLL_INTERVAL = 30.0          # 30秒
JOB_TIMEOUT_SEC = 60 * 30     # 30分(API quota待ちでも30分超えはエラー)
# shorts variants
# 環境変数 SHORT_VARIANTS="a,b" のように渡す。既定は短尺なし(本編のみ)。
SHORT_VARIANTS = tuple(v.strip() for v in os.environ.get("SHORT_VARIANTS", "").split(",") if v.strip())


# ---------- 基本IO ----------

def _ensure_dirs() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _new_job_id(slug: str) -> str:
    ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    rand = secrets.token_hex(3)
    safe_slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in slug)[:32]
    return f"{ts}_{rand}_{safe_slug}"


def _job_path(job_id: str) -> Path:
    return QUEUE_DIR / f"{job_id}.json"


def _read_job(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _write_job(job: dict) -> None:
    _job_path(job["id"]).write_text(json.dumps(job, ensure_ascii=False, indent=2))


def list_jobs(status_filter: str | None = None) -> list[dict]:
    _ensure_dirs()
    out = []
    for p in sorted(QUEUE_DIR.glob("*.json")):
        j = _read_job(p)
        if not j:
            continue
        if status_filter and j.get("status") != status_filter:
            continue
        out.append(j)
    return out


# ---------- 公開スケジュール計算 ----------

def _parse_iso8601_duration_seconds(duration: str) -> float:
    """ISO 8601 duration(PTxHxMxS)を秒数(float)に変換する。

    Examples:
        "PT1H23M45S" -> 5025.0
        "PT2M30S"    -> 150.0
        "PT45S"      -> 45.0
        "PT0S"       -> 0.0

    パース失敗時は 0.0 を返す(呼出側で判定)。
    """
    import re as _re
    m = _re.fullmatch(
        r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?",
        duration or "",
    )
    if not m:
        return 0.0
    days    = float(m.group(1) or 0)
    hours   = float(m.group(2) or 0)
    minutes = float(m.group(3) or 0)
    seconds = float(m.group(4) or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _fetch_yt_scheduled_slots(channel: str | None = None) -> set[dt.datetime]:
    """YouTube Data API から既存の予約公開スロットを取得する。

    `search.list forMine=True order=date` で直近動画を取得し、
    `videos.list part=contentDetails,status` で publishAt と duration を解決して返す。

    本編スロット判定条件(呼出側 _next_publish_slot_yt_aware で別途フィルタ):
      - privacyStatus == "private"(予約公開)
      - publishAt が未来
      - 本編タイプ: publishAt がスロット時刻ぴったり かつ duration > 60s

    quota コスト: search.list=100 + videos.list(1件)=1 = 約101 units/call
    submit は数回/日なので許容範囲。

    失敗時は警告のみ(ローカルキューだけでスロット判定する後方互換フォールバック)。

    env `SKIP_YT_SLOT_FETCH=1` で API 呼出を完全 skip。
    quotaExceeded 時に「ローカルキューだけで slot 計算したい」用途の opt-in。
    upload_queue は uploaded jobs (status=scheduled) の publish_at もローカル記録してるため
    完全な slot 計算には十分(YT API 経由は他人の動画も含めて取れる、より厳密)。
    """
    import os as _os
    used: set[dt.datetime] = set()
    if _os.environ.get("SKIP_YT_SLOT_FETCH") == "1":
        print("[upq] SKIP_YT_SLOT_FETCH=1: YT API search skipped, using local queue only", flush=True)
        return used
    try:
        import sys as _sys
        _sys.path.insert(0, str(ROOT / "src"))
        from yt_auth import get_credentials  # type: ignore[import-untyped]
        from googleapiclient.discovery import build  # type: ignore[import-untyped]

        creds = get_credentials(channel=channel)
        yt = build("youtube", "v3", credentials=creds)

        # 予約公開動画を日付順で取得(eventType=upcoming は shorts を除外してしまうため使わない)
        search_resp = yt.search().list(
            part="id",
            forMine=True,
            order="date",
            type="video",
            maxResults=50,
        ).execute()

        video_ids = [item["id"]["videoId"] for item in search_resp.get("items", [])
                     if item.get("id", {}).get("videoId")]
        if not video_ids:
            return used

        # publishAt と duration を取得
        videos_resp = yt.videos().list(
            part="status,contentDetails",
            id=",".join(video_ids),
        ).execute()

        now_utc = dt.datetime.now(dt.timezone.utc)

        for item in videos_resp.get("items", []):
            status = item.get("status", {})
            # private(予約公開)のみ対象
            if status.get("privacyStatus") != "private":
                continue
            pa_str = status.get("publishAt")
            if not pa_str:
                continue
            try:
                t_utc = dt.datetime.fromisoformat(pa_str.replace("Z", "+00:00"))
            except Exception:
                continue
            # publishAt が過去の動画は除外(既に公開済み or 公開失敗)
            if t_utc <= now_utc:
                continue

            # duration(ISO 8601)をパースして 60s 以下は Shorts とみなしスキップ
            duration_str = item.get("contentDetails", {}).get("duration", "")
            duration_sec = _parse_iso8601_duration_seconds(duration_str)
            if duration_sec <= 60.0:
                continue  # Shorts(≤60s)はスロット占有対象外

            t_jst = t_utc.astimezone(JST)
            used.add(t_jst)
    except Exception as e:
        print(f"[upq][warn] YT scheduled slots fetch failed (non-fatal, using local queue only): {e}",
              flush=True)
    return used


def _next_publish_slot_yt_aware(
    channel: str | None = None,
    now: dt.datetime | None = None,
    shorts_offset_minutes: int = SHORTS_OFFSET_MINUTES,
) -> tuple[dt.datetime, dt.datetime]:
    """全企画共有スロット(PUBLISH_HOURS_JST)の次の空きを返す。

    ローカルキューのジョブ + YouTube Data API の既存予約を合算して
    未使用の最も近いスロットを返す。

    Returns:
        (main_dt, shorts_dt):
          - main_dt: 本編の publishAt(スロット時刻ぴったり)
          - shorts_dt: Shorts の publishAt(main_dt + shorts_offset_minutes)
    """
    if not PUBLISH_HOURS_JST:
        raise RuntimeError("PUBLISH_HOURS is required (comma-separated hours in JST); no default slots are provided")
    now = now or dt.datetime.now(tz=JST)

    # 1. ローカルキューの占有スロット
    # fix: failed/cancelled も占有扱いにする(slot 重複投入 bug 修正)。
    # 由来: 失敗直後に次の job を submit すると、失敗 job が「空き」判定されて
    # 同一スロットに重複投入される race condition。
    # 失敗 job を queue から削除した時点で used から自動消失するため、再 submit 時は安全。
    used: set[dt.datetime] = set()
    for j in list_jobs():
        if j.get("status") == "cancelled":
            continue  # cancelled だけ skip(明示的キャンセル、再 submit 不要意思表示)
        pa = j.get("publish_at")
        if not pa:
            continue
        try:
            t = dt.datetime.fromisoformat(pa.replace("Z", "+00:00")).astimezone(JST)
            # 本編タイプ判定: スロット時刻ぴったりのもののみ占有カウント
            if t.minute == 0 and t.second == 0 and t.hour in PUBLISH_HOURS_JST:
                used.add(t)
        except Exception:
            continue

    # 2. YouTube API 既存予約スロット(API 失敗時は空set → ローカルキューのみで判定)
    yt_used = _fetch_yt_scheduled_slots(channel)
    # YT 側はスロット時刻ぴったりのスロットのみカウント(Shorts のオフセット枠は除外)
    for t in yt_used:
        if t.minute == 0 and t.second == 0 and t.hour in PUBLISH_HOURS_JST:
            used.add(t)

    # 3. 候補スロットを今日から順に生成して最初の空きを返す
    check_date = now.date()
    for _ in range(365):  # 1年分でタイムアウト防止
        for hour in PUBLISH_HOURS_JST:
            candidate = dt.datetime(check_date.year, check_date.month, check_date.day,
                                    hour, 0, 0, tzinfo=JST)
            if candidate <= now:
                continue
            if candidate not in used:
                shorts_dt = candidate + dt.timedelta(minutes=shorts_offset_minutes)
                return candidate, shorts_dt
        check_date += dt.timedelta(days=1)
    raise RuntimeError("publish slot exhausted (365 days)")


# 後方互換エイリアス(schedule サブコマンド等から呼ばれる)
def _next_publish_slot(now: dt.datetime | None = None) -> dt.datetime:
    """後方互換: 本編スロットのみ返す(_next_publish_slot_yt_aware の channel=None ラッパー)。"""
    main_dt, _ = _next_publish_slot_yt_aware(channel=None, now=now)
    return main_dt


# ---------- stage4 marker ----------

def _verify_stage4_marker(slug: str) -> None:
    """Stage4承認マーカーファイルを検証する。

    TTL(STAGE4_MARKER_TTL_HOURS)内の .stage4_approved.yaml が存在しない場合は SystemExit。
    エージェントが直接 submit を呼ぶ場合の安全ゲート。

    マーカーが存在すれば HUMAN_TRIGGERED=1 なしでも submit を通す。
    マーカー: <videos_root>/<slug>/.stage4_approved.yaml に `approved_at: <ISO8601>` を人が書く(生成スクリプトは同梱しない)。
    """
    import yaml as _yaml
    base = _videos_root() / slug
    marker_path = base / STAGE4_MARKER_FILENAME
    if not marker_path.exists():
        raise SystemExit(
            f"Stage4承認マーカーが見つかりません: {marker_path}\n"
            f"人間による明示承認が必要です。\n"
            f"対応: {marker_path} に `approved_at: <ISO8601>` を書くか、HUMAN_TRIGGERED=1 を付けて実行"
        )
    try:
        data = _yaml.safe_load(marker_path.read_text())
    except Exception as e:
        raise SystemExit(f"Stage4マーカーの読み込みに失敗: {e}")

    approved_at_str = data.get("approved_at")
    if not approved_at_str:
        raise SystemExit(f"Stage4マーカーに approved_at がありません: {marker_path}")

    try:
        approved_at = dt.datetime.fromisoformat(approved_at_str)
        if approved_at.tzinfo is None:
            approved_at = approved_at.replace(tzinfo=JST)
    except Exception as e:
        raise SystemExit(f"Stage4マーカーの approved_at をパースできません: {e}")

    age_hours = (dt.datetime.now(tz=JST) - approved_at.astimezone(JST)).total_seconds() / 3600
    if age_hours > STAGE4_MARKER_TTL_HOURS:
        raise SystemExit(
            f"Stage4マーカーの有効期限切れ (age={age_hours:.1f}h > TTL={STAGE4_MARKER_TTL_HOURS}h)。\n"
            f"再承認: {marker_path} の approved_at を更新"
        )


# ---------- review/ final 存在確認 ----------

def _verify_review_final_exists(slug: str) -> None:
    """submit 前に review/ フォルダに Stage3 本体(final.mp4)が配置されているか確認する。

    Stage4 承認フローでは director が final.mp4 を review/ へ実体コピーしてから
    人間が目視確認・承認するため、review/ に final.mp4 相当が無ければ
    Stage3 未完了またはレビュー未完了とみなして BLOCK する。

    判定:
      - review/ 配下に <slug>-*.mp4 / <slug>.mp4 / <slug>/final.mp4 など
        slug 名を含む *-final.mp4 / *_final.mp4 / <slug>.mp4 / final.mp4 ファイルを探す
      - av_only.mp4 / stage2.mp4 しか無い場合は BLOCK(Stage3本体が無い)
      - 1件でも Stage3 本体ファイルが見つかれば PASS

    注意: このゲートは HUMAN_TRIGGERED=1 経由でも通る。
    Stage4承認マーカー検証(_verify_stage4_marker)とは独立したガード。
    """
    review_dir = ROOT / "review"
    if not review_dir.exists():
        raise SystemExit(
            f"BLOCK: review/ ディレクトリが存在しません。\n"
            f"  Stage4 前に `cp <topic>/final.mp4 review/{slug}.mp4` で実体コピーを配置してください。"
        )

    # slug を含む mp4 ファイルを収集
    candidates = list(review_dir.glob(f"**/{slug}*.mp4")) + list(review_dir.glob(f"**/{slug}/*.mp4"))
    if not candidates:
        raise SystemExit(
            f"BLOCK: review/ に slug '{slug}' のファイルが見つかりません。\n"
            f"  Stage4 前に `cp <topic>/final.mp4 review/{slug}.mp4` を実行してください。"
        )

    # Stage3 本体判定:
    #   - 名前に "final" が含まれる: <slug>-final.mp4 / <slug>_final.mp4 / <slug>/final.mp4 等
    #   - または slug と完全一致: review/<slug>.mp4
    #   Shorts(short-A/B/C)/ av_only / stage2 は Stage3 本体としてカウントしない
    STAGE2_KW = ("av_only", "stage2", "avonly")
    SHORTS_KW = ("short-a", "short-b", "short-c", "shorta", "shortb", "shortc", "short_a", "short_b", "short_c")
    stage3_files = [
        p for p in candidates
        if (
            # "final" を含む
            "final" in p.name.lower()
            # または slug.mp4 そのまま(例: review/<topic>.mp4)
            or p.name.lower() == f"{slug}.mp4"
        )
        and not any(kw in p.name.lower() for kw in STAGE2_KW)
        and not any(kw in p.name.lower() for kw in SHORTS_KW)
    ]
    if not stage3_files:
        raise SystemExit(
            f"BLOCK: review/ に Stage3 本体(final.mp4)が見つかりません。\n"
            f"  見つかったファイル: {[p.name for p in candidates]}\n"
            f"  av_only.mp4 / stage2.mp4 / short-A 等は Stage3 本体としてカウントしません。\n"
            f"  Stage3 final.mp4 を `cp <topic>/output/final.mp4 review/{slug}.mp4` で配置してください。"
        )


# ---------- submit ----------

def _slug_video_files(slug: str) -> list[tuple[str, Path]]:
    """slug に対応する全動画ファイル(本編+短尺)のリストを返す。

    Returns: [(label, path), ...] label は "main" / "short-<variant>"
    """
    base = _videos_root() / slug
    files = []
    # 本編
    main_path = base / "final.mp4"
    if main_path.exists():
        files.append(("main", main_path))
    # Shorts
    for v in SHORT_VARIANTS:
        p = base / "shorts" / f"short-{v.upper()}" / "final.mp4"
        if p.exists():
            files.append((f"short-{v}", p))
    return files


def submit(slug: str, channel: str | None = None) -> list[dict]:
    """1企画(本編+短尺)をキューに投入する(private・予約なし。公開予約は人が設定)。

    Args:
        slug: 動画スラッグ
        channel: チャンネル名。None 時は channel_resolver / .current_channel で自動解決(Q1)

    Returns: 生成したジョブのリスト(本編+短尺)
    submit 直後に worker を Popen(start_new_session=True) で起動する(fcntl lockfile で二重起動防止)。

    HUMAN_TRIGGERED env hard guard。
      HUMAN_TRIGGERED=1(人が付ける)か有効な承認マーカーが無い submit は拒否する。
      想定する事故: agent が直接 submit を打ち複数本が一括で実アップされる。

    現行仕様:
      - publish_at は None(privacyStatus=private で upload し、publishAt は付けない)
      - 公開予約は人が YouTube Studio で設定する(またはジョブの publish_at に ISO 日時を入れた場合のみ予約公開)
      - 空きスロット計算は行わない
    """
    # channel を解決(None 時は channel_resolver に委譲)
    from channel_resolver import current_channel as _current_channel
    resolved_channel = _current_channel(channel)
    # キュー・ロック・動画置き場は import 時に決まる既定チャンネル(CHANNEL env / DEFAULT_CHANNEL)の下にある。
    # 別チャンネルを --channel だけで指定しても場所は変わらないため、食い違いは明示的に拒否する。
    if resolved_channel != _current_channel(None):
        raise SystemExit(
            f"このキューは既定チャンネル({_current_channel(None)})専用です。"
            f"別チャンネル({resolved_channel})は CHANNEL={resolved_channel} を環境変数に設定して実行してください。"
        )

    # Guard: HUMAN_TRIGGERED=1(人が明示) OR Stage4承認マーカーが有効な場合のみ通す
    if os.environ.get("HUMAN_TRIGGERED") != "1":
        # マーカー検証(TTL は STAGE4_MARKER_TTL_HOURS、既定 24h)。失効 or 不在なら SystemExit
        _verify_stage4_marker(slug)

    # review/ に Stage3 本体(final.mp4)が存在するか確認(HUMAN_TRIGGERED 経由でも通す)
    _verify_review_final_exists(slug)

    base = _videos_root() / slug
    if not base.exists():
        raise FileNotFoundError(f"topic not found: {base}")

    video_files = _slug_video_files(slug)
    if not video_files:
        raise FileNotFoundError(f"no uploadable mp4 found under {base} (expected final.mp4 or shorts/short-<variant>/final.mp4)")

    _ensure_dirs()
    # 仕様変更 — 空きスロット計算を **撤廃**、全 upload を private 即時化。
    # 由来: search.list quota 枯渇 / failed job slot 重複 race / 公開タイミング柔軟性向上要望。
    # 公開予約は人間が YouTube Studio で動画ごとに手動設定する運用に統一。
    # publish_at = None → upload_youtube.py が privacy=private で即時 upload(publishAt なし)。
    submitted_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    job_id_base = _new_job_id(slug)

    jobs = []
    for label, target in video_files:
        job_id = f"{job_id_base}_{label}"
        job = {
            "id": job_id,
            "slug": slug,
            "channel": resolved_channel,
            "label": label,   # "main" / "short-<variant>"
            "status": "queued",
            "submitted_at": submitted_at,
            "publish_at": None,  # スロット予約廃止
            "publish_at_jst": "private (no schedule)",
            "video_path": str(target.relative_to(ROOT)),
            "started_at": None,
            "ended_at": None,
            "video_id": None,
            "url": None,
            "log": None,
            "exit_code": None,
            "pid": None,
        }
        _write_job(job)
        jobs.append(job)

    # submit 時に worker を即起動(fcntl lockfile で二重起動防止)
    _ensure_worker_running()
    return jobs


def _ensure_worker_running() -> None:
    """worker が未起動なら Popen(start_new_session=True) で起動する。既存 worker がいれば何もしない。"""
    if WORKER_LOCK.exists():
        try:
            import re
            m = re.search(r"pid=(\d+)", WORKER_LOCK.read_text())
            if m:
                try:
                    os.kill(int(m.group(1)), 0)
                    # プロセス生存 → 何もしない
                    return
                except ProcessLookupError:
                    print(f"[upq] stale worker lock (pid={m.group(1)} dead), restarting worker ...", file=sys.stderr)
                except PermissionError:
                    return  # 別ユーザのプロセス=生存
        except Exception:
            pass

    # worker 未起動 → バックグラウンドで起動
    cmd = [str(PY), str(Path(__file__).resolve()), "worker"]
    env = os.environ.copy()
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"[upq] worker started pid={proc.pid}", flush=True)


# ---------- worker ----------

def _try_acquire_worker_lock():
    import fcntl
    _ensure_dirs()
    # 既存ロックファイルの pid を読み、死亡プロセスなら警告ログを出して撤去
    if WORKER_LOCK.exists():
        try:
            prev = WORKER_LOCK.read_text().strip()
            import re
            m = re.search(r"pid=(\d+)", prev)
            if m:
                prev_pid = int(m.group(1))
                try:
                    os.kill(prev_pid, 0)
                except ProcessLookupError:
                    print(f"[upq] stale worker lock detected (pid={prev_pid} dead), removing", file=sys.stderr)
                    WORKER_LOCK.unlink()
                except PermissionError:
                    pass  # プロセス生存(別ユーザ)
        except Exception:
            pass
    f = open(WORKER_LOCK, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.write(f"pid={os.getpid()} started={dt.datetime.now().isoformat()}\n")
    f.flush()
    return f


def _pick_next_job(now: dt.datetime | None = None) -> dict | None:
    """queued ジョブのうち最も古い submitted_at を返す。

    shorts ジョブは同 slug の本編(label=main)が status=scheduled になるまで skip する。
    """
    now = now or dt.datetime.now(tz=JST)
    candidates = list_jobs(status_filter="queued")
    if not candidates:
        return None
    candidates.sort(key=lambda j: j["submitted_at"])

    all_jobs = list_jobs()  # 全ステータス込み(main 完了確認用)
    for job in candidates:
        if job.get("label", "").startswith("short-"):
            # 同 slug の main が scheduled になっているか確認
            main_done = any(
                m for m in all_jobs
                if m["slug"] == job["slug"]
                and m.get("label") == "main"
                and m.get("status") == "scheduled"
            )
            if not main_done:
                continue  # 本編未完了 → この shorts は後回し
        return job
    return None


def _label_to_upload_args(label: str) -> list[str]:
    """label ("main" / "short-<variant>") を upload_youtube.py の positional 引数に変換する。"""
    if label == "main":
        return ["main"]
    # "short-a" → "shorts" + "--variant" "a"
    variant = label.split("-")[-1]  # "a" / "b" / "c"
    return ["shorts", "--variant", variant]


def _run_job(job: dict) -> None:
    """1件のジョブ(本編またはShorts1本)をアップロードする。

    upload_youtube.py を呼び出し、結果を job dict に記録する。
    label "main" → upload_youtube.py --topic <slug> main [--publish-at <iso>]
    label "short-X" → upload_youtube.py --topic <slug> shorts --variant X [--publish-at <iso>]
    (--publish-at は job["publish_at"] がある場合だけ渡す。現行の submit は None)
    """
    job_id = job["id"]
    label = job.get("label", job.get("kind", "main"))  # 旧フォーマット後方互換
    log_path = LOG_DIR / f"{job_id}.log"
    job["status"] = "uploading"
    job["started_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    job["log"] = str(log_path.relative_to(ROOT))
    _write_job(job)

    upload_args = _label_to_upload_args(label)

    # shorts ジョブ: 同 slug の main(scheduled)から video_id を取得して --main-video-id に渡す
    main_video_id: str | None = None
    if label.startswith("short-"):
        for m in list_jobs():
            if (m["slug"] == job["slug"]
                    and m.get("label") == "main"
                    and m.get("status") == "scheduled"
                    and m.get("video_id")):
                main_video_id = m["video_id"]
                break

    # channel を job から解決(channel フィールドがない旧フォーマットは環境変数に委譲)
    job_channel = job.get("channel")

    cmd = [
        str(PY), str(_HERE / "upload_youtube.py"),
        "--topic", job["slug"],
        *upload_args,
    ]
    # 仕様変更 — publish_at が None なら --publish-at 渡さず、
    # privacy=private で即時 upload(公開予約は人間が YouTube Studio で手動設定)。
    # 後方互換: 既存 jobs (publish_at が ISO 文字列) はそのまま予約公開する。
    if job.get("publish_at"):
        cmd.extend(["--publish-at", job["publish_at"]])
    if job_channel:
        cmd.extend(["--channel", job_channel])
    if main_video_id:
        cmd.append(f"--main-video-id={main_video_id}")
    print(f"[upq] ▶ {job_id}  label={label}  publish={job['publish_at_jst']}", flush=True)
    print(f"      cmd: {' '.join(cmd)}", flush=True)
    log_f = log_path.open("w", encoding="utf-8")
    log_f.write(f"# job_id: {job_id}\n# slug:   {job['slug']}\n# label:  {label}\n"
                f"# publish_at: {job['publish_at']} ({job['publish_at_jst']})\n"
                f"# start: {job['started_at']}\n\n")
    log_f.flush()

    try:
        # upload_youtube.py のハードガード突破に env 必須
        env = {**os.environ, "UPLOAD_VIA_QUEUE": "1"}
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_f, stderr=subprocess.STDOUT, env=env)
        job["pid"] = proc.pid
        _write_job(job)
        try:
            rc = proc.wait(timeout=JOB_TIMEOUT_SEC)
            job["exit_code"] = rc
            job["status"] = "scheduled" if rc == 0 else "failed"
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            job["exit_code"] = -1
            job["status"] = "timeout"
    except Exception as e:
        log_f.write(f"\n[upq exception] {e}\n")
        job["exit_code"] = -2
        job["status"] = "failed"
    finally:
        # upload_youtube.py が出力する output/upload_<label>.json を読んで video_id を保存
        safe_label = label.replace("-", "_")  # "short-a" → "short_a"
        info_path = _videos_root() / job["slug"] / "output" / f"upload_{safe_label}.json"
        if info_path.exists():
            try:
                info = json.loads(info_path.read_text())
                job["video_id"] = info.get("video_id")
                job["url"] = info.get("url")
            except Exception:
                pass
        job["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        log_f.write(f"\n# end: {job['ended_at']} status={job['status']} rc={job['exit_code']}\n")
        log_f.close()
        _write_job(job)
        print(f"[upq] ◀ {job_id}  status={job['status']}  url={job.get('url')}", flush=True)
        # upload_youtube.py(subprocess)の _update_progress はすでに呼ばれているが、
        # bypass 経路や subprocess クラッシュ時のフォールバックとして worker 側でも保険呼び出しする。
        # main / shorts / vertical いずれも成功(scheduled)のときに kind 別フィールドを書く。
        if job.get("status") == "scheduled" and job.get("url"):
            # label は "main" / "shorts" / "vertical" / "short-a" 等。
            # _update_progress の kind は main / shorts / vertical の3種を想定するため、
            # short-a/short-b/short-c は shorts に正規化。
            if label.startswith("short"):
                _kind = "shorts"
            elif label in ("main", "vertical"):
                _kind = label
            else:
                _kind = label  # 想定外も生 label を渡す
            try:
                # upload_youtube._update_progress をインポートして呼ぶ
                import importlib.util as _ilu
                _spec = _ilu.spec_from_file_location(
                    "upload_youtube", str(_HERE / "upload_youtube.py")
                )
                _mod = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
                _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
                _mod._update_progress(
                    job.get("channel"),
                    job["slug"],
                    job["url"],
                    kind=_kind,
                )
            except Exception as _e:
                print(f"[upq][warn] _progress.yaml fallback update failed (non-fatal): {_e}", flush=True)


def worker_loop(once: bool = False) -> None:
    handle = _try_acquire_worker_lock()
    if handle is None:
        print("[upq] 既に別の upload worker が稼働中、終了します", file=sys.stderr)
        sys.exit(1)

    print(f"[upq] start pid={os.getpid()} queue={QUEUE_DIR}", flush=True)
    stop = {"flag": False}

    def _on_sig(signum, _frame):
        print(f"[upq] received signal {signum}, draining...", flush=True)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)

    while not stop["flag"]:
        job = _pick_next_job()
        if job is None:
            if once:
                break
            time.sleep(POLL_INTERVAL)
            continue
        _run_job(job)

    print("[upq] exit", flush=True)


# ---------- CLI ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("submit")
    p.add_argument("--slug", required=True, help="動画スラッグ(本編+短尺を一括投入)")
    p.add_argument("--channel", default=None, help="チャンネル名(省略時は .current_channel / CHANNEL env で自動解決)")

    sub.add_parser("worker")
    sub.add_parser("once")

    pl = sub.add_parser("list")
    pl.add_argument("--status", default=None)

    ps = sub.add_parser("status")
    ps.add_argument("job_id")

    sub.add_parser("schedule")  # 次の空き publish スロットを確認

    args = ap.parse_args()

    if args.cmd == "submit":
        try:
            jobs = submit(args.slug, channel=getattr(args, "channel", None))
        except Exception as e:
            print(f"submit failed: {e}", file=sys.stderr)
            sys.exit(2)
        print(json.dumps([{
            "job_id": j["id"], "slug": j["slug"], "label": j["label"],
            "publish_at_jst": j["publish_at_jst"], "status": j["status"],
        } for j in jobs], ensure_ascii=False, indent=2))
    elif args.cmd == "worker":
        worker_loop()
    elif args.cmd == "once":
        worker_loop(once=True)
    elif args.cmd == "list":
        for j in list_jobs(args.status):
            label = j.get("label", j.get("kind", "?"))
            print(f"{j['status']:10s} {label:8s} {j['publish_at_jst']:25s} "
                  f"{j['slug']:30s} {j['id']}")
    elif args.cmd == "status":
        p = _job_path(args.job_id)
        if not p.exists():
            print(f"not found: {args.job_id}", file=sys.stderr)
            sys.exit(1)
        print(p.read_text())
    elif args.cmd == "schedule":
        slot = _next_publish_slot()
        print(f"  next slot: {slot.strftime('%Y-%m-%d %H:%M JST')}")


if __name__ == "__main__":
    main()
