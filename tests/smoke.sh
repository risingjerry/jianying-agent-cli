#!/usr/bin/env bash
# jy smoke test: synthetic clips -> every verb -> jy.v1 envelope assertions.
#
# The test creates its own media in a temp dir (no network, no GUI, no fixtures
# from the repo). It is machine-aware: the subtitle case is skipped with a
# warning only when no installed ffmpeg build has libass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JY="$ROOT/bin/jy"
PY="${PYTHON:-/opt/homebrew/bin/python3}"
[[ -x "$PY" ]] || PY="$(command -v python3)"
[[ -x "$PY" ]] || { echo "smoke: no python3 found" >&2; exit 1; }

# Deterministic discovery for the test run; the CLI itself honours these vars.
unset JY_FFMPEG JY_FFPROBE

FFMPEG_FULL="/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
FF="${FFMPEG_FULL}"
[[ -x "$FF" ]] || FF="$(command -v ffmpeg || true)"
[[ -n "$FF" ]] || { echo "smoke: no ffmpeg available" >&2; exit 1; }
FFPROBE_FULL="/opt/homebrew/opt/ffmpeg-full/bin/ffprobe"
FP="${FFPROBE_FULL}"
[[ -x "$FP" ]] || FP="$(command -v ffprobe || true)"
[[ -n "$FP" ]] || { echo "smoke: no ffprobe available" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/jy-smoke.XXXXXX")"
export JY_CACHE_DIR="$WORK/cache"
export JY_STAGING_DIR="$WORK/staging"
export JY_CONFIG_DIR="$WORK/config"   # synthetic device id + fetched-material names stay in the temp dir
export JY_LIVE_OFFLINE=1              # no live 剪映 catalog calls unless a test opts in
OUTDIR="$WORK/out dir"          # spaces test argv + filter path escaping
OUT="$WORK/envelope.json"
ERR="$WORK/stderr.log"
PASS=0
FAIL=0
SKIP=0
RC=0

cleanup() {
    if [[ "$FAIL" -eq 0 ]]; then
        rm -rf "$WORK"
    else
        echo "smoke: artifacts kept for debugging: $WORK"
    fi
}
trap cleanup EXIT

cd "$WORK" || exit 1
mkdir -p "$OUTDIR"

cat > "$WORK/check.py" <<'PY'
"""Assertions over one jy.v1 envelope; prints a short detail line on success."""
import hashlib
import json
import os
import sys


def fail(message):
    print(message)
    raise SystemExit(1)


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        fail(f"cannot read envelope: {exc}")
    try:
        document, end = json.JSONDecoder().raw_decode(raw.lstrip())
    except json.JSONDecodeError as exc:
        fail(f"stdout is not one JSON object: {exc}")
    if raw.lstrip()[end:].strip():
        fail("stdout has extra bytes after the JSON object")
    if not isinstance(document, dict):
        fail("envelope is not a JSON object")
    return document


MODE = sys.argv[1]
DOC = load(sys.argv[2])
DATA = DOC.get("data") or {}
ERROR = DOC.get("error") or {}


def expect_success():
    if DOC.get("schema") != "jy.v1":
        fail("schema is not jy.v1")
    if DOC.get("ok") is not True:
        fail(f"ok=false code={ERROR.get('code')} message={ERROR.get('message')}")
    if DOC.get("error") is not None:
        fail("error must be null when ok=true")
    if DOC.get("outcome") not in ("readonly", "preview", "executed", "noop", "unknown"):
        fail(f"bad outcome {DOC.get('outcome')!r}")
    return DATA.get("output") or {}


def output_path(output):
    path = output.get("path")
    if not path or not os.path.isfile(path):
        fail(f"output file missing: {path}")
    if not (output.get("size_bytes") or 0) > 0:
        fail("output size_bytes is zero")
    return path


if MODE in ("ok", "output", "preview"):
    output = expect_success()
    if MODE == "output":
        path = output_path(output)
        if not output.get("codec"):
            fail("output codec missing")
        print(f"codec={output['codec']} dur={output.get('duration_s')} file={os.path.basename(path)}")
    elif MODE == "preview":
        if DOC.get("outcome") != "preview":
            fail(f"outcome={DOC.get('outcome')}, expected preview")
        argv = DATA.get("ffmpeg_argv")
        if not isinstance(argv, list) or not argv:
            fail("data.ffmpeg_argv is empty")
        planned = DATA.get("output_path")
        if not planned:
            fail("data.output_path missing")
        if os.path.exists(planned):
            fail(f"dry-run created {planned}")
        print(f"argv_len={len(argv)} no_output=1")
    else:
        print(f"outcome={DOC.get('outcome')}")

elif MODE == "error":
    expected_code = sys.argv[3]
    if DOC.get("ok") is not False:
        fail("expected ok=false")
    if DOC.get("data") is not None:
        fail("data must be null on failure")
    if ERROR.get("code") != expected_code:
        fail(f"code={ERROR.get('code')}, expected {expected_code}")
    if not isinstance(ERROR.get("details"), dict):
        fail("error.details must be an object")
    print(f"code={expected_code}")

elif MODE == "duration":
    low, high = float(sys.argv[3]), float(sys.argv[4])
    duration = (DATA.get("output") or {}).get("duration_s")
    if duration is None:
        fail("output duration_s is null")
    if not low <= float(duration) <= high:
        fail(f"duration {duration}s outside [{low}, {high}]")
    print(f"dur={float(duration):.2f}s")

elif MODE == "dims":
    want_w, want_h = int(sys.argv[3]), int(sys.argv[4])
    output = DATA.get("output") or {}
    got = (output.get("width"), output.get("height"))
    if got != (want_w, want_h):
        fail(f"dims {got[0]}x{got[1]}, expected {want_w}x{want_h}")
    print(f"dims={want_w}x{want_h}")

elif MODE == "codec":
    want = sys.argv[3]
    got = (DATA.get("output") or {}).get("codec")
    if got != want:
        fail(f"codec={got}, expected {want}")
    print(f"codec={want}")

elif MODE == "faststart":
    path = output_path(DATA.get("output") or {})
    with open(path, "rb") as handle:
        head = handle.read(256 * 1024)
    moov = head.find(b"moov")
    mdat = head.find(b"mdat")
    if moov < 0:
        fail("moov atom not found in the first 256 KiB")
    if 0 <= mdat < moov:
        fail("mdat precedes moov: faststart did not run")
    print("faststart=moov-first")

elif MODE == "compatible":
    concat = DATA.get("concat") or {}
    if concat.get("compatible") is not True:
        fail(f"concat.compatible={concat.get('compatible')} mismatches={concat.get('mismatches')}")
    print(f"compatible=1 key={concat.get('key')}")

elif MODE == "incompatible":
    concat = DATA.get("concat") or {}
    if concat.get("compatible") is not False:
        fail("expected concat.compatible=false")
    if not concat.get("mismatches"):
        fail("expected concat.mismatches to be populated")
    print(f"incompatible=1 mismatches={len(concat['mismatches'])}")

elif MODE == "doctor":
    selected = DATA.get("selected") or {}
    if not selected.get("ffmpeg"):
        fail("doctor.selected.ffmpeg is null")
    matrix = DATA.get("capability_matrix") or {}
    if not matrix.get("subtitles"):
        fail("no candidate exposes the subtitles filter")
    candidates = [row for row in DATA.get("candidates") or [] if row.get("exists")]
    if len(candidates) >= 2:
        has_plain = any("subtitles" in (row.get("missing_filters") or []) for row in candidates)
        if not has_plain:
            fail("expected one installed build to lack subtitles")
    print(f"selected={selected['ffmpeg']} builds={len(candidates)}")

elif MODE == "has_filter":
    name = sys.argv[3]
    if not (DATA.get("capability_matrix") or {}).get(name):
        fail(f"no candidate has filter {name}")
    print("available=1")

elif MODE == "effects":
    effects = DATA.get("effects") or {}
    missing = [
        name
        for name in ("libass", "ass", "subtitles")
        if not (effects.get(name) or {}).get("available")
    ]
    if missing:
        fail(f"doctor.effects missing: {missing}")
    xfade = effects.get("xfade") or {}
    if not xfade.get("available"):
        print("libass=1 xfade=absent")
    else:
        count = xfade.get("transition_count") or 0
        if count < 50:
            fail(f"doctor.effects.xfade.transition_count={count}")
        print(f"libass=1 xfade={count}")

elif MODE == "transition_list":
    count = DATA.get("count") or 0
    names = DATA.get("transitions") or []
    if count < 50 or len(names) != count:
        fail(f"transition count={count} names={len(names)}")
    if "fade" not in names:
        fail("fade missing from the transition list")
    print(f"count={count}")

elif MODE == "pixfmt":
    want = sys.argv[3]
    got = (DATA.get("output") or {}).get("pix_fmt")
    if got != want:
        fail(f"pix_fmt={got}, expected {want}")
    print(f"pix_fmt={want}")

elif MODE == "events_min":
    want = int(sys.argv[3])
    got = DATA.get("events") or 0
    if got < want:
        fail(f"events={got}, expected >= {want}")
    print(f"events={got}")

elif MODE == "segments":
    want = int(sys.argv[3])
    segments = DATA.get("segments") or []
    if len(segments) != want:
        fail(f"segments={len(segments)}, expected {want}")
    print(f"segments={want}")

elif MODE == "wrapped":
    if DATA.get("wrapped") is not True:
        fail(f"wrapped={DATA.get('wrapped')}, expected true")
    print("wrapped=1")

elif MODE == "honest":
    note = DATA.get("note") or ""
    if "VESDK" not in note:
        fail("data.note does not point at the 剪映/VESDK engine")
    if "face_aware" in DATA and DATA["face_aware"] is not False:
        fail("data.face_aware must be false")
    print("honest=1")

elif MODE == "probe_rotation":
    want_rotation, want_w, want_h = (int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))
    files = DATA.get("files") or []
    if not files:
        fail("no probed file")
    got = (files[0].get("rotation"), files[0].get("width"), files[0].get("height"))
    if got != (want_rotation, want_w, want_h):
        fail(f"rotation/dims {got}, expected {(want_rotation, want_w, want_h)}")
    print(f"rotation={want_rotation} dims={want_w}x{want_h}")

elif MODE == "jianying_state":
    state = DATA.get("jianying") or {}
    if not isinstance(state, dict) or "running" not in state:
        fail("doctor.data.jianying missing (expected running/descriptor/film-export)")
    for key in ("descriptor_found", "health_ok"):
        if key not in state:
            fail(f"doctor.data.jianying.{key} missing")
    film = state.get("film_export") or {}
    if "responded" not in film:
        fail("doctor.data.jianying.film_export.responded missing")
    print(f"running={int(bool(state.get('running')))} port={state.get('bridge_port')} "
          f"film_export={int(bool(film.get('responded')))}")

elif MODE == "jianying_up":
    state = DATA.get("jianying") or {}
    film = state.get("film_export") or {}
    if not (state.get("running") and state.get("health_ok") and film.get("responded")):
        fail(f"剪映 native bridge not ready: running={state.get('running')} "
             f"health={state.get('health_ok')} film_export={film.get('responded')}")
    print("up=1")

elif MODE == "draft":
    want_duration = float(sys.argv[4]) if len(sys.argv) > 4 else None
    path = DATA.get("draft_path")
    if not path or not os.path.isdir(path):
        fail(f"draft_path is not a directory: {path}")
    info = os.path.join(path, "draft_info.json")
    with open(info, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    if DATA.get("draft_sha256") != digest or len(digest) != 64:
        fail("draft_sha256 does not match draft_info.json")
    counts = {track.get("type"): track.get("segments") for track in DATA.get("tracks") or []}
    if counts.get("video") != 2 or counts.get("text") != 1:
        fail(f"unexpected track counts {counts}")
    canvas = DATA.get("canvas") or {}
    if (canvas.get("width"), canvas.get("height")) != (1280, 720):
        fail(f"canvas {canvas}")
    if want_duration is not None:
        duration = DATA.get("duration_s") or 0
        if abs(duration - want_duration) > 0.25:
            fail(f"draft duration {duration}s, expected ~{want_duration}s")
    print(f"sha={digest[:8]} tracks=video:2,text:1 dur={DATA.get('duration_s')}s")

elif MODE == "hardening":
    registry = DATA.get("registry") or {}
    if registry.get("errors"):
        fail(f"verb registry errors: {registry['errors']}")
    if (registry.get("verbs") or 0) < 30:
        fail(f"registry.verbs={registry.get('verbs')}")
    if not isinstance(DATA.get("jobs"), dict):
        fail("doctor.data.jobs missing")
    if not isinstance(DATA.get("asr"), dict):
        fail("doctor.data.asr missing")
    if not isinstance(DATA.get("hdr"), dict):
        fail("doctor.data.hdr missing")
    print(f"verbs={registry.get('verbs')} registry_errors=0")

elif MODE == "entitlements":
    entitlements = DATA.get("entitlements") or {}
    if entitlements.get("tier_readable_offline") is not False:
        fail("doctor.entitlements.tier_readable_offline must be false")
    tiers = entitlements.get("tiers") or []
    if "VIP" not in tiers or "SVIP" not in tiers:
        fail(f"tiers={tiers}, expected VIP and SVIP")
    reason = entitlements.get("reason") or ""
    if "服务端" not in reason:
        fail("reason does not say the tier is server-side")
    if "vipRemindViewedInfo.ini" not in " ".join(entitlements.get("checked") or []):
        fail("checked does not mention the UI-only config file")
    print(f"tiers={','.join(tiers)} offline=false")

elif MODE == "focus_ready":
    focus = DATA.get("focus") or {}
    if focus.get("provider") != "lsappinfo/open":
        fail(f"focus.provider={focus.get('provider')!r}")
    if "frontmost" not in focus:
        fail("focus.frontmost missing")
    print(f"focus_available={focus.get('available')}")

elif MODE == "jobs_state":
    if not isinstance(DATA.get("cache_root"), str) or not DATA["cache_root"]:
        fail("jobs.cache_root missing")
    if not isinstance(DATA.get("jobs"), list):
        fail("jobs.jobs is not a list")
    print(f"jobs={DATA.get('count')} running={DATA.get('running')}")

elif MODE == "job_submitted":
    job = DATA.get("job") or {}
    if DATA.get("async") is not True:
        fail("data.async is not true")
    if not str(job.get("id", "")).startswith("j-"):
        fail(f"job id looks wrong: {job.get('id')!r}")
    if job.get("state") not in ("queued", "running"):
        fail(f"job state={job.get('state')!r}")
    print(f"job={job['id']} state={job['state']}")

elif MODE == "job_id":
    want = sys.argv[3]
    got = (DATA.get("job") or {}).get("id")
    if got != want:
        fail(f"job id={got}, expected {want}")
    print(f"job={want}")

elif MODE == "job_output_duration":
    low, high = float(sys.argv[3]), float(sys.argv[4])
    job = DATA.get("job") or {}
    output = DATA.get("output") or (job.get("result", {}).get("data") or {}).get("output") or {}
    duration = output.get("duration_s")
    if duration is None:
        fail("job output duration is null")
    if not low <= float(duration) <= high:
        fail(f"job output duration {duration}s outside [{low}, {high}]")
    print(f"dur={float(duration):.2f}s")

elif MODE == "job_state":
    allowed = sys.argv[3].split(",")
    state = (DATA.get("job") or {}).get("state")
    if state not in allowed:
        fail(f"job state={state!r}, expected one of {allowed}")
    print(f"state={state}")

elif MODE == "clean_report":
    summary = DATA.get("summary") or {}
    want_dry = sys.argv[3] == "dry"
    minimum = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    if bool(summary.get("dry_run")) is not want_dry:
        fail(f"clean summary.dry_run={summary.get('dry_run')}, expected {want_dry}")
    if (summary.get("removed") or 0) < minimum:
        fail(f"clean removed={summary.get('removed')}, expected >= {minimum}")
    print(f"removed={summary.get('removed')} bytes={summary.get('freed_bytes')}")

elif MODE == "jobs_count":
    want = int(sys.argv[3])
    got = DATA.get("count")
    if got != want:
        fail(f"jobs.count={got}, expected {want}")
    print(f"jobs={got}")

elif MODE == "jobs_count_ge":
    want = int(sys.argv[3])
    got = DATA.get("count")
    if not isinstance(got, int) or got < want:
        fail(f"jobs.count={got}, expected >= {want}")
    print(f"jobs={got}")

elif MODE == "preflight_job":
    result = DATA.get("result") or {}
    if result.get("ok") is not True:
        fail("preflight job result is not ok")
    if result.get("outcome") != "readonly":
        fail(f"preflight job outcome={result.get('outcome')!r}")
    output = DATA.get("output")
    if output is not None:
        fail("preflight job reported an output file")
    print("preflight_job=readonly")

elif MODE == "render_preflight":
    preflight = DATA.get("preflight") or {}
    focus_state = DATA.get("focus") or {}
    lock = DATA.get("lock") or {}
    if DOC.get("outcome") != "readonly":
        fail(f"outcome={DOC.get('outcome')!r}, expected readonly")
    state = (DATA.get("bridge_state") or {}).get("state")
    if state not in ("ok", "wedged", "not_running"):
        fail(f"bridge_state.state={state!r}, expected ok|wedged|not_running")
    if preflight.get("health_ok") is not (state == "ok"):
        fail(f"health_ok={preflight.get('health_ok')} inconsistent with state={state}")
    if focus_state.get("provider") != "lsappinfo/open":
        fail("preflight focus provider missing")
    if lock.get("held") is not False:
        fail("preflight must not hold the render lock")
    cold = DATA.get("cold_start") or {}
    if not isinstance(cold.get("detected"), bool):
        fail("preflight cold_start.detected missing")
    if focus_state.get("default_mode") != "watch":
        fail(f"focus default_mode={focus_state.get('default_mode')!r}")
    print(f"health_ok=1 route={(preflight.get('route') or {}).get('responded')} "
          f"cold={cold.get('detected')} uptime={cold.get('app_uptime_s')}")

elif MODE == "render_guard":
    guard = DATA.get("focus_guard") or {}
    lock = DATA.get("lock") or {}
    if guard.get("enabled") is not True:
        fail("focus_guard.enabled is not true")
    if guard.get("mode") != "watch":
        fail(f"default focus mode={guard.get('mode')!r}, expected watch")
    if not isinstance(guard.get("reactivations"), int):
        fail("focus_guard.reactivations missing")
    if not isinstance(lock.get("waited_ms"), int):
        fail("lock.waited_ms missing")
    if not isinstance(DATA.get("cold_start"), dict):
        fail("data.cold_start missing")
    print(f"mode={guard.get('mode')} reactivations={guard.get('reactivations')} "
          f"lock_wait={lock.get('waited_ms')}ms")

elif MODE == "render_retriable":
    error = ERROR or {}
    details = error.get("details") or {}
    if DOC.get("ok") is not False:
        fail("expected ok=false (剪映 busy/unreachable)")
    if error.get("code") != "UNAVAILABLE":
        fail(f"code={error.get('code')}, expected UNAVAILABLE")
    attempts = details.get("attempts") or []
    if len(attempts) < 2:
        fail(f"expected >= 2 bounded attempts, got {len(attempts)}")
    if not isinstance(details.get("focus_guard"), dict):
        fail("error.details.focus_guard missing")
    print(f"attempts={len(attempts)} retriable={error.get('retriable')}")

elif MODE == "cover":
    cover = DATA.get("cover") or {}
    if not isinstance(cover.get("stream_index"), int):
        fail("cover.stream_index missing")
    if cover.get("codec") not in ("mjpeg", "png"):
        fail(f"cover codec={cover.get('codec')!r}")
    print(f"cover_stream={cover['stream_index']} codec={cover['codec']}")

elif MODE == "alias_list":
    kind, min_aliases, min_raw = sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
    aliases = DATA.get("aliases") or []
    if len(aliases) < min_aliases:
        fail(f"{kind} alias_count={len(aliases)}, expected >= {min_aliases}")
    if not all(isinstance(item.get("fidelity"), str) for item in aliases):
        fail("an alias row has no fidelity")
    if min_raw and (DATA.get("count") or 0) < min_raw:
        fail(f"raw xfade count={DATA.get('count')}, expected >= {min_raw}")
    if not (DATA.get("fidelity_legend") or {}):
        fail("fidelity_legend missing")
    print(f"{kind}_aliases={len(aliases)} raw={DATA.get('count')}")

elif MODE == "resolution":
    requested, resolved, fidelity = sys.argv[3], sys.argv[4], sys.argv[5]
    resolution = DATA.get("resolution") or {}
    if resolution.get("requested") != requested:
        fail(f"requested={resolution.get('requested')!r}, expected {requested!r}")
    if resolution.get("resolved") != resolved:
        fail(f"resolved={resolution.get('resolved')!r}, expected {resolved!r}")
    if resolution.get("fidelity") != fidelity:
        fail(f"fidelity={resolution.get('fidelity')!r}, expected {fidelity!r}")
    if resolution.get("engine") != "local":
        fail(f"engine={resolution.get('engine')!r}")
    if not resolution.get("note"):
        fail("resolution.note is empty (must say what the approximation is)")
    print(f"{requested}->{resolved} {fidelity}")

elif MODE == "suggestions_contain":
    needle = sys.argv[3]
    details = ERROR.get("details") or {}
    if DOC.get("ok") is not False:
        fail("expected ok=false (unknown filter name)")
    if ERROR.get("code") != "VALIDATION":
        fail(f"code={ERROR.get('code')}, expected VALIDATION")
    suggestions = details.get("suggestions") or []
    if not any(needle in str(item) for item in suggestions):
        fail(f"suggestions={suggestions}, expected one containing {needle!r}")
    print(f"suggestions={len(suggestions)}")

elif MODE == "beats_file":
    path = sys.argv[3]
    count = DATA.get("count") or 0
    if count < 3:
        fail(f"only {count} beats detected")
    if not os.path.isfile(path):
        fail(f"beats file missing: {path}")
    lines = [line for line in open(path, encoding="utf-8").read().splitlines() if line.strip()]
    if len(lines) != count:
        fail(f"beats file has {len(lines)} lines but count={count}")
    values = [int(line) for line in lines]
    if values != sorted(values) or any(value <= 0 for value in values):
        fail("beats timestamps are not strictly increasing positives")
    print(f"beats={count} first={values[0]}ms")

elif MODE == "srt_export":
    output = DATA.get("output") or {}
    if not (output.get("size_bytes") or 0) > 0:
        fail("exported SRT is empty")
    if (DATA.get("cues") or 0) < 1:
        fail(f"cues={DATA.get('cues')}")
    print(f"cues={DATA.get('cues')} bytes={output['size_bytes']}")

elif MODE == "ok_or_unsupported":
    code = (ERROR or {}).get("code")
    if DOC.get("ok") is True:
        print("outcome=ok")
    elif code == "UNSUPPORTED":
        print("outcome=unsupported (honest)")
    else:
        fail(f"expected success or UNSUPPORTED, got ok=false code={code}")

elif MODE == "batch_preview":
    want = int(sys.argv[3])
    if DOC.get("outcome") != "preview":
        fail(f"outcome={DOC.get('outcome')!r}, expected preview")
    files = DATA.get("files") or []
    if len(files) != want:
        fail(f"planned files={len(files)}, expected {want}")
    if not all(item.get("ffmpeg_argv") for item in files):
        fail("a planned file has no ffmpeg_argv")
    if DATA.get("outdir") and os.path.isdir(DATA["outdir"]):
        made = [name for name in os.listdir(DATA["outdir"]) if name.endswith(".mp4")]
        if made:
            fail(f"dry-run wrote files: {made}")
    print(f"planned={want} no_output=1")

elif MODE == "batch_files":
    want = int(sys.argv[3])
    if (DATA.get("count") or 0) != want:
        fail(f"batch count={DATA.get('count')}, expected {want}")
    if (DATA.get("failed") or 0) != 0:
        fail(f"batch failed={DATA.get('failed')}")
    missing = [item["output"] for item in DATA.get("files") or [] if not os.path.isfile(item["output"])]
    if missing:
        fail(f"batch outputs missing: {missing}")
    print(f"batch={want} ok")

else:
    fail(f"unknown check mode {MODE}")
PY

# The checks live in an embedded Python file; compile it up front so a syntax
# error there aborts loudly instead of failing every assertion one by one.
if ! "$PY" -c 'import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)' \
        "$WORK/check.py" 2> "$WORK/check_syntax.err"; then
    echo "smoke: embedded check.py has a syntax error:" >&2
    sed -n '1,5p' "$WORK/check_syntax.err" >&2
    exit 1
fi

pass() { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
skip() { SKIP=$((SKIP + 1)); printf '  skip %s\n' "$1"; }
fail() {
    FAIL=$((FAIL + 1))
    printf '  FAIL %s\n' "$1"
    [[ -n "${2:-}" ]] && printf '       %s\n' "$2"
    if [[ -s "$ERR" ]]; then
        sed -n '1,3p' "$ERR" | sed 's/^/       stderr: /'
    fi
}
check() { "$PY" "$WORK/check.py" "$@" 2>&1; }

run_jy() { "$JY" "$@" > "$OUT" 2> "$ERR"; RC=$?; }

expect_ok() {
    local name="$1"; shift
    run_jy "$@"
    local info
    if [[ $RC -eq 0 ]] && info="$(check ok "$OUT")"; then
        pass "$name [$info]"
    else
        fail "$name" "rc=$RC ${info:-} stdout=$(head -c 240 "$OUT")"
    fi
}

expect_output() {
    local name="$1"; shift
    run_jy "$@"
    local info
    if [[ $RC -eq 0 ]] && info="$(check output "$OUT")"; then
        pass "$name [$info]"
    else
        fail "$name" "rc=$RC ${info:-} stdout=$(head -c 240 "$OUT")"
    fi
}

expect_preview() {
    local name="$1"; shift
    run_jy "$@"
    local info
    if [[ $RC -eq 0 ]] && info="$(check preview "$OUT")"; then
        pass "$name [$info]"
    else
        fail "$name" "rc=$RC ${info:-} stdout=$(head -c 240 "$OUT")"
    fi
}

expect_error() {
    local name="$1" code="$2" want_rc="$3"; shift 3
    run_jy "$@"
    local info
    if [[ $RC -eq $want_rc ]] && info="$(check error "$OUT" "$code")"; then
        pass "$name [rc=$RC $info]"
    else
        fail "$name" "expected rc=$want_rc code=$code; rc=$RC ${info:-} stdout=$(head -c 240 "$OUT")"
    fi
}

expect_ok_or_unsupported() { # ok=true OR a clean UNSUPPORTED both count as honest
    local name="$1"; shift
    run_jy "$@"
    local info
    if info="$(check ok_or_unsupported "$OUT")"; then
        pass "$name [$info]"
    else
        fail "$name" "rc=$RC ${info:-} stdout=$(head -c 240 "$OUT")"
    fi
}

expect_field() { # name mode [args...]
    local name="$1" mode="$2"; shift 2
    local info
    if info="$(check "$mode" "$OUT" "$@")"; then
        pass "$name [$info]"
    else
        fail "$name" "${info:-}"
    fi
}

echo "jy smoke test"
echo "  jy=$JY"
echo "  python=$PY"
echo "  ffmpeg=$FF"
echo "  work=$WORK"

# --- synthetic media -------------------------------------------------------
gen_clip() { # path size rate duration frequency
    "$FF" -v error \
        -f lavfi -i "testsrc2=size=$2:rate=$3:duration=$4" \
        -f lavfi -i "sine=frequency=$5:duration=$4" \
        -c:v libx264 -preset veryfast -crf 25 -pix_fmt yuv420p \
        -c:a aac -b:a 128k -shortest -y "$1"
}
gen_clip a.mp4 1280x720 30 3 440
gen_clip b.mp4 1280x720 30 3 880
gen_clip c.mp4 640x360 25 2 220
"$FF" -v error -display_rotation 90 -i a.mp4 -c copy -y rotated.mp4
printf '1\n00:00:00,000 --> 00:00:02,000\nhello jy\n\n' > "$WORK/sub title.srt"
printf '1\n00:00:00,000 --> 00:00:02,000\nthis subtitle line is far too long to fit\n\n' \
    > "$WORK/long.srt"
printf '700\n1200\n' > "$WORK/beats.txt"

# chroma-key fixture: pure green frame with a red box (no audio)
"$FF" -v error \
    -f lavfi -i "color=c=0x00FF00:s=640x360:r=30:d=2" \
    -f lavfi -i "color=c=0xFF0000:s=120x120:r=30:d=2" \
    -filter_complex "[0:v][1:v]overlay=x=100:y=100[v]" -map "[v]" \
    -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -y green.mp4
"$FF" -v error -f lavfi -i "color=c=0x2244cc:s=400x400" -frames:v 1 -y bg.jpg
# 2x2x2 identity and inverter .cube LUTs (no fixtures shipped in the repo)
printf 'TITLE "identity"\nLUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n' \
    > identity.cube
# local-verb fixtures: a red sticker, a click track, a generated speech clip
"$FF" -v error -f lavfi -i "color=c=0xFF0000:s=160x160" -frames:v 1 -y sticker.png
"$FF" -v error -f lavfi -i "color=c=0x00FF00:s=640x360:r=30:d=2" \
    -c:v libx264 -preset veryfast -pix_fmt yuv420p -y solid_green.mp4
"$FF" -v error -f lavfi \
    -i "aevalsrc='if(lt(mod(t,0.5),0.03),sin(2*PI*1000*t)*0.9,0)':s=44100:d=3" \
    -c:a aac -b:a 128k -y clicks.m4a
# PQ-tagged source: exercises jy hdr's HDR->SDR probe (ok or honest UNSUPPORTED)
"$FF" -v error -f lavfi -i "testsrc2=size=320x180:rate=25:duration=1" \
    -c:v libx264 -preset veryfast -pix_fmt yuv420p -color_primaries bt2020 \
    -color_trc smpte2084 -colorspace bt2020nc -y pq_src.mp4
say_ok=0
if command -v say > /dev/null 2>&1; then
    if say -o "$WORK/speech.aiff" "hello jy smoke test one two three" 2>/dev/null; then
        if "$FF" -v error -i "$WORK/speech.aiff" -c:a aac -b:a 128k -y speech.m4a 2>/dev/null; then
            say_ok=1
        fi
    fi
fi
cat > passthru.ass <<'ASS'
[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Hiragino Sans GB,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.20,0:00:02.00,Default,,0,0,0,,{\fad(200,200)}ass passthrough
ASS

echo
echo "== inspection"
expect_ok "doctor" doctor
cp "$OUT" "$WORK/doctor.json"
if [[ -s "$WORK/doctor.json" ]]; then
    expect_field "doctor: two builds + libass" doctor
    expect_field "doctor: effect capabilities (libass/xfade/...)" effects
fi
expect_ok "probe two identical clips" probe a.mp4 b.mp4
expect_field "probe: concat-copy compatible" compatible
expect_ok "probe mismatched clips" probe a.mp4 c.mp4
expect_field "probe: mismatch reported" incompatible
expect_ok "probe rotated clip" probe rotated.mp4
expect_field "probe: rotation=90 swaps dims" probe_rotation 90 720 1280
expect_error "probe: missing input" NOT_FOUND 1 probe missing.mp4

echo
echo "== edit"
expect_output "trim re-encode" trim a.mp4 --start 0.5 --end 2.0 -o "$OUTDIR/trim.mp4"
expect_field "trim: duration 1.5s" duration 1.2 1.8
expect_output "trim stream copy" trim a.mp4 --start 1 --duration 1 -o "$OUTDIR/trim_copy.mp4"
expect_field "trim copy: duration ~1s" duration 0.6 1.8
expect_output "concat copy (identical)" concat a.mp4 b.mp4 -o "$OUTDIR/cat_copy.mp4"
expect_field "concat copy: ~6s" duration 5.6 6.5
expect_field "concat copy: h264" codec h264
cp a.mp4 "a's clip.mp4"; cp b.mp4 "b's clip.mp4"
expect_output "concat copy (apostrophes in names)" concat "a's clip.mp4" "b's clip.mp4" \
    -o "$OUTDIR/cat_quote.mp4"
expect_field "concat quote: ~6s" duration 5.6 6.5
expect_output "concat re-encode (mismatched)" concat a.mp4 c.mp4 -o "$OUTDIR/cat_reenc.mp4"
expect_field "concat re-encode: ~5s" duration 4.5 5.6
expect_field "concat re-encode: normalised to input 0" dims 1280 720
expect_error "concat: forced copy rejects mismatch" VALIDATION 2 \
    concat a.mp4 c.mp4 --mode copy -o "$OUTDIR/nope.mp4"
expect_output "speed 2x" speed a.mp4 --factor 2 -o "$OUTDIR/speed.mp4"
expect_field "speed: duration ~1.5s" duration 1.2 1.9
expect_output "speed --no-audio" speed a.mp4 --factor 2 --no-audio -o "$OUTDIR/speed_noaudio.mp4"
expect_field "speed --no-audio: duration ~1.5s" duration 1.2 1.9
if [[ -z "$("$FP" -v error -select_streams a -show_entries stream=index -of csv=p=0 "$OUTDIR/speed_noaudio.mp4")" ]]; then
    pass "speed --no-audio: no audio stream"
else
    fail "speed --no-audio: audio stream still present"
fi
expect_output "extract-audio m4a" extract-audio a.mp4 -o "$OUTDIR/audio.m4a"
expect_field "extract m4a: aac" codec aac
expect_field "extract m4a: ~3s" duration 2.7 3.3
expect_output "extract-audio mp3" extract-audio a.mp4 -o "$OUTDIR/audio.mp3"
expect_field "extract mp3: mp3" codec mp3
expect_output "frame at 1.5s" frame a.mp4 --at 1.5 -o "$OUTDIR/frame.jpg"
expect_field "frame jpg: mjpeg 1280x720" codec mjpeg
expect_field "frame jpg: dims" dims 1280 720
expect_output "frame at 50%" frame a.mp4 --at 50% -o "$OUTDIR/frame_pct.png"
expect_field "frame png: png" codec png
expect_field "frame png: dims" dims 1280 720

echo
echo "== export"
expect_output "export 720p h264" export a.mp4 --resolution 720p -o "$OUTDIR/e720.mp4"
expect_field "export 720p: dims" dims 1280 720
expect_field "export 720p: duration ~3s" duration 2.7 3.3
expect_field "export 720p: h264" codec h264
expect_output "export 1080p h265" export a.mp4 --resolution 1080p --codec h265 --quality 26 \
    -o "$OUTDIR/e1080_h265.mp4"
expect_field "export h265: dims" dims 1920 1080
expect_field "export h265: hevc" codec hevc
expect_output "export 4k h264" export a.mp4 --resolution 4k --preset ultrafast --quality 30 \
    -o "$OUTDIR/e4k.mp4"
expect_field "export 4k: dims" dims 3840 2160
expect_output "export explicit 640x480" export c.mp4 --resolution 640x480 \
    -o "$OUTDIR/e640x480.mp4"
expect_field "export WxH: dims" dims 640 480
expect_output "export --hw (VideoToolbox)" export a.mp4 --resolution 720p --hw -o "$OUTDIR/ehw.mp4"
expect_field "export hw: dims" dims 1280 720
expect_output "export --faststart" export a.mp4 --resolution 720p --faststart -o "$OUTDIR/efast.mp4"
expect_field "export faststart: moov first" faststart
expect_preview "export --dry-run" export a.mp4 --resolution 720p --dry-run -o "$OUTDIR/edry.mp4"
expect_error "export: quality+bitrate exclusive" VALIDATION 2 \
    export a.mp4 --resolution 720p --quality 20 --bitrate 4M -o "$OUTDIR/eboth.mp4"
expect_error "export: overwrite refused" VALIDATION 2 \
    export a.mp4 --resolution 720p -o "$OUTDIR/e720.mp4"
expect_output "export: --force overwrites" export a.mp4 --resolution 720p --force \
    -o "$OUTDIR/e720.mp4"

echo
echo "== gif / subtitle"
expect_output "gif 320w 10fps" gif a.mp4 --start 0 --duration 2 --width 320 --fps 10 \
    -o "$OUTDIR/anim.gif"
expect_field "gif: gif codec" codec gif
expect_field "gif: dims 320x180" dims 320 180
expect_field "gif: duration 2s" duration 1.7 2.3

if [[ -s "$WORK/doctor.json" ]] && check has_filter "$WORK/doctor.json" subtitles > /dev/null 2>&1; then
    expect_output "subtitle burn-in (srt path with space)" subtitle a.mp4 \
        --srt "$WORK/sub title.srt" --font-size 36 --margin-v 24 -o "$OUTDIR/subbed.mp4"
    expect_field "subtitle: duration ~3s" duration 2.7 3.3
    expect_output "subtitle cinematic + top + wrap 12" subtitle a.mp4 --srt long.srt \
        --style cinematic --position top --max-chars-per-line 12 \
        -o "$OUTDIR/sub_cin.mp4"
    expect_field "subtitle: srt re-wrapped" wrapped
    expect_output "subtitle karaoke preset" subtitle a.mp4 --srt long.srt \
        --style karaoke --position center -o "$OUTDIR/sub_kara.mp4"
    expect_output "subtitle .ass passthrough" subtitle a.mp4 --srt passthru.ass \
        -o "$OUTDIR/sub_ass.mp4"
    expect_error "subtitle: wrap limit too small" VALIDATION 2 subtitle a.mp4 \
        --srt long.srt --max-chars-per-line 2 -o "$OUTDIR/sub_bad.mp4"
else
    skip "subtitle burn-in (no installed ffmpeg has libass)"
fi

echo
echo "== effects (transition / text / montage / filter / key / beautify)"
if [[ -s "$WORK/doctor.json" ]] \
    && check has_filter "$WORK/doctor.json" xfade > /dev/null 2>&1 \
    && check has_filter "$WORK/doctor.json" blend > /dev/null 2>&1; then
    expect_ok "transition --list" transition --list
    expect_field "transition --list: 50+ names" transition_list
    expect_output "transition fade (3s + 3s - 0.5s)" transition a.mp4 b.mp4 --type fade \
        --duration 0.5 -o "$OUTDIR/t_fade.mp4"
    expect_field "transition: duration ~5.5s" duration 5.4 5.7
    expect_field "transition: normalised to input 0" dims 1280 720
    expect_field "transition: yuv420p" pixfmt yuv420p
    expect_output "transition wipeleft (mismatched input)" transition a.mp4 c.mp4 \
        --type wipeleft --duration 0.4 -o "$OUTDIR/t_mixed.mp4"
    expect_field "transition mixed: dims from input 0" dims 1280 720
    expect_output "transition --offset 1.0 (1s + 3s = 4s)" transition a.mp4 b.mp4 \
        --type fade --duration 0.5 --offset 1.0 -o "$OUTDIR/t_offset.mp4"
    expect_field "transition offset: duration ~4s" duration 3.8 4.2
    expect_error "transition: unknown type" VALIDATION 2 transition a.mp4 b.mp4 \
        --type nosuch -o "$OUTDIR/t_bad.mp4"
    expect_error "transition: missing -o" VALIDATION 2 transition a.mp4 b.mp4
    expect_error "transition: clip shorter than transition" VALIDATION 2 \
        transition a.mp4 c.mp4 --duration 5 -o "$OUTDIR/t_long.mp4"

    if check has_filter "$WORK/doctor.json" ass > /dev/null 2>&1; then
        expect_output "text fadein (CJK + literal braces)" text a.mp4 \
            --content "标题{A,B}" --start 0 --duration 2 --position center \
            --font-size 64 --outline 3 --animation fadein -o "$OUTDIR/x_fade.mp4"
        expect_field "text: duration ~3s (whole clip)" duration 2.7 3.3
        expect_field "text: yuv420p" pixfmt yuv420p
        expect_output "text typewriter at X,Y" text a.mp4 --content "typewriter" \
            --position 100,600 --animation typewriter -o "$OUTDIR/x_type.mp4"
        expect_field "text: typewriter emits per-char events" events_min 5
        expect_output "text slideup top, hex colour" text a.mp4 --content "hello jy" \
            --position top --color "#FFCC00" --animation slideup -o "$OUTDIR/x_slide.mp4"
        expect_error "text: bad colour" VALIDATION 2 text a.mp4 --content hi \
            --color nope -o "$OUTDIR/x_bad.mp4"
        expect_error "text: position outside the frame" VALIDATION 2 text a.mp4 \
            --content hi --position 5000,5000 -o "$OUTDIR/x_oob.mp4"
    else
        skip "text verbs (no installed ffmpeg has libass)"
    fi

    expect_output "montage per-clip 1.5 + fade 0.3" montage a.mp4 b.mp4 c.mp4 \
        --per-clip 1.5 --transition fade --transition-duration 0.3 \
        -o "$OUTDIR/m_fade.mp4"
    expect_field "montage: 3*1.5 - 2*0.3 = 3.9s" duration 3.7 4.1
    expect_field "montage: normalised to input 0" dims 1280 720
    expect_field "montage: segments" segments 3
    expect_output "montage accepts 剪映 中文名 (叠化 -> fade)" montage a.mp4 b.mp4 \
        --per-clip 1.0 --transition 叠化 --transition-duration 0.3 -o "$OUTDIR/m_zh.mp4"
    expect_output "montage concat (no transition, audio kept)" montage a.mp4 b.mp4 \
        --per-clip 1.0 -o "$OUTDIR/m_cat.mp4"
    expect_field "montage concat: 2.0s" duration 1.8 2.2
    if [[ -n "$("$FP" -v error -select_streams a -show_entries stream=index -of csv=p=0 \
        "$OUTDIR/m_cat.mp4")" ]]; then
        pass "montage concat: audio stream preserved"
    else
        fail "montage concat: audio stream missing"
    fi
    expect_output "montage --sync-beats (0.7s + 0.5s)" montage a.mp4 b.mp4 \
        --sync-beats beats.txt -o "$OUTDIR/m_beats.mp4"
    expect_field "montage beats: 1.2s" duration 1.0 1.4
    expect_error "montage: beats need one clip per cut" VALIDATION 2 montage a.mp4 \
        --sync-beats beats.txt -o "$OUTDIR/m_bad.mp4"
    expect_error "montage: transition longer than segment" VALIDATION 2 \
        montage a.mp4 --per-clip 0.5 --transition fade --transition-duration 0.6 \
        -o "$OUTDIR/m_bad2.mp4"

    expect_output "filter vivid 60" filter a.mp4 --preset vivid --intensity 60 \
        -o "$OUTDIR/f_vivid.mp4"
    expect_field "filter: duration kept" duration 2.7 3.3
    expect_field "filter: dims kept" dims 1280 720
    expect_output "filter bw 100" filter a.mp4 --preset bw -o "$OUTDIR/f_bw.mp4"
    expect_output "filter --lut (identity cube, 80)" filter a.mp4 --lut identity.cube \
        --intensity 80 -o "$OUTDIR/f_lut.mp4"
    expect_error "filter: preset and lut are exclusive" VALIDATION 2 filter a.mp4 \
        --preset vivid --lut identity.cube -o "$OUTDIR/f_both.mp4"
    expect_error "filter: intensity out of range" VALIDATION 2 filter a.mp4 \
        --preset vivid --intensity 150 -o "$OUTDIR/f_over.mp4"

    expect_output "key green out over colour bg" key green.mp4 --color 00FF00 \
        --similarity 0.3 --blend 0.1 --bg color:black -o "$OUTDIR/k_black.mp4"
    expect_field "key: duration kept" duration 1.8 2.2
    expect_field "key: yuv420p" pixfmt yuv420p
    read -r k_r k_g k_b <<< "$("$FF" -v error -i "$OUTDIR/k_black.mp4" -frames:v 1 \
        -vf "crop=2:2:5:5,scale=1:1,format=rgb24" -f rawvideo - 2>/dev/null \
        | od -An -tu1 | tr -s ' ')"
    if [[ ${k_r:-999} -lt 40 && ${k_g:-999} -lt 40 && ${k_b:-999} -lt 40 ]]; then
        pass "key: green pixel replaced by black bg [rgb=$k_r,$k_g,$k_b]"
    else
        fail "key: green not removed" "corner rgb=$k_r,$k_g,$k_b"
    fi
    expect_field "key: honest note reports 色度抠图 only" honest
    expect_output "key green out over image bg" key green.mp4 --bg bg.jpg \
        -o "$OUTDIR/k_img.mp4"
    expect_field "key image bg: duration kept" duration 1.8 2.2
    expect_error "key: video file as bg rejected" VALIDATION 2 key green.mp4 \
        --bg a.mp4 -o "$OUTDIR/k_bad.mp4"

    expect_output "beautify (40/20/30)" beautify a.mp4 -o "$OUTDIR/b_def.mp4"
    expect_field "beautify: duration kept" duration 2.7 3.3
    expect_field "beautify: dims kept" dims 1280 720
    expect_field "beautify: face_aware=false + VESDK note" honest
    expect_error "beautify: strength out of range" VALIDATION 2 beautify a.mp4 \
        --smooth 101 -o "$OUTDIR/b_bad.mp4"
else
    skip "effects verbs (no installed ffmpeg has xfade+blend)"
fi

echo
echo "== 剪映 alias layer (transition / filter 中文名 + fidelity)"
expect_ok "transition --list (xfade + 剪映 aliases)" transition --list
expect_field "transition list: 40+ 剪映 names, 50+ xfade" alias_list transitions 40 50
expect_ok "filter --list (剪映 alias -> local preset)" filter --list
expect_field "filter list: 25+ 剪映 names" alias_list filters 25 0
expect_output "transition 叠化 (exact alias)" transition a.mp4 b.mp4 --type 叠化 \
    --duration 0.4 -o "$OUTDIR/alias_fade.mp4"
expect_field "叠化: fidelity exact, resolved fade" resolution 叠化 fade exact
expect_output "transition 闪白转场 (suffix + exact)" transition a.mp4 b.mp4 --type 闪白转场 \
    -o "$OUTDIR/alias_white.mp4"
expect_field "闪白: fidelity exact, resolved fadewhite" resolution 闪白转场 fadewhite exact
expect_output "transition 模糊 (close alias)" transition a.mp4 b.mp4 --type 模糊 \
    -o "$OUTDIR/alias_blur.mp4"
expect_field "模糊: fidelity close, resolved hblur" resolution 模糊 hblur close
expect_output "transition 拉远 (rough alias warns)" transition a.mp4 b.mp4 --type 拉远 \
    -o "$OUTDIR/alias_zoomout.mp4"
expect_field "拉远: fidelity rough, resolved zoomin" resolution 拉远 zoomin rough
if grep -q "rough" <<< "$("$PY" -c "
import json;d=json.load(open('$OUT'));print(' '.join(d.get('warnings') or []))")"; then
    pass "拉远: rough reported in warnings"
else
    fail "拉远: rough warning missing"
fi
expect_error "transition 星形 (no local approximation)" UNSUPPORTED 1 transition a.mp4 b.mp4 \
    --type 星形 -o "$OUTDIR/alias_star.mp4"
expect_error "transition typo -> suggestions" VALIDATION 2 transition a.mp4 b.mp4 \
    --type 闪自 -o "$OUTDIR/alias_typo.mp4"
expect_output "transition raw xfade still works (dissolve)" transition a.mp4 b.mp4 \
    --type dissolve -o "$OUTDIR/alias_raw.mp4"
expect_field "dissolve: matched_by xfade" resolution dissolve dissolve exact

expect_output "filter 黑白 (exact alias)" filter a.mp4 --preset 黑白 -o "$OUTDIR/alias_mono.mp4"
expect_field "黑白: local mono, fidelity exact" resolution 黑白 mono exact
expect_output "filter 日系 (close alias, intensity 80)" filter a.mp4 --preset 日系 \
    --intensity 80 -o "$OUTDIR/alias_jp.mp4"
expect_field "日系: local japanese, fidelity close" resolution 日系 japanese close
expect_output "filter 清晰 (chain approx)" filter a.mp4 --preset 清晰 -o "$OUTDIR/alias_clarity.mp4"
expect_field "清晰: local clarity, fidelity close" resolution 清晰 clarity close
expect_output "filter 马赛克 (pixelize alias)" filter a.mp4 --preset 马赛克 \
    -o "$OUTDIR/alias_mosaic.mp4"
expect_field "马赛克: fidelity exact" resolution 马赛克 mosaic exact
expect_output "filter 暗调 (curves+vignette alias)" filter a.mp4 --preset 暗调 \
    -o "$OUTDIR/alias_dark.mp4"
expect_output "filter 宽银幕 (drawbox bars)" filter a.mp4 --preset 变形宽银幕 \
    -o "$OUTDIR/alias_scope.mp4"
expect_output "filter 镜像 (flip approximation)" filter a.mp4 --preset 镜像 \
    -o "$OUTDIR/alias_mirror.mp4"
expect_field "镜像: fidelity rough (flip, not half-copy)" resolution 镜像 mirror rough
expect_error "filter 油画 (no local approximation)" UNSUPPORTED 1 filter a.mp4 \
    --preset 油画 -o "$OUTDIR/alias_oil.mp4"
expect_error "filter 电影感 (not a catalogue name) -> group suggestions" VALIDATION 2 \
    filter a.mp4 --preset 电影感 -o "$OUTDIR/alias_sense.mp4"
expect_field "电影感: suggests the 电影 group" suggestions_contain 暗调
expect_output "filter local preset untouched (vivid)" filter a.mp4 --preset vivid \
    -o "$OUTDIR/alias_vivid.mp4"
expect_field "vivid: matched_by local_preset" resolution vivid vivid exact

echo
echo "== local verbs (geometry / overlay / grading / audio / cover / batch / beats / asr)"
expect_output "reverse (video + audio)" reverse a.mp4 -o "$OUTDIR/rev.mp4"
expect_field "reverse: duration kept" duration 2.7 3.3
expect_output "rotate 90" rotate a.mp4 --degrees 90 -o "$OUTDIR/rot90.mp4"
expect_field "rotate 90: dims swapped" dims 720 1280
expect_output "rotate 270" rotate a.mp4 --degrees 270 -o "$OUTDIR/rot270.mp4"
expect_field "rotate 270: dims swapped" dims 720 1280
expect_output "rotate 180" rotate a.mp4 --degrees 180 -o "$OUTDIR/rot180.mp4"
expect_field "rotate 180: dims kept" dims 1280 720
expect_output "flip vertical" flip a.mp4 --axis v -o "$OUTDIR/flipv.mp4"
expect_field "flip: dims kept" dims 1280 720
expect_output "crop by aspect 9:16" crop a.mp4 --aspect 9:16 -o "$OUTDIR/crop916.mp4"
expect_field "crop 9:16: 404x720" dims 404 720
expect_output "crop explicit box" crop a.mp4 --width 320 --height 180 --x 10 --y 10 \
    -o "$OUTDIR/cropbox.mp4"
expect_field "crop box: dims" dims 320 180
expect_error "crop: box outside the frame" VALIDATION 2 crop a.mp4 --width 2000 \
    --height 200 -o "$OUTDIR/cropbad.mp4"

expect_output "overlay sticker (top-left window)" overlay solid_green.mp4 sticker.png \
    --position top-left --scale 0.25 --start 0 --duration 1 -o "$OUTDIR/ov.mp4"
expect_field "overlay: duration kept" duration 1.8 2.2
read -r ov_r ov_g ov_b <<< "$("$FF" -v error -ss 0.4 -i "$OUTDIR/ov.mp4" -frames:v 1 \
    -vf "crop=2:2:40:40,scale=1:1,format=rgb24" -f rawvideo - 2>/dev/null | od -An -tu1 | tr -s ' ')"
if [[ ${ov_r:-0} -gt 120 && ${ov_g:-255} -lt 160 ]]; then
    pass "overlay: sticker visible in the window [rgb=$ov_r,$ov_g,$ov_b]"
else
    fail "overlay: sticker not visible" "rgb=$ov_r,$ov_g,$ov_b"
fi
read -r ov2_r ov2_g ov2_b <<< "$("$FF" -v error -ss 1.5 -i "$OUTDIR/ov.mp4" -frames:v 1 \
    -vf "crop=2:2:40:40,scale=1:1,format=rgb24" -f rawvideo - 2>/dev/null | od -An -tu1 | tr -s ' ')"
if [[ ${ov2_g:-0} -gt 120 && ${ov2_r:-255} -lt 160 ]]; then
    pass "overlay: window ends (base visible again) [rgb=$ov2_r,$ov2_g,$ov2_b]"
else
    fail "overlay: window did not end" "rgb=$ov2_r,$ov2_g,$ov2_b"
fi
expect_output "overlay video-on-video" overlay a.mp4 b.mp4 --position center --scale 0.4 \
    --opacity 70 -o "$OUTDIR/ov2.mp4"
expect_error "overlay: bad position" VALIDATION 2 overlay a.mp4 sticker.png \
    --position nowhere -o "$OUTDIR/ovbad.mp4"

expect_output "adjust (7 parameters)" adjust a.mp4 --brightness 30 --contrast 20 \
    --saturation 40 --gamma 10 --temperature 30 --sharpness 30 --vignette 40 \
    -o "$OUTDIR/adj.mp4"
expect_field "adjust: dims kept" dims 1280 720
expect_field "adjust: duration kept" duration 2.7 3.3
expect_output "adjust: all zeros = plain re-encode" adjust a.mp4 -o "$OUTDIR/adj0.mp4"
expect_error "adjust: strength out of range" VALIDATION 2 adjust a.mp4 --brightness 101 \
    -o "$OUTDIR/adjbad.mp4"
expect_output "curves preset vintage" curves a.mp4 --preset vintage -o "$OUTDIR/cv1.mp4"
expect_output "curves explicit points" curves a.mp4 --points "0/0 0.5/0.7 1/1" \
    -o "$OUTDIR/cv2.mp4"
expect_error "curves: preset+points exclusive" VALIDATION 2 curves a.mp4 --preset vintage \
    --points "0/0 1/1" -o "$OUTDIR/cvbad.mp4"
expect_output "hsl hue 90 + saturation" hsl a.mp4 --hue 90 --saturation 0.4 -o "$OUTDIR/hsl.mp4"
expect_error "hsl: nothing to do" VALIDATION 2 hsl a.mp4 -o "$OUTDIR/hslbad.mp4"
expect_output "hdr 709 -> 2020 (SDR matrix)" hdr a.mp4 --from 709 --to 2020 \
    -o "$OUTDIR/hdr2020.mp4"
expect_ok_or_unsupported "hdr pq -> 709 tonemap (ok, or honest UNSUPPORTED)" hdr pq_src.mp4 \
    --from pq --to 709 -o "$OUTDIR/hdrpq.mp4"
expect_error "hdr: HDR output declined" UNSUPPORTED 1 hdr a.mp4 --from 709 --to pq \
    -o "$OUTDIR/hdrout.mp4"
expect_output "mix screen 60" mix a.mp4 solid_green.mp4 --mode screen --opacity 60 \
    -o "$OUTDIR/mix.mp4"
expect_field "mix: dims from base" dims 1280 720

expect_output "audio: volume+fades+loudnorm" audio a.mp4 --volume 1.5 --fade-in 0.3 \
    --fade-out 0.3 --loudnorm -o "$OUTDIR/au1.mp4"
expect_field "audio: duration kept" duration 2.7 3.3
expect_output "audio: bgm mix" audio a.mp4 --bgm clicks.m4a --bgm-volume 0.4 \
    -o "$OUTDIR/au2.mp4"
expect_output "audio: bgm duck" audio a.mp4 --bgm clicks.m4a --duck -o "$OUTDIR/au3.mp4"
expect_output "audio: bgm replace + denoise" audio a.mp4 --bgm clicks.m4a \
    --bgm-replace --denoise -o "$OUTDIR/au4.mp4"
expect_error "audio: nothing to do" VALIDATION 2 audio a.mp4 -o "$OUTDIR/aubad.mp4"

expect_output "cover: frame + attached_pic" cover a.mp4 --at 1 -o "$OUTDIR/cov.mp4" \
    --cover-out "$OUTDIR/cov.jpg"
expect_field "cover: mjpeg attached_pic stream" cover
expect_error "cover: overwrite refused" VALIDATION 2 cover a.mp4 --at 1 \
    -o "$OUTDIR/cov.mp4"

expect_ok "batch 2 inputs -> 480p fragments" batch a.mp4 b.mp4 -o "$WORK/frag" \
    --resolution 480p --start 0.2 --duration 1.0 --suffix _frag
expect_field "batch: 2 files, all ok" batch_files 2
expect_ok "batch --dry-run" batch a.mp4 b.mp4 -o "$WORK/frag_dry" --dry-run
expect_field "batch --dry-run: preview with 2 planned files" batch_preview 2
expect_error "batch: overwrite refused" VALIDATION 2 batch a.mp4 -o "$WORK/frag" \
    --resolution 480p --start 0.2 --duration 1.0 --suffix _frag

expect_ok "beats: onset detection on a click track" beats clicks.m4a -o "$WORK/clicks.txt" \
    --force
expect_field "beats: >=3 monotonic ms timestamps" beats_file "$WORK/clicks.txt"
head -2 "$WORK/clicks.txt" > "$WORK/two_beats.txt"
expect_output "montage --sync-beats from jy beats" montage a.mp4 b.mp4 \
    --sync-beats "$WORK/two_beats.txt" -o "$OUTDIR/m_beats2.mp4"
expect_field "montage from jy beats: ~1s" duration 0.7 1.3

expect_ok "subtitle --export-srt (srt -> srt, wrapped)" subtitle --srt long.srt \
    --export-srt "$WORK/exported.srt" --max-chars-per-line 10 --force
expect_field "export-srt: cues + bytes" srt_export
expect_ok "subtitle --export-srt (.ass -> srt)" subtitle --srt passthru.ass \
    --export-srt "$WORK/from_ass.srt" --force
expect_field "export-srt from .ass: cues" srt_export
expect_error "subtitle: --export-srt with -o refused" VALIDATION 2 subtitle --srt long.srt \
    --export-srt "$WORK/x.srt" -o "$OUTDIR/x.mp4"

if [[ "$say_ok" -eq 1 ]]; then
    expect_ok_or_unsupported "asr: local whisper.cpp -> SRT" asr speech.m4a --language en \
        -o "$WORK/asr.srt" --force
    if [[ -s "$WORK/asr.srt" ]]; then
        if grep -q -- "-->" "$WORK/asr.srt"; then
            pass "asr: SRT contains cues"
        else
            fail "asr: SRT has no cues"
        fi
    else
        skip "asr: no local model/binary (UNSUPPORTED, as designed)"
    fi
else
    skip "asr (no 'say' binary to synthesise speech)"
fi

echo
echo "== hardening (async jobs / focus guard / clean)"
expect_ok "doctor: registry + focus + jobs + asr + hdr" doctor
expect_field "doctor: no registry errors, 30+ verbs" hardening
expect_field "doctor: focus provider" focus_ready
expect_ok "jobs list" jobs list
expect_field "jobs: cache + list shape" jobs_state
expect_ok "async export submit (subcommand --async)" export a.mp4 --async \
    --resolution 480p -o "$OUTDIR/async_export.mp4"
JOB1="$("$PY" -c "import json,sys;print(json.load(open('$OUT'))['data']['job']['id'])")"
expect_field "async: job id + queued/running" job_submitted
expect_ok "jobs status" jobs status "$JOB1"
expect_field "jobs status: same id" job_id "$JOB1"
expect_ok "jobs wait (async export)" jobs wait "$JOB1" --timeout-ms 180000
expect_field "async: output duration ~3s" job_output_duration 2.7 3.3
expect_ok "async export submit (global --async)" --async export a.mp4 --resolution 480p \
    -o "$OUTDIR/async_export2.mp4"
expect_field "global --async: job id" job_submitted
JOB2="$("$PY" -c "import json,sys;print(json.load(open('$OUT'))['data']['job']['id'])")"
expect_ok "jobs wait (global --async)" jobs wait "$JOB2" --timeout-ms 180000
expect_ok "async 8k export (for the kill path)" export a.mp4 --async --resolution 8k \
    -o "$OUTDIR/async_kill.mp4"
JOB3="$("$PY" -c "import json,sys;print(json.load(open('$OUT'))['data']['job']['id'])")"
expect_ok "jobs kill" jobs kill "$JOB3"
expect_field "kill: job reached a terminal state" job_state killed,succeeded,failed
expect_ok "jobs list --state succeeded" jobs list --state succeeded
expect_field "jobs list: at least 2 succeeded" jobs_count_ge 2
mkdir -p "$JY_CACHE_DIR/drafts/smoke-scratch"
printf '{"schema":"jy.draft.v1","author":"jy"}\n' > "$JY_CACHE_DIR/drafts/smoke-scratch/.jy-draft.json"
echo "not a draft" > "$JY_CACHE_DIR/drafts/smoke-scratch/leftover.tmp"
expect_ok "clean --dry-run" clean --dry-run --older-than-hours 0
expect_field "clean: dry-run removed >= 1" clean_report dry 1
expect_ok "clean --older-than-hours 0" clean --older-than-hours 0
expect_field "clean: really removed >= 1" clean_report real 1
expect_ok "jobs list after clean" jobs list
expect_field "clean: job records swept" jobs_count 0

echo
echo "== jianying engine (剪映 draft -> native render)"
# Draft authoring is local JSON + ffprobe only: it must work on every machine.
# Rendering needs 剪映 running (native bridge); that part skips, never fails.
cat > "$WORK/jy_spec.json" <<'JSON'
{
  "canvas": {"width": 1280, "height": 720, "fps": 30},
  "clips": [
    {"path": "a.mp4", "start": 0, "duration": 1.5},
    {"path": "b.mp4", "start": 1.5, "duration": 1.5, "source_start": 0.5}
  ],
  "texts": [
    {"content": "JY SMOKE", "start": 0.2, "duration": 1.5,
     "style": {"color": "#FFD400", "alignment": "center", "bold": true}}
  ]
}
JSON
expect_ok "draft build (2 clips + 1 text)" draft build --spec jy_spec.json \
    -o "$WORK/jy draft"
expect_field "draft: sha256 + video:2/text:1 + 3.0s" draft "$WORK/jy draft" 3.0
if [[ -f "$WORK/jy draft/draft_info.json" ]]; then
    pass "draft: draft_info.json written"
else
    fail "draft: draft_info.json missing"
fi
if [[ -f "$WORK/jy draft/.jy-spec.json" ]]; then
    pass "draft: .jy-spec.json recorded (input for --on-entitlement local)"
else
    fail "draft: .jy-spec.json missing"
fi
expect_error "draft: refuses a non-jy directory" VALIDATION 2 draft build \
    --spec jy_spec.json -o "$OUTDIR"
expect_error "render: --engine=local is rejected" UNSUPPORTED 1 render \
    "$WORK/jy draft" -o "$OUTDIR/jy_local.mp4" --engine local
expect_ok "doctor (剪映 engine state)" doctor
expect_field "doctor: 剪映 running/descriptor/film-export" jianying_state
cp "$OUT" "$WORK/doctor_engine.json"   # later sections overwrite $OUT; gate on this
echo
echo "== cold-start retry (剪映's first export after a launch)"
cat > "$WORK/cold_check.py" <<'PY'
"""Cold-start policy: a wedged first export is retried once only when 剪映 is fresh."""
import sys
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
from jy_cli import jianying, jianying_commands as jc  # noqa: E402
from jy_cli.commands import Context  # noqa: E402

failures = []


def exercise(cold_start):
    calls = []

    def fake_render(root, temp, timeout):
        calls.append(1)
        raise jianying.BridgeError(
            "stuck", status=409, kind="http",
            body={"error": "native_export_window_close_timeout"},
        )

    original = jc._render_once
    jc._render_once = fake_render
    try:
        ctx = Context()
        args = SimpleNamespace(relaunch_app=False, relaunch_wait_ms=1000, cold_start=cold_start)
        attempts = []
        try:
            jc._render_with_recovery(ctx, args, "/tmp/draft", "/tmp/out.mp4", 5000, attempts)
        except Exception:
            pass
        return len(calls), attempts, ctx.warnings
    finally:
        jc._render_once = original


cold_calls, cold_attempts, cold_warnings = exercise(True)
warm_calls, warm_attempts, _ = exercise(False)
if cold_calls != 2:
    failures.append(f"cold start should retry exactly once, got {cold_calls} calls")
if not (cold_attempts and cold_attempts[0].get("cold_start_retry")):
    failures.append("cold-start retry not recorded in data.attempts")
if not any("冷启动" in warning for warning in cold_warnings):
    failures.append("cold-start warning missing")
if warm_calls != 1:
    failures.append(f"warm app must not retry a wedged export, got {warm_calls} calls")
uptime = jc._app_uptime_s()
if uptime is not None and uptime < 0:
    failures.append("app uptime looks wrong")
print({"ok": not failures, "failures": failures,
       "cold_calls": cold_calls, "warm_calls": warm_calls, "app_uptime_s": uptime})
PY
if cold_out="$("$PY" "$WORK/cold_check.py" "$ROOT" 2>&1)"; then
    pass "cold-start policy: one retry when fresh, none when warm [$(echo "$cold_out" | head -c 160)]"
else
    fail "cold-start policy" "$(echo "$cold_out" | tail -c 300)"
fi

echo
echo "== resilience (bridge health gate / relaunch hint / ASR model fallback)"
cat > "$WORK/resilience_check.py" <<'PY'
"""Prove jy fails fast when 剪映 is wedged, and that ASR survives a crashing model."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

JY, DRAFT, SPEECH, WORK = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, sys.argv[5])
failures = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}")


def run(args, env_extra=None, timeout=120):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    started = time.monotonic()
    done = subprocess.run([JY, *args], capture_output=True, text=True, env=env, timeout=timeout)
    elapsed = time.monotonic() - started
    try:
        payload = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"_raw": done.stdout[:200], "_stderr": done.stderr[:200]}
    return done.returncode, payload, elapsed


# --- fixtures: a stale descriptor whose pid is alive but whose port is dead
stale = Path(WORK) / "stale-bridge.json"
stale.write_text(
    json.dumps({"app": "test", "pid": os.getpid(), "port": 1, "token": "deadbeef", "version": 1}),
    "utf-8",
)
missing = Path(WORK) / "no-such-bridge.json"
if missing.exists():
    missing.unlink()

# --- doctor distinguishes wedged vs not_running
_, doctor_wedged, _ = run(["doctor"], {"JY_BRIDGE_DESCRIPTOR": str(stale)})
state = ((doctor_wedged.get("data") or {}).get("jianying") or {}).get("bridge_state") or {}
check("doctor wedged", state.get("state") == "wedged", str(state)[:160])
check("doctor wedged reachable false", state.get("reachable") is False, str(state)[:120])
check("doctor wedged remedy", "重启" in (state.get("remedy") or ""), state.get("remedy") or "")

_, doctor_missing, _ = run(["doctor"], {"JY_BRIDGE_DESCRIPTOR": str(missing)})
state2 = ((doctor_missing.get("data") or {}).get("jianying") or {}).get("bridge_state") or {}
check("doctor not_running", state2.get("state") == "not_running", str(state2)[:160])
check("doctor not_running remedy", "打开剪映" in (state2.get("remedy") or ""), state2.get("remedy") or "")

# --- render fast-fails (well under 5 s) instead of hanging on a wedged bridge
out = str(Path(WORK) / "wedge.mp4")
rc, payload, elapsed = run(
    ["render", DRAFT, "-o", out, "--force"],
    {"JY_BRIDGE_DESCRIPTOR": str(stale)},
    timeout=60,
)
error = payload.get("error") or {}
details = error.get("details") or {}
bridge = details.get("bridge") or {}
check("wedged render fails fast", elapsed < 5.0, f"{elapsed:.2f}s")
check("wedged render rc=1", rc == 1, f"rc={rc}")
check("wedged render UNAVAILABLE", error.get("code") == "UNAVAILABLE", str(error)[:160])
check("wedged render names the bridge", bridge.get("state") == "wedged", str(bridge)[:160])
check("wedged render reachable=false", bridge.get("reachable") is False, str(bridge)[:160])
check("wedged render hints --relaunch-app", "--relaunch-app" in (error.get("message") or ""), (error.get("message") or "")[:160])
check("wedged render did not write output", not os.path.exists(out), out)

# --- not_running is a different, equally fast answer
rc2, payload2, elapsed2 = run(
    ["render", DRAFT, "-o", out, "--force"],
    {"JY_BRIDGE_DESCRIPTOR": str(missing)},
    timeout=60,
)
error2 = payload2.get("error") or {}
check("not_running render fails fast", elapsed2 < 5.0, f"{elapsed2:.2f}s")
check("not_running render UNAVAILABLE", error2.get("code") == "UNAVAILABLE", str(error2)[:160])
check(
    "not_running says open the app",
    "没有运行" in (error2.get("message") or ""),
    (error2.get("message") or "")[:160],
)

# --- account_scope must not use the 10 s probe default while 剪映 is busy
from jy_cli import jianying  # noqa: E402

seen = {}
original = jianying._http


def spy(method, url, headers, body=None, timeout=30.0):
    seen[url.rsplit("/", 1)[-1]] = timeout
    return 200, {"authContext": {"principal": {"userId": "unit-test"}}}


jianying._http = spy
try:
    scope = jianying.Client(
        {"origin": "http://127.0.0.1:1", "port": 1, "pid": 0, "token": "x", "path": "x"},
        timeout=10.0,
    ).account_scope()
finally:
    jianying._http = original
check("account_scope returns uid", scope == "uid:unit-test", str(scope))
check("account_scope timeout >= 120s", seen.get("context", 0) >= 120.0, str(seen))

# --- ASR: a crashing model must fall back to the discovered default, and say so
asr_enabled = SPEECH != "-"
bogus = Path(WORK) / "bogus-model.ggml.bin"
bogus.write_text("not a ggml model\n", "utf-8")
srt = Path(WORK) / "asr_fallback.srt"
if not asr_enabled:
    print(json.dumps({"ok": not failures, "failures": failures,
                      "wedged_s": round(elapsed, 2), "not_running_s": round(elapsed2, 2),
                      "account_scope_timeout": seen.get("context"), "asr": "skipped"},
                     ensure_ascii=False))
    raise SystemExit(0)
rc3, payload3, _ = run(
    ["asr", SPEECH, "--model", str(bogus), "-o", str(srt), "--force"], timeout=300
)
data3 = payload3.get("data") or {}
if payload3.get("ok"):
    check("asr fell back to a real model", data3.get("model") != str(bogus), str(data3.get("model")))
    check("asr reports the requested model", data3.get("model_requested") == str(bogus), str(data3.get("model_requested")))
    check("asr records the fallback", bool(data3.get("model_fallback")), str(data3.get("model_fallback")))
    check(
        "asr warns about the crashing model",
        any("回退" in warning for warning in payload3.get("warnings") or []),
        str(payload3.get("warnings"))[:160],
    )
    check("asr wrote the SRT", srt.is_file() and srt.stat().st_size > 0, str(srt))
else:
    code = (payload3.get("error") or {}).get("code")
    check("asr crash is UNSUPPORTED, never INTERNAL", code == "UNSUPPORTED", str(payload3.get("error"))[:160])

# --- ASR with no default model and a crashing --model: UNSUPPORTED + hint
empty_home = Path(WORK) / "empty-home"
empty_home.mkdir(exist_ok=True)
env = {"HOME": str(empty_home), "JY_WHISPER_MODEL": ""}
rc4, payload4, _ = run(
    ["asr", SPEECH, "--model", str(bogus), "-o", str(Path(WORK) / "nope.srt"), "--force"],
    env,
    timeout=300,
)
error4 = payload4.get("error") or {}
check("asr no-default rc=1", rc4 == 1, f"rc={rc4}")
check("asr no-default UNSUPPORTED", error4.get("code") == "UNSUPPORTED", str(error4)[:160])
check(
    "asr no-default hint mentions a bigger model or faster-whisper",
    ("ggml-medium" in (error4.get("message") or ""))
    or ("faster-whisper" in (error4.get("message") or "")),
    (error4.get("message") or "")[:200],
)

print(json.dumps({"ok": not failures, "failures": failures,
                  "wedged_s": round(elapsed, 2), "not_running_s": round(elapsed2, 2),
                  "account_scope_timeout": seen.get("context")}, ensure_ascii=False))
PY
speech_arg="$WORK/speech.m4a"
[[ "${say_ok:-0}" -eq 1 ]] || speech_arg="-"
if res_out="$("$PY" "$WORK/resilience_check.py" "$JY" "$WORK/jy draft" "$speech_arg" \
    "$WORK" "$ROOT" 2>&1)"; then
    pass "bridge health gate + ASR fallback [$(echo "$res_out" | head -c 220)]"
else
    fail "bridge health gate + ASR fallback" "$(echo "$res_out" | tail -c 400)"
fi

echo
echo "== entitlement story (naming + --on-entitlement local)"
expect_ok "doctor: membership tier is not readable offline" doctor
expect_field "doctor: VIP/SVIP, server-side only" entitlements
cat > "$WORK/entitlement_check.py" <<'PY'
"""Exercise entitlement naming + the local fallback without a real 剪映 error."""
import json
import sys

sys.path.insert(0, sys.argv[1])
from jy_cli import jianying, jianying_commands  # noqa: E402
from jy_cli.commands import Context  # noqa: E402

root, fallback_out = sys.argv[2], sys.argv[3]
failures = []


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}")


draft = json.load(open(f"{root}/draft_info.json", encoding="utf-8"))
material_id = draft["materials"]["videos"][0]["id"]

exc = jianying.BridgeError(
    "denied", status=403, kind="http",
    body={"ok": False, "error": "film_export_entitlement_denied", "resourceId": material_id},
)
error = jianying_commands._entitlement_error(jianying.to_jy_error(exc), root)
check("code BLOCKED", error.code == "BLOCKED", error.code)
check("names the material id", material_id in error.message, error.message)
check("names the draft path", str(root) in error.message)
check("raw exporter code", "film_export_entitlement_denied" in error.message)
check("says VIP AND SVIP", "VIP" in error.message and "SVIP" in error.message)
check("blocked_item resolved", (error.details.get("blocked_item") or {}).get("id") == material_id)
check("tier not readable offline", (error.details.get("membership") or {}).get("tier_readable_offline") is False)

exc2 = jianying.BridgeError(
    "missing", status=409, kind="http",
    body={"ok": False, "error": "film_export_dependency_missing", "resourceId": "effect-xyz"},
)
dep = jianying_commands._dependency_error(jianying.to_jy_error(exc2), root)
check("dependency names the resource", "effect-xyz" in dep.message, dep.message)
check("dependency suggests opening 剪映", "打开剪映" in dep.message)

ctx = Context()
tool = ctx.require()
data = jianying_commands._local_fallback(ctx, tool, root, fallback_out, None)
items = data.get("degraded_items") or []
check("fallback is marked degraded", data.get("degraded") is True)
check("fallback itemises replacements", len(items) >= 1, str(items))
check("fallback output exists", bool((data.get("output") or {}).get("path")))
print(json.dumps({"ok": not failures, "failures": failures,
                  "degraded_kinds": [item["kind"] for item in items]}))
PY
if ent_out="$("$PY" "$WORK/entitlement_check.py" "$ROOT" "$WORK/jy draft" \
    "$WORK/local_fallback.mp4" 2>&1)"; then
    pass "entitlement naming + local fallback [$(echo "$ent_out" | head -c 200)]"
else
    fail "entitlement naming + local fallback" "$(echo "$ent_out" | tail -c 300)"
fi
fb_dur="$("$FP" -v error -show_entries format=duration -of csv=p=0 \
    "$WORK/local_fallback.mp4" 2>/dev/null)"
if awk -v d="${fb_dur:-0}" 'BEGIN{exit !(d>2.5 && d<3.5)}'; then
    pass "local fallback: ffprobe duration ${fb_dur}s"
else
    fail "local fallback: ffprobe duration ${fb_dur}s outside (2.5, 3.5)"
fi

# Preflight is read-only: it is safe to exercise the async job path here even when
# 剪映 is busy, because nothing is exported.
expect_ok "render --preflight --async (job, no export)" render "$WORK/jy draft" \
    -o "$OUTDIR/pf_async.mp4" --preflight --async
PJOB="$("$PY" -c "import json,sys;print(json.load(open('$OUT'))['data']['job']['id'])")"
expect_ok "jobs wait (preflight-only job)" jobs wait "$PJOB" --timeout-ms 120000
expect_field "preflight job: readonly, no output" preflight_job
if check jianying_up "$WORK/doctor_engine.json" > /dev/null 2>&1; then
    # 剪映's film-export window is SINGLE-INSTANCE: overlapping exports wedge the
    # app (native_export_busy / native_export_window_close_timeout). So the suite
    # never exports by default — it only asserts the read-only preflight contract.
    # Real exports are opt-in: JY_SMOKE_RENDER=1 (one at a time, please).
    expect_ok "render --preflight (read-only, no export)" render "$WORK/jy draft" \
        -o "$OUTDIR/jianying_preflight.mp4" --preflight
    expect_field "preflight: health/route/focus/lock reported" render_preflight
    if [[ -f "$OUTDIR/jianying_preflight.mp4" ]]; then
        fail "render --preflight must not produce an output file"
    else
        pass "preflight: no output file written"
    fi
    if [[ "${JY_SMOKE_RENDER:-0}" == "1" ]]; then
        expect_output "剪映 render -> MP4 (opt-in)" render "$WORK/jy draft" \
            -o "$OUTDIR/jianying.mp4"
        expect_field "render: h264 1280x720" dims 1280 720
        expect_field "render: ~3s" duration 2.7 3.4
        expect_field "render: focus guard reported" render_guard
        expect_error "render: overwrite refused" VALIDATION 2 render "$WORK/jy draft" \
            -o "$OUTDIR/jianying.mp4"
        render_dur="$("$FP" -v error -show_entries format=duration -of csv=p=0 \
            "$OUTDIR/jianying.mp4" 2>/dev/null)"
        if awk -v d="${render_dur:-0}" 'BEGIN{exit !(d>2.7 && d<3.4)}'; then
            pass "render: ffprobe duration ${render_dur}s"
        else
            fail "render: ffprobe duration ${render_dur}s outside (2.7, 3.4)"
        fi
    else
        skip "real 剪映 render (opt-in: JY_SMOKE_RENDER=1; film-export is single-instance)"
        skip "render dims/duration (opt-in)"
        skip "render overwrite + ffprobe (opt-in)"
    fi
else
    skip "剪映 render --preflight (剪映 not running / native bridge descriptor missing)"
    skip "剪映 render export (needs 剪映)"
    skip "render dims/duration (needs 剪映)"
    skip "render overwrite + ffprobe (needs 剪映)"
fi

echo
echo "== Batch C (arbitrary-path staging / per-clip props / draft verbs / assets / fonts)"
cat > "$WORK/batchc_check.py" <<'PY'
"""Batch C checks — no 剪映 required: temp draft root, temp asset box, temp staging."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

JY, WORK, ROOT = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
failures = []
env = dict(os.environ)
env["JY_STAGING_DIR"] = str(WORK / "staging")
env["JY_ASSET_DIR"] = str(WORK / "assets")
env["JY_DRAFT_ROOT"] = str(WORK / "droot")


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}")


def run(args, extra_env=None, timeout=180):
    environ = dict(env)
    if extra_env:
        environ.update(extra_env)
    done = subprocess.run([JY, *args], capture_output=True, text=True, env=environ, timeout=timeout)
    try:
        payload = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"_raw": done.stdout[:300], "_stderr": done.stderr[:300]}
    return done.returncode, payload


# ---------------------------------------------------------------- C5 fonts
rc, payload = run(["fonts"])
data = payload.get("data") or {}
check("fonts list ok", payload.get("ok") is True, str(payload.get("error"))[:160])
check("fonts found families", (data.get("family_count") or 0) > 50, str(data.get("family_count")))
sources = {source for row in data.get("families") or [] for source in row.get("sources") or []}
if Path("/Applications/VideoFusion-macOS.app/Contents/Resources/Font").is_dir():
    check("fonts include 剪映 dir", "jianying" in sources, str(sorted(sources)))

rc, payload = run(["fonts", "--check", "Hiragino Sans GB"])
check("fonts --check good", payload.get("ok") is True, str(payload.get("error"))[:160])
rc, payload = run(["fonts", "--check", "Hiragino Sans GBB"])
error = payload.get("error") or {}
check("fonts --check typo rc=2", rc == 2, f"rc={rc}")
check("fonts --check typo VALIDATION", error.get("code") == "VALIDATION", str(error)[:160])
check("fonts --check suggests", bool((error.get("details") or {}).get("suggestions")), str(error)[:160])

# ---------------------------------------------------------------- C4 assets
shutil.rmtree(Path(env["JY_ASSET_DIR"]), ignore_errors=True)
rc, payload = run(["asset", "import", str(WORK / "a.mp4"), str(WORK / "sticker.png")])
data = payload.get("data") or {}
check("asset import ok", payload.get("ok") is True, str(payload.get("error"))[:160])
check("asset import copied 2", (data.get("count") or 0) == 2, str(data.get("count")))
asset_root = str(Path(env["JY_ASSET_DIR"]).resolve())
check(
    "asset import staged into JY_ASSET_DIR",
    all(str(asset.get("staged", "")).startswith(asset_root) for asset in data.get("assets") or []),
    str(data.get("assets"))[:160],
)
rc, payload = run(["asset", "import", str(WORK / "a.mp4")])
data = payload.get("data") or {}
check("asset import dedupes", (data.get("assets") or [{}])[0].get("deduped") is True, str(data.get("assets"))[:160])
rc, payload = run(["asset", "list"])
data = payload.get("data") or {}
check("asset list count", (data.get("count") or 0) == 2, str(data.get("count")))
check("asset list missing 0", (data.get("missing") or 0) == 0, str(data.get("missing")))
rc, payload = run(["asset", "prune", "--older-than-hours", "0"])
data = payload.get("data") or {}
check("asset prune is dry-run", payload.get("outcome") == "preview", str(payload.get("outcome")))
check("asset prune planned 2", len(data.get("would_remove") or []) == 2, str(data.get("would_remove"))[:160])
check("asset prune removed nothing", not data.get("removed"), str(data.get("removed"))[:160])
check("asset prune left files", all(Path(item).is_file() for item in data.get("would_remove") or []), "files gone in dry-run")
rc, payload = run(["asset", "prune", "--older-than-hours", "0", "--apply"])
data = payload.get("data") or {}
check("asset prune apply", payload.get("outcome") == "executed" and len(data.get("removed") or []) == 2, str(data)[:160])

# ---------------------------------------------------------------- C1 staging + C2 props
staging = Path(env["JY_STAGING_DIR"])
shutil.rmtree(staging, ignore_errors=True)
spec = {
    "canvas": {"width": 640, "height": 360, "fps": 30},
    "clips": [
        {
            "path": str(WORK / "a.mp4"), "start": 0, "duration": 1.5,
            "transform": {"x": 0.1, "y": -0.05, "scale": 1.2, "rotation": 0.0},
            "opacity": 0.9, "volume": 0.6, "speed": 1.25, "render_index": 3,
            "crop": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8},
        },
        {"path": str(WORK / "b.mp4"), "start": 1.5, "duration": 1.0},
    ],
}
spec_path = WORK / "batchc_spec.json"
spec_path.write_text(json.dumps(spec), "utf-8")
draft_dir = WORK / "batchc-draft"
shutil.rmtree(draft_dir, ignore_errors=True)
rc, payload = run(["draft", "build", "--spec", str(spec_path), "-o", str(draft_dir), "--force"])
data = payload.get("data") or {}
check("draft build ok", payload.get("ok") is True, str(payload.get("error"))[:200])
staging_info = data.get("staging") or {}
check("staging enabled", staging_info.get("enabled") is True, str(staging_info)[:160])
check("staging copied every clip", len(staging_info.get("clips") or []) == 2, str(staging_info)[:160])
check(
    "staged paths live under JY_STAGING_DIR",
    all(str(item.get("staged", "")).startswith(str(staging)) for item in staging_info.get("clips") or []),
    str(staging_info.get("clips"))[:200],
)
document = json.loads((draft_dir / "draft_info.json").read_text("utf-8"))
video_track = next(track for track in document["tracks"] if track["type"] == "video")
segments = video_track["segments"]
materials = {item["id"]: item for item in document["materials"]["videos"]}
material_paths = [materials[segment["material_id"]].get("path", "") for segment in segments]
check(
    "draft references staged media, not the original",
    all(str(staging) in path for path in material_paths),
    str(material_paths)[:200],
)
first = segments[0]
check("clip.transform applied", first["clip"]["transform"] == {"x": 0.1, "y": -0.05}, str(first["clip"]))
check("clip.scale applied", first["clip"]["scale"] == {"x": 1.2, "y": 1.2}, str(first["clip"]))
check("opacity -> clip.alpha", first["clip"]["alpha"] == 0.9, str(first["clip"]))
check("speed applied", first["speed"] == 1.25, str(first["speed"]))
check("volume applied", first["volume"] == 0.6, str(first["volume"]))
check("render_index applied", first["render_index"] == 3, str(first["render_index"]))
crop = materials[first["material_id"]].get("crop") or {}
check(
    "crop -> 剪映 polygon",
    round(crop.get("upper_left_x", -1), 3) == 0.1 and round(crop.get("lower_right_x", -1), 3) == 0.9,
    str(crop)[:200],
)

# unknown fields must fail loudly (the white-text lesson)
typo = json.loads(json.dumps(spec))
typo["clips"][0]["transfrom"] = {"x": 1}
typo_path = WORK / "batchc_typo.json"
typo_path.write_text(json.dumps(typo), "utf-8")
rc, payload = run(["draft", "build", "--spec", str(typo_path), "-o", str(WORK / "batchc-typo"), "--force"])
error = payload.get("error") or {}
check("unknown clip field rc=2", rc == 2, f"rc={rc}")
check("unknown clip field VALIDATION", error.get("code") == "VALIDATION", str(error)[:160])
check("unknown clip field names it", "transfrom" in (error.get("message") or ""), (error.get("message") or "")[:160])

top_typo = json.loads(json.dumps(spec))
top_typo["clipz"] = []
top_path = WORK / "batchc_top_typo.json"
top_path.write_text(json.dumps(top_typo), "utf-8")
rc, payload = run(["draft", "build", "--spec", str(top_path), "-o", str(WORK / "batchc-toptypo"), "--force"])
check("unknown spec key VALIDATION", (payload.get("error") or {}).get("code") == "VALIDATION", str(payload.get("error"))[:160])

# --no-staging keeps the original paths
no_stage = WORK / "batchc-nostage"
shutil.rmtree(no_stage, ignore_errors=True)
rc, payload = run(["draft", "build", "--spec", str(spec_path), "-o", str(no_stage), "--force", "--no-staging"])
data = payload.get("data") or {}
check("--no-staging disables staging", (data.get("staging") or {}).get("enabled") is False, str(data.get("staging"))[:160])
nostage_doc = json.loads((no_stage / "draft_info.json").read_text("utf-8"))
nostage_paths = [m.get("path", "") for m in nostage_doc["materials"]["videos"]]
check("--no-staging keeps originals", all(str(staging) not in path for path in nostage_paths), str(nostage_paths)[:160])

# ---------------------------------------------------------------- C3 draft management
droot = Path(env["JY_DRAFT_ROOT"])
shutil.rmtree(droot, ignore_errors=True)
for name, duration in (("d1", 3_000_000), ("d2", 5_000_000)):
    (droot / name).mkdir(parents=True)
    (droot / name / "draft_info.json").write_text(
        json.dumps({"id": f"id-{name}", "name": name, "duration": duration}), "utf-8"
    )
store = [
    {
        "draft_id": f"id-{name}",
        "draft_name": name,
        "draft_fold_path": str(droot / name),
        "draft_json_file": str(droot / name / "draft_info.json"),
        "draft_root_path": str(droot),
        "tm_draft_modified": int(time.time() * 1e6),
        "draft_timeline_materials_size": 10,
    }
    for name in ("d1", "d2")
]
(droot / "root_meta_info.json").write_text(
    json.dumps({"all_draft_store": store, "draft_ids": ["id-d1", "id-d2"], "root_path": str(droot)}),
    "utf-8",
)

rc, payload = run(["draft", "list"])
data = payload.get("data") or {}
check("draft list count", (data.get("count") or 0) == 2, str(data.get("count")))
check("draft list duration", {row["duration_s"] for row in data.get("drafts") or []} == {3.0, 5.0}, str(data.get("drafts"))[:160])

rc, payload = run(["draft", "rename", "d1", "renamed"])
check("rename dry-run is preview", payload.get("outcome") == "preview", str(payload.get("outcome"))[:120])
check("rename dry-run touches nothing", (droot / "d1").is_dir() and not (droot / "renamed").exists(), "dir moved in dry-run")
rc, payload = run(["draft", "rename", "d1", "renamed", "--apply"])
data = payload.get("data") or {}
check("rename apply", payload.get("outcome") == "executed" and (droot / "renamed").is_dir() and not (droot / "d1").exists(), str(data)[:160])
check("rename backs up the index", bool((data.get("changes") or {}).get("index_backup")), str(data.get("changes"))[:160])

rc, payload = run(["draft", "duplicate", "renamed", "--name", "renamed-copy", "--apply"])
data = payload.get("data") or {}
check("duplicate apply", (droot / "renamed-copy").is_dir(), str(data)[:160])
check("duplicate gets a new id", bool((data.get("changes") or {}).get("draft_id")), str(data.get("changes"))[:160])

rc, payload = run(["draft", "move", "d2", "projects", "--apply"])
check("move apply", (droot / "projects" / "d2").is_dir() and not (droot / "d2").exists(), str(payload.get("data"))[:160])

rc, payload = run(["draft", "delete", "d2", "--apply"])
data = payload.get("data") or {}
trash = droot / ".jy-trash"
check("delete moves to .jy-trash", trash.is_dir() and len(list(trash.iterdir())) == 1, str(data)[:160])
check("delete keeps the data recoverable", not (droot / "projects" / "d2").exists(), "draft still in place")

rc, payload = run(["draft", "history"])
data = payload.get("data") or {}
check("history lists index backups", len(data.get("index_backups") or []) >= 1, str(data)[:160])

outside = WORK / "outside-draft"
outside.mkdir(exist_ok=True)
document = json.loads((droot / "root_meta_info.json").read_text("utf-8"))
document["all_draft_store"].append(
    {
        "draft_id": "evil",
        "draft_name": "evil",
        "draft_fold_path": str(outside),
        "draft_json_file": str(outside / "draft_info.json"),
        "draft_root_path": str(droot),
    }
)
(droot / "root_meta_info.json").write_text(json.dumps(document), "utf-8")
rc, payload = run(["draft", "delete", "evil", "--apply"])
check("outside-root draft refused", (payload.get("error") or {}).get("code") == "VALIDATION", str(payload.get("error"))[:160])
check("outside-root folder untouched", outside.is_dir(), "outside folder moved")

# ---------------------------------------------------------------- C4 draft register + import
regroot = WORK / "regroot"
shutil.rmtree(regroot, ignore_errors=True)
regroot.mkdir(parents=True)
(regroot / "root_meta_info.json").write_text(
    json.dumps({"all_draft_store": [], "draft_ids": 0, "root_path": str(regroot)}), "utf-8"
)

reg_spec = WORK / "reg_spec.json"
reg_spec.write_text(
    json.dumps({"canvas": {"width": 320, "height": 568, "fps": 30},
                "clips": [{"path": str(WORK / "a.mp4"), "duration": 1.0}]}),
    "utf-8",
)
ext = WORK / "ext-draft"
shutil.rmtree(ext, ignore_errors=True)
rc, payload = run(["draft", "build", "--spec", str(reg_spec), "-o", str(ext), "--force"])
data = payload.get("data") or {}
check("build outside the draft root skips registration",
      (data.get("registration") or {}).get("registered") is False, str(data.get("registration"))[:200])
check("build writes draft_meta_info.json", (ext / "draft_meta_info.json").is_file(), "meta missing")
check("build writes a list cover", (ext / "draft_cover.jpg").is_file(), "cover missing")
meta = json.loads((ext / "draft_meta_info.json").read_text("utf-8"))
built = json.loads((ext / "draft_info.json").read_text("utf-8"))
check("draft_meta_info carries the draft id", meta.get("draft_id") == built.get("id"), str(meta.get("draft_id")))
check("draft_meta_info points at its folder", meta.get("draft_fold_path") == str(ext.resolve()), str(meta.get("draft_fold_path")))
check("draft_meta_info is not hidden", meta.get("draft_is_invisible") is False, str(meta.get("draft_is_invisible")))

raw_index = (regroot / "root_meta_info.json").read_bytes()
rc, payload = run(["draft", "register", str(ext), "--draft-root", str(regroot)])
data = payload.get("data") or {}
check("register dry-run is preview", payload.get("outcome") == "preview", str(payload.get("outcome")))
check("register dry-run previews an insert", data.get("action") == "insert", str(data.get("action"))[:160])
check("register dry-run uses the counter, not len()",
      (data.get("draft_ids") or {}).get("after") == 1, str(data.get("draft_ids")))
check("register dry-run warns outside the root", data.get("draft_inside_root") is False, str(data.get("draft_inside_root")))
check("register dry-run touches nothing", (regroot / "root_meta_info.json").read_bytes() == raw_index, "index changed")

rc, payload = run(["draft", "register", str(ext), "--draft-root", str(regroot), "--apply"])
data = payload.get("data") or {}
check("register apply", payload.get("outcome") == "executed" and data.get("applied") is True, str(payload.get("error"))[:200])
check("register writes two backups", len(data.get("backups") or []) == 2, str(data.get("backups"))[:200])
index = json.loads((regroot / "root_meta_info.json").read_text("utf-8"))
check("register inserted one entry", len(index["all_draft_store"]) == 1, str(len(index["all_draft_store"])))
check("register bumped draft_ids", index["draft_ids"] == 1, str(index["draft_ids"]))
check("register keeps root_path", index.get("root_path") == str(regroot), str(index.get("root_path")))
sys.path.insert(0, ROOT)
from jy_cli.draft_index import ENTRY_TEMPLATE  # noqa: E402 - the shipped 38-key schema
check("register entry uses 剪映's key schema",
      set(index["all_draft_store"][0]) == set(ENTRY_TEMPLATE),
      str(sorted(set(index["all_draft_store"][0]) ^ set(ENTRY_TEMPLATE))))

rc, payload = run(["draft", "import", str(ext), "--draft-root", str(regroot), "--name", "导入草稿", "--apply"])
data = payload.get("data") or {}
imported = regroot / "导入草稿"
check("import apply", payload.get("outcome") == "executed" and imported.is_dir(), str(payload.get("error"))[:200])
check("import left the source in place", (ext / "draft_info.json").is_file(), "source vanished")
index = json.loads((regroot / "root_meta_info.json").read_text("utf-8"))
check("import updates instead of duplicating", len(index["all_draft_store"]) == 1, str(len(index["all_draft_store"])))
check("import keeps the counter", index["draft_ids"] == 1, str(index["draft_ids"]))
entry = index["all_draft_store"][0]
check("import entry points at the copy", entry["draft_fold_path"] == str(imported.resolve()), str(entry["draft_fold_path"]))
check("import entry keeps the draft id", entry["draft_id"] == built.get("id"), str(entry["draft_id"]))
check("import renamed the list entry", entry["draft_name"] == "导入草稿", str(entry["draft_name"]))
copied = json.loads((imported / "draft_info.json").read_text("utf-8"))
check("import rewrites draft_info.path", copied.get("path") == str(imported.resolve()), str(copied.get("path")))
copied_meta = json.loads((imported / "draft_meta_info.json").read_text("utf-8"))
check("import rewrites meta fold path", copied_meta.get("draft_fold_path") == str(imported.resolve()), str(copied_meta.get("draft_fold_path")))
check("import rewrites meta root path", copied_meta.get("draft_root_path") == str(regroot.resolve()), str(copied_meta.get("draft_root_path")))
check("import keeps a cover", (imported / "draft_cover.jpg").is_file(), "cover missing after import")

rc, payload = run(["draft", "import", str(ext), "--draft-root", str(regroot), "--name", "导入草稿", "--apply"])
data = payload.get("data") or {}
index = json.loads((regroot / "root_meta_info.json").read_text("utf-8"))
check("import twice updates instead of copying again", data.get("mode") == "update", str(data.get("mode")))
check("import twice keeps one entry", len(index["all_draft_store"]) == 1, str(len(index["all_draft_store"])))
check("import twice keeps the counter", index["draft_ids"] == 1, str(index["draft_ids"]))
check("import twice made no second folder",
      sorted(path.name for path in regroot.iterdir() if path.is_dir() and path.name not in (".jy-backups",)) == ["导入草稿"],
      str(sorted(path.name for path in regroot.iterdir() if path.is_dir())))

rc, payload = run(["draft", "list", "--draft-root", str(regroot)])
data = payload.get("data") or {}
check("draft list shows the imported draft", [row.get("name") for row in data.get("drafts") or []] == ["导入草稿"],
      str(data.get("drafts"))[:200])

rc, payload = run(["draft", "build", "--spec", str(reg_spec), "-o", str(regroot / "自动草稿"), "--force"])
data = payload.get("data") or {}
check("build inside the root auto-registers", (data.get("registration") or {}).get("action") == "insert",
      str(data.get("registration"))[:240])
index = json.loads((regroot / "root_meta_info.json").read_text("utf-8"))
check("auto-registered entry is listed", [entry["draft_name"] for entry in index["all_draft_store"]][:1] == ["自动草稿"],
      str([entry["draft_name"] for entry in index["all_draft_store"]]))
check("auto-register bumps draft_ids", index["draft_ids"] == 2, str(index["draft_ids"]))

rc, payload = run(["draft", "build", "--spec", str(reg_spec), "-o", str(regroot / "未登记"), "--force", "--no-register"])
index = json.loads((regroot / "root_meta_info.json").read_text("utf-8"))
check("--no-register leaves the index alone", len(index["all_draft_store"]) == 2, str(len(index["all_draft_store"])))

plain = WORK / "plain-folder"
plain.mkdir(exist_ok=True)
rc, payload = run(["draft", "import", str(plain), "--draft-root", str(regroot), "--apply"])
check("import refuses a plain folder", (payload.get("error") or {}).get("code") == "NOT_FOUND", str(payload.get("error"))[:160])

# ---------------------------------------------------------------- update guard
sys.path.insert(0, ROOT)
from jy_cli import update_guard as ug  # noqa: E402

rc, payload = run(["update", "status"])
data = payload.get("data") or {}
check("update status ok", payload.get("ok") is True, str(payload.get("error"))[:200])
check("update status names the app",
      str((data.get("app") or {}).get("path", "")).endswith("VideoFusion-macOS.app"),
      str(data.get("app"))[:200])
check("update status reports the toggle", "auto_update" in (data.get("config") or {}),
      str(data.get("config"))[:200])

installed = ug.APP.is_dir() and ug.bundle_id() == ug.BUNDLE_ID
rc, payload = run(["update", "lock"])
data = payload.get("data") or {}
if installed:
    check("update lock is dry-run by default",
          payload.get("outcome") == "preview" and data.get("applied") is False,
          str(payload.get("outcome")))
    check("update lock preview lists the pending state", "before" in data, str(data)[:200])
else:
    # No 剪映 on this machine (CI): refusing loudly is the correct behaviour.
    check("update lock refuses a machine without 剪映",
          (payload.get("error") or {}).get("code") == "UNSUPPORTED", str(payload.get("error"))[:200])

rc, payload = run(["update", "unlock"])
if not installed:
    check("update unlock refuses a machine without 剪映",
          (payload.get("error") or {}).get("code") == "UNSUPPORTED", str(payload.get("error"))[:200])
elif ug._manifests():
    check("update unlock is dry-run by default", payload.get("outcome") == "preview",
          str(payload.get("outcome")))
else:
    check("update unlock explains a missing manifest",
          (payload.get("error") or {}).get("code") == "NOT_FOUND", str(payload.get("error"))[:200])

# the flag machinery itself, on a throwaway tree (never the installed app)
fixture = WORK / "update-fixture"
shutil.rmtree(fixture, ignore_errors=True)
(fixture / "nested").mkdir(parents=True)
(fixture / "file.txt").write_text("x", "utf-8")
(fixture / "nested" / "deep.txt").write_text("y", "utf-8")
entries = ug._walk_entries([str(fixture)])
check("flag walk covers every file and directory", len(entries) == 4,
      str([entry["path"] for entry in entries]))
changed, skipped = ug._chflags(entries, add=True)
check("flag lock applies to every entry", changed == len(entries) and not skipped,
      f"changed={changed} skipped={skipped[:2]}")
import stat as _stat  # noqa: E402
check("flag lock really sets UF_IMMUTABLE",
      all(os.lstat(entry["path"]).st_flags & _stat.UF_IMMUTABLE for entry in entries), "flag missing")
try:
    (fixture / "file.txt").write_text("blocked", "utf-8")
    check("locked file refuses writes", False, "write succeeded")
except OSError:
    check("locked file refuses writes", True)
ug._chflags(entries, add=False)
check("flag unlock clears UF_IMMUTABLE",
      all(not (os.lstat(entry["path"]).st_flags & _stat.UF_IMMUTABLE) for entry in entries), "flag stuck")
check("flag unlock keeps the files", (fixture / "nested" / "deep.txt").read_text("utf-8") == "y", "content lost")

# C2b: agent-bridge's verified material keys pass; garbage in them still fails loudly
rich = {
    "canvas": {"width": 640, "height": 360, "fps": 30},
    "clips": [
        {
            "path": str(WORK / "a.mp4"), "start": 0, "duration": 2.0,
            "keyframes": [{"property": "KFTypeScaleX",
                           "points": [{"time": 0, "values": [1.0]},
                                      {"time": 1.0, "values": [1.3]}]}],
            "curve_speed": {"name": "自定义", "id": "curve-test",
                            "points": [{"x": 0, "y": 1.0}, {"x": 1, "y": 2.0}]},
            "mask": {"resource_type": "circle", "name": "圆形",
                     "resource_id": "", "path": "", "config": {"feather": 0.2}},
        }
    ],
    "texts": [
        {"content": "动画文字", "start": 0.2, "duration": 1.0,
         "style": {"color": "#FFD400"},
         "animations": [{"type": "in", "resource_id": "6798726011837280776",
                         "name": "入场动画", "duration": 0.5}]}
    ],
}
rich_path = WORK / "batchc_materials.json"
rich_path.write_text(json.dumps(rich), "utf-8")
rich_draft = WORK / "batchc-materials"
shutil.rmtree(rich_draft, ignore_errors=True)
rc, payload = run(["draft", "build", "--spec", str(rich_path), "-o", str(rich_draft), "--force"])
check("material keys build ok", payload.get("ok") is True, str(payload.get("error"))[:200])
rich_doc = json.loads((rich_draft / "draft_info.json").read_text("utf-8"))
video_segment = next(track for track in rich_doc["tracks"] if track["type"] == "video")["segments"][0]
check("keyframes -> common_keyframes", bool(video_segment.get("common_keyframes")), str(video_segment.keys())[:160])
check("curve_speed -> speeds material", len(rich_doc["materials"].get("speeds") or []) >= 1, str(rich_doc["materials"].get("speeds"))[:160])
check("mask -> common_mask material", len(rich_doc["materials"].get("common_mask") or []) == 1, str(rich_doc["materials"].get("common_mask"))[:160])
check("text animations -> material_animations", len(rich_doc["materials"].get("material_animations") or []) >= 1, str(rich_doc["materials"].get("material_animations"))[:160])

bad_type = json.loads(json.dumps(rich))
bad_type["texts"][0]["animations"][0]["type"] = "sideways"
bad_type_path = WORK / "batchc_bad_anim.json"
bad_type_path.write_text(json.dumps(bad_type), "utf-8")
rc, payload = run(["draft", "build", "--spec", str(bad_type_path), "-o", str(WORK / "batchc-badanim"), "--force"])
check("bad animation type VALIDATION", (payload.get("error") or {}).get("code") == "VALIDATION", str(payload.get("error"))[:160])

bad_mask = json.loads(json.dumps(rich))
bad_mask["clips"][0]["mask"] = {"name": "圆形"}
bad_mask_path = WORK / "batchc_bad_mask.json"
bad_mask_path.write_text(json.dumps(bad_mask), "utf-8")
rc, payload = run(["draft", "build", "--spec", str(bad_mask_path), "-o", str(WORK / "batchc-badmask"), "--force"])
check("mask without resource_type VALIDATION", (payload.get("error") or {}).get("code") == "VALIDATION", str(payload.get("error"))[:160])

# C5 integration: a typo'd font warns instead of silently falling back
rc, payload = run(["text", str(WORK / "a.mp4"), "--content", "字体检查", "--font-name", "NoSuchFontXYZ",
                   "-o", str(WORK / "font-warning.mp4"), "--force"])
check("text with unknown font still renders", payload.get("ok") is True, str(payload.get("error"))[:160])
check(
    "text warns about the unknown font",
    any("NoSuchFontXYZ" in warning for warning in payload.get("warnings") or []),
    str(payload.get("warnings"))[:200],
)

print(json.dumps({"ok": not failures, "failures": failures,
                  "staged_clips": len(staging_info.get("clips") or []),
                  "draft_root": str(droot)}, ensure_ascii=False))
raise SystemExit(0 if not failures else 1)
PY
if bc_out="$("$PY" "$WORK/batchc_check.py" "$JY" "$WORK" "$ROOT" 2>&1)"; then
    pass "Batch C: staging + props + draft verbs + assets + fonts [$(echo "$bc_out" | head -c 200)]"
else
    fail "Batch C checks" "$(echo "$bc_out" | tail -c 500)"
fi

echo
echo "== follow-ups (colour wheel / timeline migration / clean --staging / register backups)"
cat > "$WORK/followup_check.py" <<'PY'
"""Follow-up checks: colour wheel, multi-timeline migration, duration fallback,
clean --staging/--assets, and the register backup contract. No 剪映 required."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

JY, WORK = sys.argv[1], Path(sys.argv[2])
failures = []
env = dict(os.environ)
env["JY_STAGING_DIR"] = str(WORK / "fu-staging")
env["JY_ASSET_DIR"] = str(WORK / "fu-assets")


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}")


def run(args, extra_env=None, timeout=180):
    environ = dict(env)
    if extra_env:
        environ.update(extra_env)
    done = subprocess.run([JY, *args], capture_output=True, text=True, env=environ, timeout=timeout)
    try:
        payload = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"_raw": done.stdout[:300], "_stderr": done.stderr[:300]}
    return done.returncode, payload


# ---------------------------------------------------------------- 1) colour wheel
spec = {
    "canvas": {"width": 640, "height": 360, "fps": 30},
    "clips": [{
        "path": str(WORK / "a.mp4"), "start": 0, "duration": 2.0,
        "color_wheel": [
            {"segment": "gamma", "red": 0.10, "green": -0.20, "blue": 0.05},
            {"segment": "lift", "red": -0.05, "green": 0.0, "blue": 0.08, "hsl": False},
        ],
    }],
}
spec_path = WORK / "fu_wheel.json"
spec_path.write_text(json.dumps(spec), "utf-8")
wheel_draft = WORK / "fu-wheel"
shutil.rmtree(wheel_draft, ignore_errors=True)
rc, payload = run(["draft", "build", "--spec", str(spec_path), "-o", str(wheel_draft), "--force"])
check("color_wheel build ok", payload.get("ok") is True, str(payload.get("error"))[:200])
doc = json.loads((wheel_draft / "draft_info.json").read_text("utf-8"))
wheels = doc["materials"].get("primary_color_wheels") or []
hsl = doc["materials"].get("hsl") or []
segment = next(t for t in doc["tracks"] if t["type"] == "video")["segments"][0]
check("color_wheel -> primary_color_wheels", len(wheels) == 2, str(wheels)[:200])
check("color_wheel segments", {w["segment"] for w in wheels} == {"gamma", "lift"}, str(wheels)[:200])
check("color_wheel channels", wheels[0]["red"] == 0.1 and wheels[0]["green"] == -0.2, str(wheels[0]))
check("hsl companion only when enabled", len(hsl) == 1, str(hsl)[:160])
check("hsl companion pairs via constant_material_id",
      hsl[0]["constant_material_id"] == wheels[0]["id"] and wheels[0]["constant_material_id"] == hsl[0]["id"],
      str(wheels[0])[:200])
refs = set(segment.get("extra_material_refs") or [])
check("wheel refs attached", all(w["id"] in refs for w in wheels), str(refs)[:160])
check("hsl ref attached", hsl[0]["id"] in refs, str(refs)[:160])

bad = json.loads(json.dumps(spec))
bad["clips"][0]["color_wheel"][0]["segment"] = "midtones"
bad_path = WORK / "fu_wheel_bad.json"
bad_path.write_text(json.dumps(bad), "utf-8")
rc, payload = run(["draft", "build", "--spec", str(bad_path), "-o", str(WORK / "fu-wheelbad"), "--force"])
check("bad color_wheel segment VALIDATION", (payload.get("error") or {}).get("code") == "VALIDATION",
      str(payload.get("error"))[:160])

# ---------------------------------------------------------------- 1b) text_template (new bucket)
template_spec = {
    "canvas": {"width": 640, "height": 360, "fps": 30},
    "clips": [{"path": str(WORK / "a.mp4"), "start": 0, "duration": 2.0}],
    "texts": [{
        "content": "模板文字", "start": 0.2, "duration": 1.0,
        "text_template": {
            "resource_id": "7182123456789012345", "name": "影视3D",
            "category_id": "text_template", "category_name": "文字模板",
            "resources": [
                {"panel": "default", "resource_id": "7182123456789012345"},
                {"panel": "fonts", "resource_id": "7182123456789012346"},
            ],
        },
    }],
}
template_path = WORK / "fu_template.json"
template_path.write_text(json.dumps(template_spec), "utf-8")
template_draft = WORK / "fu-template"
shutil.rmtree(template_draft, ignore_errors=True)
rc, payload = run(["draft", "build", "--spec", str(template_path), "-o", str(template_draft), "--force"])
check("text_template build ok", payload.get("ok") is True, str(payload.get("error"))[:200])
if payload.get("ok"):
    tdoc = json.loads((template_draft / "draft_info.json").read_text("utf-8"))
    templates = tdoc["materials"].get("text_templates") or []
    check("text_template -> materials.text_templates", len(templates) == 1, str(templates)[:200])
    check("text_template resources kept", len((templates[0].get("resources") or [])) == 2, str(templates)[:200])

bad_panel = json.loads(json.dumps(template_spec))
bad_panel["texts"][0]["text_template"]["resources"][0]["panel"] = "dafault"
bad_panel_path = WORK / "fu_template_badpanel.json"
bad_panel_path.write_text(json.dumps(bad_panel), "utf-8")
rc, payload = run(["draft", "build", "--spec", str(bad_panel_path), "-o", str(WORK / "fu-tplbad"), "--force"])
error = payload.get("error") or {}
check("bad text_template panel VALIDATION", error.get("code") == "VALIDATION", str(error)[:160])
check("bad panel name mentioned", "panel" in (error.get("message") or ""), (error.get("message") or "")[:160])

typo = json.loads(json.dumps(template_spec))
typo["texts"][0]["text_template"]["resourceid"] = "x"
typo_path = WORK / "fu_template_typo.json"
typo_path.write_text(json.dumps(typo), "utf-8")
rc, payload = run(["draft", "build", "--spec", str(typo_path), "-o", str(WORK / "fu-tpltypo"), "--force"])
error = payload.get("error") or {}
check("unknown text_template key refused", error.get("code") == "VALIDATION", str(error)[:160])
check("unknown key suggests resource_id", "resource_id" in (error.get("message") or ""), (error.get("message") or "")[:200])

# ---------------------------------------------------------------- 2) multi-timeline migration
multi = WORK / "fu-multi"
shutil.rmtree(multi, ignore_errors=True)
shutil.copytree(wheel_draft, multi)
timeline_id = "11111111-2222-3333-4444-555555555555"
timelines = multi / "Timelines"
(timelines / timeline_id).mkdir(parents=True)
shutil.move(str(multi / "draft_info.json"), str(timelines / timeline_id / "draft_info.json"))
decoy = timelines / "99999999-8888-7777-6666-555555555555"
decoy.mkdir()
(decoy / "draft_info.json").write_text(json.dumps({"duration": 99_000_000}), "utf-8")
(timelines / "project.json").write_text(json.dumps({"main_timeline_id": timeline_id}), "utf-8")
rc, payload = run(["render", str(multi), "-o", str(WORK / "fu-multi.mp4"), "--preflight"])
draft_info = (payload.get("data") or {}).get("draft") or {}
check("migrated draft preflight ok", payload.get("ok") is True, str(payload.get("error"))[:200])
check("migrated layout detected", draft_info.get("layout") == "multi-timeline", str(draft_info)[:200])
check("migrated main timeline chosen", draft_info.get("timeline_id") == timeline_id, str(draft_info)[:200])
check("migrated duration from real timeline", draft_info.get("duration_s") == 2.0, str(draft_info)[:200])
check("migrated sha from Timelines file", bool(draft_info.get("sha256")), str(draft_info)[:160])
check("migration warned", any("多时间线" in w for w in payload.get("warnings") or []),
      str(payload.get("warnings"))[:200])

# ---------------------------------------------------------------- 3) duration fallback
droot = WORK / "fu-droot"
shutil.rmtree(droot, ignore_errors=True)
enc = droot / "enc"
enc.mkdir(parents=True)
(enc / "draft_info.json").write_bytes(b"encrypted-blob-not-json")
(droot / "root_meta_info.json").write_text(json.dumps({
    "all_draft_store": [{
        "draft_id": "enc-1", "draft_name": "enc", "draft_fold_path": str(enc),
        "draft_json_file": str(enc / "draft_info.json"), "draft_root_path": str(droot),
        "tm_duration": 12_345_678, "tm_draft_modified": 1,
    }],
    "draft_ids": 1, "root_path": str(droot),
}), "utf-8")
rc, payload = run(["draft", "list"], {"JY_DRAFT_ROOT": str(droot)})
rows = (payload.get("data") or {}).get("drafts") or []
check("draft list ok", payload.get("ok") is True, str(payload.get("error"))[:160])
check("encrypted draft duration falls back", rows and rows[0]["duration_s"] == 12.346, str(rows)[:200])
check("duration source reported", rows and rows[0]["duration_source"] == "root_meta_info.tm_duration", str(rows)[:200])

# ---------------------------------------------------------------- 4) clean --staging / --assets
staging = Path(env["JY_STAGING_DIR"])
shutil.rmtree(staging, ignore_errors=True)
for name in ("hashA", "hashB"):
    (staging / name).mkdir(parents=True)
    (staging / name / "clip.mp4").write_bytes(b"x" * 16)
assets = Path(env["JY_ASSET_DIR"])
shutil.rmtree(assets, ignore_errors=True)
run(["asset", "import", str(WORK / "a.mp4")])
rc, payload = run(["clean", "--staging", "--older-than-hours", "0"])
data = payload.get("data") or {}
check("clean --staging defaults to dry-run", data.get("dry_run") is True, str(data)[:160])
check("clean --staging planned 2", len(data.get("removed") or []) == 2, str(data.get("removed"))[:160])
check("clean --staging left the files", len(list(staging.iterdir())) == 2, "files gone in dry-run")
rc, payload = run(["clean", "--staging", "--older-than-hours", "0", "--apply"])
data = payload.get("data") or {}
check("clean --staging --apply deletes", len(data.get("removed") or []) == 2 and not list(staging.iterdir()),
      str(data)[:160])
rc, payload = run(["clean", "--assets", "--older-than-hours", "0", "--apply"])
data = payload.get("data") or {}
check("clean --assets --apply deletes", len(data.get("removed") or []) == 1, str(data)[:160])
rc, payload = run(["clean", "--dry-run", "--older-than-hours", "0"])
data = payload.get("data") or {}
check("plain clean still cache-only", "staging" not in data and "assets" not in data, str(data)[:160])

# ---------------------------------------------------------------- 5) register backups
regroot = WORK / "fu-regroot"
shutil.rmtree(regroot, ignore_errors=True)
regroot.mkdir(parents=True)
shutil.copytree(wheel_draft, regroot / "jy-reg")
(regroot / "root_meta_info.json").write_text(json.dumps({
    "all_draft_store": [{
        "draft_id": "seed", "draft_name": "seed", "draft_fold_path": str(regroot / "seed"),
        "draft_json_file": str(regroot / "seed/draft_info.json"),
        "draft_root_path": str(regroot), "draft_cover": "",
        "draft_timeline_materials_size": 1, "tm_duration": 1_000_000,
    }],
    "draft_ids": 1, "root_path": str(regroot),
}), "utf-8")
meta = str(regroot / "root_meta_info.json")
rc, payload = run(["material", "register", str(regroot / "jy-reg"), "--root-meta", meta])
check("register dry-run writes nothing", (payload.get("data") or {}).get("applied") is False, str(payload.get("data"))[:160])
check("register dry-run has no backup", not (regroot / ".jy-backups").exists(), "backup made on dry-run")
rc, payload = run(["material", "register", str(regroot / "jy-reg"), "--root-meta", meta, "--apply"])
data = payload.get("data") or {}
check("register --apply ok", data.get("applied") is True, str(payload.get("error"))[:160])
backups = data.get("backups") or []
check("register writes two backups", len(backups) == 2, str(backups)[:200])
check("register backups exist", all(Path(item).is_file() for item in backups), str(backups)[:200])
check("register backup under .jy-backups", any(".jy-backups" in item for item in backups), str(backups)[:200])
check("register backup with .bak- name", any(".bak-" in item for item in backups), str(backups)[:200])

print(json.dumps({"ok": not failures, "failures": failures,
                  "wheels": len(wheels), "backups": len(backups)}, ensure_ascii=False))
raise SystemExit(0 if not failures else 1)
PY
if fu_out="$("$PY" "$WORK/followup_check.py" "$JY" "$WORK" 2>&1)"; then
    pass "follow-ups: colour wheel + migration + clean + backups [$(echo "$fu_out" | head -c 200)]"
else
    fail "follow-up checks" "$(echo "$fu_out" | tail -c 600)"
fi

echo
echo "== effects cached (synthetic 剪映 cache) + name resolution + relative --staging-dir"
cat > "$WORK/cached_check.py" <<'PY'
"""`jy effects cached` against a fake 剪映 cache, draft build by material name,
and relative --staging-dir. Never touches a real 剪映 install."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

JY, WORK = sys.argv[1], Path(sys.argv[2])
failures = []


def check(name, cond, detail=""):
    if not cond:
        failures.append(f"{name}: {detail}")


def run(args, env=None, cwd=None):
    p = subprocess.run([JY, *args], capture_output=True, text=True, env=env, cwd=cwd)
    return json.loads(p.stdout)


root = WORK / "fake-jianying-cache"
packages = {  # rid: (dir, md5, name, effect_type, category)
    "7000000000000000019": ("effect", "a" * 32, "测试叠化", 19, "热门"),
    "7000000000000000012": ("artistEffect", "b" * 32, "测试滤镜", 12, "电影"),
    "7000000000000000007": ("effect", "c" * 32, "测试暗角", 7, "基础"),
    "7000000000000000018": ("effect", "d" * 32, "测试渐显", 18, "入场"),
    "7000000000000000099": ("effect", "e" * 32, "", 7, ""),  # cached but unnamed
}
items = []
for rid, (sub, md5, name, etype, cat) in packages.items():
    pkg = root / sub / rid / md5
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "config.json").write_text("{}", "utf-8")
    if name:
        items.append({"category_name": cat, "effects": [
            {"id": rid, "effect_id": rid, "title": name, "effect_type": etype, "md5": md5,
             "extra": "{\"is_vip\":false}"}]})
db_dir = root / "ressdk_db" / "1"
db_dir.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(str(db_dir / "rp.db"))
conn.execute("CREATE TABLE http_cache (id INTEGER PRIMARY KEY, url TEXT, response_body TEXT, version TEXT)")
conn.execute("INSERT INTO http_cache (url, response_body) VALUES (?, ?)",
             ("/artist/v1/panel/get_panel_info_x", json.dumps({"data": {"categories": items}})))
conn.commit()
conn.close()
db_before = (db_dir / "rp.db").stat().st_mtime_ns

env = dict(os.environ, JY_JIANYING_CACHE=str(root))
payload = run(["effects", "cached", "--limit", "0"], env=env)
data = payload.get("data") or {}
kinds = {it["kind"]: it for it in data.get("items") or []}
check("cached ok", payload.get("ok") is True, str(payload.get("error"))[:200])
check("cached reports root", data.get("cache_root") == str(root), str(data.get("cache_root")))
check("cached finds 4 named", data.get("matched") == 4, str(data.get("matched")))
check("cached counts unnamed", data.get("unnamed_packages") == 1, str(data.get("unnamed_packages")))
check("kinds mapped", set(kinds) == {"transition", "filter", "video_effect", "text_animation"}, str(set(kinds)))
check("status cached", all(it.get("status") == "cached" for it in data.get("items") or []), "")
check("transition snippet", "clips[i].transition" in (kinds.get("transition") or {}).get("spec", {}), str(kinds.get("transition")))
check("animation type from category", kinds.get("text_animation", {}).get("animation_type") == "in", str(kinds.get("text_animation")))
check("filter snippet path", (kinds.get("filter") or {}).get("spec", {}).get("filters[]", {}).get("path", "").endswith("b" * 32), "")
check("live db untouched", (db_dir / "rp.db").stat().st_mtime_ns == db_before, "rp.db modified")
payload = run(["effects", "cached", "--kind", "filter", "--search", "滤镜"], env=env)
check("kind+search filter", (payload.get("data") or {}).get("matched") == 1, str(payload.get("data"))[:200])
payload = run(["effects", "cached"], env=dict(os.environ, JY_JIANYING_CACHE=str(WORK / "no-such-cache")))
check("missing cache UNAVAILABLE", (payload.get("error") or {}).get("code") == "UNAVAILABLE", str(payload.get("error"))[:200])
empty = WORK / "empty-jianying-cache"
(empty / "effect").mkdir(parents=True, exist_ok=True)
payload = run(["effects", "cached"], env=dict(os.environ, JY_JIANYING_CACHE=str(empty)))
check("empty cache NOT_FOUND", (payload.get("error") or {}).get("code") == "NOT_FOUND", str(payload.get("error"))[:200])

# draft build resolves names from the cache; relative --staging-dir is CWD-relative
spec_dir = WORK / "byname"
spec_dir.mkdir(exist_ok=True)
spec = {"canvas": {"width": 640, "height": 360, "fps": 30},
        "clips": [{"path": str(WORK / "a.mp4"), "start": 0, "duration": 1.0,
                   "transition": {"name": "测试叠化", "duration": 0.3}},
                  {"path": str(WORK / "b.mp4"), "start": 1.0, "duration": 1.0}],
        "texts": [{"content": "名字", "start": 0, "duration": 1,
                   "animations": [{"type": "in", "name": "测试渐显", "duration": 0.3}]}],
        "filters": [{"name": "测试滤镜", "value": 0.5}],
        "effects": [{"name": "测试暗角"}]}
(spec_dir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), "utf-8")
payload = run(["draft", "build", "--spec", "spec.json", "-o", "draft", "--staging-dir", "rel-staging", "--force"],
              env=env, cwd=spec_dir)
data = payload.get("data") or {}
check("by-name build ok", payload.get("ok") is True, str(payload.get("error"))[:300])
check("4 names resolved", len(data.get("materials_resolved") or []) == 4, str(data.get("materials_resolved"))[:300])
check("relative staging under CWD", (spec_dir / "rel-staging").is_dir() and str(data.get("staging", {}).get("root")) == str((spec_dir / "rel-staging").resolve()),
      str(data.get("staging", {}).get("root")))
doc = json.loads((spec_dir / "draft" / "draft_info.json").read_text("utf-8")) if (spec_dir / "draft" / "draft_info.json").is_file() else {}
trans = (doc.get("materials") or {}).get("transitions") or []
check("transition id filled", bool(trans) and trans[0].get("resource_id") == "7000000000000000019", str(trans)[:200])
bad = dict(spec, filters=[{"name": "不存在"}])
(spec_dir / "bad.json").write_text(json.dumps(bad, ensure_ascii=False), "utf-8")
payload = run(["draft", "build", "--spec", "bad.json", "-o", "bad", "--no-staging", "--force"], env=env, cwd=spec_dir)
check("unknown name NOT_FOUND", (payload.get("error") or {}).get("code") == "NOT_FOUND", str(payload.get("error"))[:200])

# --- search-remote / fetch against a local mock CDN (no network) ---
import hashlib, http.server, io, threading, zipfile


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


good = make_zip({"config.json": "{}", "AmazingAuto_out/main.scene": "x", "../evil.txt": "no"})
good_md5 = hashlib.md5(good).hexdigest()
other = make_zip({"config.json": "{\"v\":2}"})
other_md5 = hashlib.md5(other).hexdigest()
blobs = {f"/{good_md5}": good, f"/{other_md5}": other, "/badmd5": good}


class Mock(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/forbidden":
            self.send_response(403); self.end_headers(); return
        body = blobs.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_address[1]}"
remote = [  # rid, name, type, md5, url
    ("7100000000000000019", "测试推近", 19, good_md5, f"{base}/{good_md5}"),
    ("7100000000000000018", "测试扫光", 18, other_md5, f"{base}/{other_md5}"),
    ("7100000000000000007", "坏校验", 7, "f" * 32, f"{base}/badmd5"),
    ("7100000000000000008", "被拒绝", 7, "e" * 32, f"{base}/forbidden"),
    ("7100000000000000009", "非白名单", 7, "d" * 32, "https://evil.example.com/x"),
    ("7100000000000000010", "连不上", 7, "c" * 32, "http://127.0.0.1:9/x"),
]
conn = sqlite3.connect(str(db_dir / "rp.db"))
conn.execute("INSERT INTO http_cache (url, response_body) VALUES (?, ?)", ("/artist/v1/x2", json.dumps(
    {"data": {"categories": [{"category_name": "入场", "effects": [
        {"id": rid, "effect_id": rid, "title": name, "effect_type": t, "md5": md5, "item_urls": [url],
         "publish_source": "loki", "extra": "{\"is_vip\":true}"} for rid, name, t, md5, url in remote]}]}})))
conn.commit()
conn.close()
fenv = dict(env, JY_FETCH_ALLOW_LOOPBACK="1")
payload = run(["effects", "search-remote", "测试推近"], env=fenv)
row = ((payload.get("data") or {}).get("items") or [{}])[0]
check("search-remote finds remote", row.get("status") == "remote" and row.get("downloadable") is True and row.get("vip") is True, str(row))
payload = run(["effects", "search-remote", "测试叠化"], env=fenv)
check("search-remote marks cached", ((payload.get("data") or {}).get("items") or [{}])[0].get("status") == "cached", str(payload.get("data"))[:200])
payload = run(["effects", "fetch", "测试推近"], env=fenv)
data = payload.get("data") or {}
pkg = root / "effect" / "7100000000000000019" / good_md5
check("fetch executed", payload.get("outcome") == "executed" and data.get("fetched") is True, str(payload)[:300])
check("fetch layout", (pkg / "config.json").is_file() and (pkg / "AmazingAuto_out" / "main.scene").is_file(), str(pkg))
check("fetch blocks zip traversal", not (root / "effect" / "7100000000000000019" / "evil.txt").exists() and not (pkg.parent.parent / "evil.txt").exists(), "")
check("fetch writes modity_time", (pkg.parent / f"{good_md5}_modity_time.txt").is_file(), "")
check("fetch leaves no temp dirs", not [p for p in pkg.parent.iterdir() if p.name.startswith(".jy-fetch")], "")
check("fetch reports vip warning", any("会员" in w for w in payload.get("warnings") or []), str(payload.get("warnings")))
payload = run(["effects", "fetch", "7100000000000000019"], env=fenv)
check("refetch is noop", payload.get("outcome") == "noop", str(payload)[:200])
payload = run(["effects", "cached", "--kind", "transition", "--limit", "0"], env=fenv)
check("fetched shows in cached", "测试推近" in [i["name"] for i in (payload.get("data") or {}).get("items") or []], str(payload.get("data"))[:300])
for name, code, retriable in (("坏校验", "UNAVAILABLE", False), ("被拒绝", "BLOCKED", False),
                              ("非白名单", "UNAVAILABLE", False), ("连不上", "UNAVAILABLE", True)):
    payload = run(["effects", "fetch", name], env=fenv)
    err = payload.get("error") or {}
    check(f"fetch {name} -> {code}", err.get("code") == code and bool(err.get("retriable")) == retriable, str(err)[:300])
check("failed fetch wrote nothing", not (root / "effect" / "7100000000000000007").exists() or not any(
    p.is_dir() and not p.name.startswith(".") for p in (root / "effect" / "7100000000000000007").iterdir()), "")
fspec = json.loads(json.dumps(spec))
fspec["texts"][0]["animations"] = [{"type": "in", "name": "测试扫光", "duration": 0.3}]
(spec_dir / "fetch.json").write_text(json.dumps(fspec, ensure_ascii=False), "utf-8")
payload = run(["draft", "build", "--spec", "fetch.json", "-o", "fdraft", "--no-staging", "--force"], env=fenv, cwd=spec_dir)
check("uncached name without --fetch NOT_FOUND", (payload.get("error") or {}).get("code") == "NOT_FOUND", str(payload.get("error"))[:200])
payload = run(["draft", "build", "--spec", "fetch.json", "-o", "fdraft", "--no-staging", "--force", "--fetch"], env=fenv, cwd=spec_dir)
fetched = [r for r in (payload.get("data") or {}).get("materials_resolved") or [] if r.get("fetched")]
check("draft build --fetch downloads", payload.get("ok") is True and len(fetched) == 1, str(payload.get("error") or payload.get("data", {}).get("materials_resolved"))[:300])

# --- live catalog client against a mock Loki + Artist server ---
import urllib.parse
live_blob = make_zip({"config.json": "{\"live\":1}"})
live_md5 = hashlib.md5(live_blob).hexdigest()
art_blob = make_zip({"config.json": "{\"artist\":1}"})
art_md5 = hashlib.md5(art_blob).hexdigest()
blobs[f"/{live_md5}"] = live_blob
blobs[f"/{art_md5}"] = art_blob
seen = {"sign_ok": None, "params": None}


def loki_effect(rid, name, md5):
    return {"name": name, "id": md5, "effect_id": rid[-6:], "resource_id": rid,
            "file_url": {"uri": md5, "url_list": [f"{base}/{md5}"]}, "extra": "{\"is_vip\":true}",
            "effect_type": 0}


class LiveMock(Mock):
    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path.startswith("/effect/api/"):
            seen["params"] = q
            if q.get("aid") != "1059" or not q.get("device_id", "").isdigit():
                return self._json({"status_code": 2009, "message": "access_key does not exist"})
            effects = [loki_effect("7200000000000000001", "直播转场", live_md5)] if q.get("panel") == "transitions" else []
            if parsed.path.endswith("/search/effects") and q.get("keyword") not in ("直播", "直播转场"):
                effects = []
            return self._json({"status_code": 0, "data": {"effects": effects, "category": [
                {"name": "热门", "effects": [e["effect_id"] for e in effects]}]}})
        return super().do_GET()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        dev, ts = self.headers.get("tdid"), self.headers.get("device-time")
        want = hashlib.md5(f"9e2c|{body}|mac|11.7.0|{ts}|{dev}|11ac".encode()).hexdigest()  # gitleaks:allow
        seen["sign_ok"] = self.headers.get("sign") == want
        if not seen["sign_ok"]:
            return self._json({"ret": "1", "errmsg": "bad sign"})
        ids = json.loads(body)["id_list"]
        return self._json({"ret": "0", "data": {"effect_item_list": [{"common_attr": {
            "id": i, "effect_id": i, "title": "艺术轮廓", "effect_type": 29, "md5": art_md5,
            "item_urls": [f"{base}/{art_md5}"], "business_info": {"json_str": "{\"is_vip\":false}"}}} for i in ids]}})

    def _json(self, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)


server.shutdown()
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LiveMock)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_address[1]}"
lenv = dict(fenv, JY_LIVE_BASE=base, JY_CACHE_DIR=str(WORK / "live-cache-dir"))
lenv.pop("JY_LIVE_OFFLINE", None)
payload = run(["effects", "search-remote", "直播", "--kind", "transition"], env=lenv)
rows = (payload.get("data") or {}).get("items") or []
check("live search-remote ok", payload.get("ok") is True and rows and rows[0].get("source") == "live"
      and rows[0].get("status") == "remote" and rows[0].get("vip") is True, str(payload)[:300])
check("live uses public client params + synthetic device id",
      (seen["params"] or {}).get("access_key") == "0051d530508b11e9b441ed975323ebf8"  # gitleaks:allow
      and len((seen["params"] or {}).get("device_id", "")) == 19, str(seen["params"])[:200])
dev = json.loads((WORK / "config" / "device.json").read_text("utf-8"))
payload = run(["effects", "search-remote", "直播", "--kind", "transition"], env=lenv)
check("device id is stable", (seen["params"] or {}).get("device_id") == dev["device_id"], "")
payload = run(["effects", "search-remote", "直播", "--kind", "transition", "--offline"], env=lenv)
check("--offline skips live", (payload.get("error") or {}).get("code") == "NOT_FOUND", str(payload)[:200])
payload = run(["effects", "fetch", "直播转场", "--kind", "transition"], env=lenv)
data = payload.get("data") or {}
check("live fetch executed", payload.get("outcome") == "executed" and data.get("cache_dir") == "effect"
      and (root / "effect" / "7200000000000000001" / live_md5 / "config.json").is_file(), str(payload)[:300])
payload = run(["effects", "cached", "--kind", "transition", "--limit", "0"], env=lenv)
check("live-fetched name in cached", "直播转场" in [i["name"] for i in (payload.get("data") or {}).get("items") or []],
      str(payload.get("data"))[:300])
payload = run(["effects", "fetch", "7300000000000000029"], env=lenv)
data = payload.get("data") or {}
check("artist mget signed + fetched to artistEffect", seen["sign_ok"] is True and data.get("cache_dir") == "artistEffect"
      and (root / "artistEffect" / "7300000000000000029" / art_md5 / "config.json").is_file(), str(payload)[:300])
denv = dict(lenv, JY_LIVE_BASE="http://127.0.0.1:9")
payload = run(["effects", "search-remote", "测试推近"], env=denv)
check("live down falls back to cache", payload.get("ok") is True and any("回退" in w for w in payload.get("warnings") or []),
      str(payload)[:300])
server.shutdown()

print(json.dumps({"ok": not failures, "failures": failures}, ensure_ascii=False))
raise SystemExit(0 if not failures else 1)
PY
if cc_out="$("$PY" "$WORK/cached_check.py" "$JY" "$WORK" 2>&1)"; then
    pass "effects cached + by-name draft build + relative staging [$(echo "$cc_out" | head -c 160)]"
else
    fail "effects cached checks" "$(echo "$cc_out" | tail -c 800)"
fi
if [[ "${JY_LIVE_FETCH:-}" == "1" && -d "$HOME/Movies/JianyingPro/User Data/Cache/ressdk_db" ]]; then
    # live CDN fetch into a COPY of the listings, never into the real 剪映 cache
    LIVE="$WORK/live-cache"; mkdir -p "$LIVE/effect"
    cp -R "$HOME/Movies/JianyingPro/User Data/Cache/ressdk_db" "$LIVE/"
    run_jy_env() { JY_LIVE_OFFLINE= JY_JIANYING_CACHE="$LIVE" "$JY" "$@" > "$OUT" 2> "$ERR"; RC=$?; }
    run_jy_env effects search-remote 烟雾 --kind transition
    if [[ $RC -eq 0 ]] && grep -q '"source": "live"' "$OUT"; then
        pass "live search-remote 烟雾 (剪映 live catalog, JY_LIVE_FETCH=1)"
    else
        fail "live search-remote" "rc=$RC $(head -c 300 "$OUT")"
    fi
    run_jy_env effects fetch 烟雾转场 --kind transition
    if [[ $RC -eq 0 ]] && ls "$LIVE/effect/7450031574923350555/"*/config.json >/dev/null 2>&1; then
        pass "live fetch 烟雾转场 from the CDN (JY_LIVE_FETCH=1)"
    else
        fail "live fetch" "rc=$RC $(head -c 300 "$OUT")"
    fi
else
    skip "live CDN fetch (set JY_LIVE_FETCH=1 on a machine with 剪映)"
fi
REAL_JY_CACHE="$HOME/Movies/JianyingPro/User Data/Cache"
if [[ -d "$REAL_JY_CACHE/effect" ]]; then
    expect_ok "effects cached on the real 剪映 cache (read-only)" effects cached --limit 5
else
    skip "effects cached on a real 剪映 cache (none on this machine)"
fi

echo
echo "== sfx (index / search / add / label / forget, synthetic fixtures only)"
cat > "$WORK/sfx_check.py" <<'PY'
"""`jy sfx` checks — synthetic fixtures only, no 剪映 needed."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

JY, WORK, FF = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
failures = []
lib = WORK / "sfx-lib"
extra = WORK / "sfx-extra"
env = dict(os.environ)
env["JY_SFX_DIR"] = str(lib)


def check(name, condition, detail=""):
    if not condition:
        failures.append(f"{name}: {detail}")


def run(args, extra_env=None, timeout=180):
    environ = dict(env)
    if extra_env:
        environ.update(extra_env)
    done = subprocess.run([JY, *args], capture_output=True, text=True, env=environ, timeout=timeout)
    try:
        payload = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"_raw": done.stdout[:300], "_stderr": done.stderr[:300]}
    return done.returncode, payload


def ffmpeg(*args):
    done = subprocess.run([FF, *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"fixture ffmpeg failed: {done.stderr[-200:]}")


shutil.rmtree(lib, ignore_errors=True)
shutil.rmtree(extra, ignore_errors=True)
lib.mkdir(parents=True)
extra.mkdir(parents=True)
ffmpeg("-v", "error", "-f", "lavfi", "-i", "sine=frequency=1800:duration=0.4",
       "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", "-y", str(lib / "叮-提示音.wav"))
ffmpeg("-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=0.8",
       "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", "-y", str(lib / "whoosh-转场.mp3"))
ffmpeg("-v", "error", "-f", "lavfi", "-i", "sine=frequency=900:duration=0.3",
       "-ac", "2", "-y", str(lib / "提示音A.wav"))
ffmpeg("-v", "error", "-f", "lavfi", "-i", "sine=frequency=1000:duration=0.3",
       "-ac", "2", "-y", str(lib / "提示音B.wav"))
ffmpeg("-v", "error", "-f", "lavfi", "-i", "sine=frequency=700:duration=0.2",
       "-ac", "2", "-y", str(extra / "extra-beep.wav"))
ffmpeg("-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=4",
       "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-shortest", "-y", str(WORK / "sfx-base.mp4"))
ffmpeg("-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=2",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
       "-y", str(WORK / "sfx-silent.mp4"))

# -- index ---------------------------------------------------------------
rc, payload = run(["sfx", "index", "--source", str(extra)])
data = payload.get("data") or {}
check("sfx index ok", payload.get("ok") is True, str(payload.get("error"))[:200])
counts = data.get("counts") or {}
check("index counts user files", counts.get("user") == 4, str(counts))
check("index counts --source dir", counts.get("extra") == 1, str(counts))
check("index writes index.json", (lib / "index.json").is_file(), "missing index.json")
entries = data.get("entries") or []
names = {entry["name"]: entry for entry in entries}
check("index derived name from filename", "叮 提示音" in names, str(sorted(names))[:200])
ding = names.get("叮 提示音") or {}
check("index probes duration/channels", ding.get("duration_s") == 0.4 and ding.get("channels") == 2, str(ding)[:200])
check("index derives english tags", {"ding", "hint"} & set(ding.get("tags") or []) == {"ding", "hint"}, str(ding.get("tags"))[:160])
check("index keeps id/path/bytes", bool(ding.get("id")) and Path(ding.get("path", "")).is_file() and ding.get("bytes"), str(ding)[:160])

# cache harvest must be read-only: count + mtimes unchanged
cache_root = Path.home() / "Movies/JianyingPro/User Data/Cache"
before = {str(p): p.stat().st_mtime for p in cache_root.rglob("*") if p.suffix.lower() in (".mp3", ".wav")} if cache_root.is_dir() else {}
rc, payload = run(["sfx", "index", "--source", str(extra)])
after = {str(p): p.stat().st_mtime for p in cache_root.rglob("*") if p.suffix.lower() in (".mp3", ".wav")} if cache_root.is_dir() else {}
check("sfx index leaves 剪映 cache untouched", before == after, "cache mtime/set changed")

# -- search --------------------------------------------------------------
rc, payload = run(["sfx", "search", "叮"])
results = (payload.get("data") or {}).get("results") or []
check("search 叮 finds it", results and results[0]["name"] == "叮 提示音", str(results)[:200])
rc, payload = run(["sfx", "search", "ding"])
results = (payload.get("data") or {}).get("results") or []
# 提示音* also maps to the "ding" tag (a hint sound), so assert membership not rank
check("search by english tag", any(row["name"] == "叮 提示音" for row in results), str(results)[:200])
rc, payload = run(["sfx", "search", "whoosh"])
results = (payload.get("data") or {}).get("results") or []
check("search whoosh finds the mp3", results and "whoosh" in results[0]["name"], str(results)[:200])
rc, payload = run(["sfx", "search", "提示音", "--max-duration", "0.35"])
results = (payload.get("data") or {}).get("results") or []
check("search duration filter", all(float(row["duration_s"]) <= 0.35 for row in results) and results, str(results)[:200])
rc, payload = run(["sfx", "search", "zzz-no-match"])
check("empty search explains itself", any("没有匹配" in w for w in payload.get("warnings") or []), str(payload.get("warnings"))[:160])

# -- ambiguity -----------------------------------------------------------
rc, payload = run(["sfx", "search", "提示音"])
check("提示音 has several candidates", (payload.get("data") or {}).get("matches", 0) >= 2, str(payload.get("data"))[:160])
rc, payload = run(["sfx", "add", "提示音", "--at", "1.0", "-i", str(WORK / "sfx-base.mp4"), "-o", str(WORK / "sfx-amb.mp4")])
error = payload.get("error") or {}
check("ambiguous add refuses (rc=2)", rc == 2, f"rc={rc}")
check("ambiguous add lists candidates", len((error.get("details") or {}).get("candidates") or []) >= 2, str(error)[:200])
check("ambiguous add writes nothing", not (WORK / "sfx-amb.mp4").exists(), "output created on ambiguity")

# -- add -----------------------------------------------------------------
ding_id = ding["id"]
out = WORK / "sfx-mixed.mp4"
rc, payload = run(["sfx", "add", ding_id, "--at", "1.2", "--gain", "3", "-i", str(WORK / "sfx-base.mp4"), "-o", str(out)])
data = payload.get("data") or {}
check("sfx add ok", payload.get("ok") is True, str(payload.get("error"))[:220])
check("sfx add keeps duration", data.get("video", {}).get("duration_s") == 4.0, str(data.get("video"))[:160])
check("sfx add keeps one audio stream", data.get("video", {}).get("audio_streams") == 1, str(data.get("video"))[:160])
check("sfx add reports placement", (data.get("placement") or {}).get("at_s") == 1.2, str(data.get("placement"))[:160])
verification = data.get("verification") or {}
check("sfx add boosts the window rms",
      verification.get("window_rms_db") is not None
      and verification.get("original_window_rms_db") is not None
      and verification["window_rms_db"] > verification["original_window_rms_db"] + 0.5,
      str(verification)[:200])
probe = subprocess.run([str(Path(FF).with_name("ffprobe")), "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out)],
                       capture_output=True, text=True)
check("ffprobe sees the mixed audio", probe.stdout.strip() == "aac", probe.stdout.strip()[:80])

rc, payload = run(["sfx", "add", ding_id, "--at", "1.2", "-i", str(WORK / "sfx-base.mp4"), "-o", str(out)])
check("sfx add refuses to overwrite", (payload.get("error") or {}).get("code") == "VALIDATION", str(payload.get("error"))[:160])

silent_out = WORK / "sfx-silent-mixed.mp4"
rc, payload = run(["sfx", "add", ding_id, "--at", "0.3", "-i", str(WORK / "sfx-silent.mp4"), "-o", str(silent_out)])
check("sfx add works on a silent video", payload.get("ok") is True, str(payload.get("error"))[:200])
check("silent-video path warns", any("没有音轨" in w for w in payload.get("warnings") or []), str(payload.get("warnings"))[:160])

rc, payload = run(["sfx", "add", ding_id, "--at", "99", "-i", str(WORK / "sfx-base.mp4"), "-o", str(WORK / "sfx-oob.mp4")])
check("--at beyond duration refused", (payload.get("error") or {}).get("code") == "VALIDATION", str(payload.get("error"))[:160])
rc, payload = run(["sfx", "add", "no-such-sound-xyz", "--at", "1", "-i", str(WORK / "sfx-base.mp4"), "-o", str(WORK / "sfx-nf.mp4")])
check("unknown selector NOT_FOUND", (payload.get("error") or {}).get("code") == "NOT_FOUND", str(payload.get("error"))[:160])

# -- label / list / forget ----------------------------------------------
rc, payload = run(["sfx", "label", ding_id, "--name", "叮-点击", "--tags", "click,ui"])
data = payload.get("data") or {}
check("sfx label renames the entry", (data.get("entry") or {}).get("name") == "叮-点击", str(data)[:160])
check("sfx label adds tags", {"click", "ui"} <= set((data.get("entry") or {}).get("tags") or []), str(data)[:160])
rc, payload = run(["sfx", "search", "click"])
check("labelled entry is searchable", bool((payload.get("data") or {}).get("results")), str(payload.get("data"))[:160])

rc, payload = run(["sfx", "list"])
data = payload.get("data") or {}
check("sfx list shows counts", (data.get("counts") or {}).get("total", 0) >= 5, str(data.get("counts")))
rc, payload = run(["sfx", "forget", ding_id])
data = payload.get("data") or {}
check("sfx forget drops the entry", len(data.get("forgot") or []) == 1, str(data)[:160])
check("sfx forget keeps the file", Path(ding["path"]).is_file(), "file deleted")
rc, payload = run(["sfx", "search", "click"])
check("forgotten entry is gone from search", not (payload.get("data") or {}).get("results"), str(payload.get("data"))[:160])

# -- doctor --------------------------------------------------------------
rc, payload = run(["doctor"])
sfx_state = (payload.get("data") or {}).get("sfx") or {}
check("doctor reports sfx", "indexed" in sfx_state and "cache_audio_files" in sfx_state, str(sfx_state)[:200])
check("doctor counts the index", sfx_state.get("indexed", 0) >= 4, str(sfx_state)[:200])

print(json.dumps({"ok": not failures, "failures": failures,
                  "indexed": (payload.get("data") or {}).get("sfx", {}).get("indexed")}, ensure_ascii=False))
raise SystemExit(0 if not failures else 1)
PY
if sfx_out="$("$PY" "$WORK/sfx_check.py" "$JY" "$WORK" "$FF" 2>&1)"; then
    pass "sfx: index + search + mix + label/forget [$(echo "$sfx_out" | head -c 200)]"
else
    fail "sfx checks" "$(echo "$sfx_out" | tail -c 700)"
fi

echo
echo "----------------------------------------"
printf 'smoke summary: %d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
if [[ "$FAIL" -ne 0 ]]; then
    echo "smoke result: FAIL"
    exit 1
fi
echo "smoke result: PASS"
