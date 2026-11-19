"""Automated GTA 6 live-stream clipper -> YouTube Shorts -> Google Drive uploader.

End-to-end pipeline:
  1. Discover top live GTA 6 streams on Twitch and YouTube (or use --url).
  2. Record the live stream for --minutes using live.py (catches chat spikes automatically).
     If no chat spikes trigger during the window, falls back to the recorded segments.
  3. Run moments.py (audio loudness + faster-whisper transcript + hype words) to pick the best 15-45s moments.
  4. Cut 1080x1920 vertical Shorts with cut.py (auto-facecam react layout, captions, title, credit, -14 LUFS).
  5. Validate each clip with check.py.
  6. Upload passing .mp4 clips + ready-to-paste YouTube Shorts .txt metadata to Google Drive
     (uses `gdrive` CLI in Antigravity cloud, or `rclone` in GitHub Actions / VPS).

usage:
  # Make 10 clips across top live streams and upload to Google Drive
  python scripts/autopilot.py --target-clips 10 --minutes 4 --max-clips 2 --cleanup

  # Single stream test
  python scripts/autopilot.py --url https://twitch.tv/kaicenat --minutes 5 --max-clips 2
"""
import argparse, json, os, re, shutil, sys, time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from common import ROOT, job_dir, load_json, run, save_json, tool
from check import BANNED
import streams as st_mod

STATE_FILE = ROOT / "autopilot_state.json"
DEFAULT_DRIVE_FOLDER = "GTA6-Shorts"
DEFAULT_DRIVE_FOLDER_ID = "1_E_al6Tmn1UKkwpyrI4pxWHM8PL0_3dk"


def py():
    return sys.executable


def load_state():
    if STATE_FILE.exists():
        try:
            return load_json(STATE_FILE)
        except Exception:
            pass
    return {"drive_folder_id": DEFAULT_DRIVE_FOLDER_ID, "uploaded": []}


def save_state(state):
    save_json(STATE_FILE, state)


def sanitize(text):
    out = re.sub(r"\s+", " ", (text or "")).strip()
    for b in BANNED:
        out = re.sub(re.escape(b), "", out, flags=re.I)
    return re.sub(r"\s+", " ", out).strip(" -|:,.")


def slugify(text):
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", (text or "clip")).strip("_")[:40] or "clip"


def detect_upload_backend(requested):
    if requested == "none":
        return "none"
    if requested in ("auto", "gdrive") and shutil.which("gdrive"):
        r = run([shutil.which("gdrive"), "auth", "status"], capture=True, check=False)
        if r.returncode == 0 and "Logged in" in (r.stdout or ""):
            return "gdrive"
        if requested == "gdrive":
            sys.exit("gdrive CLI is not logged in.")
    if requested in ("auto", "rclone") and shutil.which("rclone"):
        r = run([shutil.which("rclone"), "listremotes"], capture=True, check=False)
        remotes = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        if "gdrive:" in remotes:
            return "rclone"
        if requested == "rclone":
            sys.exit("rclone remote 'gdrive:' not configured. Set RCLONE_CONF or run `rclone config`.")
    return "none"


def resolve_drive_folder(folder_name, folder_id_arg, state, backend):
    if backend == "none":
        return None
    if folder_id_arg:
        return folder_id_arg
    if backend == "rclone":
        return os.environ.get("GDRIVE_FOLDER_ID") or state.get("drive_folder_id") or DEFAULT_DRIVE_FOLDER_ID
    if state.get("drive_folder_id") and folder_name == DEFAULT_DRIVE_FOLDER:
        return state["drive_folder_id"]
    gdrive = tool("gdrive")
    r = run([gdrive, "readonly", "search", "--name-exact", folder_name, "--type", "folder", "--json"],
            capture=True, check=False)
    if r.returncode == 0 and r.stdout.strip():
        try:
            items = json.loads(r.stdout)
            if isinstance(items, list) and items:
                fid = items[0].get("id") or items[0].get("Id")
                if fid:
                    state["drive_folder_id"] = fid
                    save_state(state)
                    return fid
        except Exception:
            pass
    r = run([gdrive, "mutate", "mkdir", folder_name, "--json"], capture=True)
    m = re.search(r"ID:\s*([A-Za-z0-9_-]+)", r.stdout)
    if m:
        fid = m.group(1)
    else:
        data = json.loads(r.stdout)
        fid = data.get("id") or data.get("Id")
    state["drive_folder_id"] = fid
    save_state(state)
    return fid


def upload_to_drive(local_path, folder_id, backend):
    if backend == "gdrive":
        gdrive = tool("gdrive")
        r = run([gdrive, "mutate", "upload", str(local_path), "--parent", folder_id, "--json"], capture=True)
        out = (r.stdout or "").strip()
        fid = ""
        try:
            data = json.loads(out)
            fid = data.get("id") or data.get("Id") or ""
        except Exception:
            m = re.search(r"(?:ID:|id[\"']?\s*:\s*[\"']?)([A-Za-z0-9_-]{15,})", out + "\n" + (r.stderr or ""))
            fid = m.group(1) if m else ""
        url = f"https://drive.google.com/file/d/{fid}/view" if fid else ""
        return fid, url, out
    if backend == "rclone":
        rclone = tool("rclone")
        cmd = [rclone, "copy", str(local_path), "gdrive:", "--drive-root-folder-id", folder_id]
        r = run(cmd, capture=True)
        return "", f"https://drive.google.com/drive/folders/{folder_id}", (r.stdout or "").strip()
    return "", "", ""


def pick_live_stream(platform, query, pick_index, skip_channels=()):
    rows = []
    if platform in ("twitch", "both"):
        rows += st_mod.twitch(query) or []
    if platform in ("youtube", "both"):
        rows += st_mod.youtube(query, 15) or []
    rows = [r for r in rows if r.get("url")]
    rows.sort(key=lambda r: r.get("viewers") or -1, reverse=True)
    if not rows:
        return None
    skip = {c.lower() for c in skip_channels}
    filtered = [r for r in rows if (r.get("channel") or "").lower() not in skip] or rows
    idx = max(0, min(pick_index - 1, len(filtered) - 1))
    chosen = filtered[idx]
    print(f"[stream] picked #{idx + 1}: {chosen['platform']} | {chosen['channel']} ({chosen.get('viewers')} viewers) | {chosen['url']}")
    return chosen


def concat_segments(live_dir, out_mp4):
    segs = sorted(live_dir.glob("seg_*.ts"))
    if not segs:
        return None
    lst = live_dir / "concat_all.txt"
    lst.write_text("".join(f"file '{s.resolve().as_posix()}'\n" for s in segs), encoding="utf-8")
    r = run([tool("ffmpeg"), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", "-movflags", "+faststart", str(out_mp4)], check=False, capture=True)
    lst.unlink(missing_ok=True)
    if r.returncode == 0 and out_mp4.exists() and out_mp4.stat().st_size > 0:
        return out_mp4
    return None


def make_title(channel, cand_text):
    clean_chan = sanitize(channel) or "Streamer"
    txt = sanitize(cand_text)
    if txt:
        words = txt.split()
        snippet = " ".join(words[:7]).strip(" ,.?!")
        if 3 <= len(snippet.split()) <= 7 and len(snippet) <= 46:
            return f"{clean_chan}: \"{snippet}\""
    return f"{clean_chan} loses it playing GTA 6"


def build_shorts_metadata(title, channel, source_url, cand):
    credit = f"@{channel.lstrip('@')}"
    desc = "\n".join([
        title,
        "",
        f"Clip: {credit}",
        f"Stream: {source_url}" if source_url else "",
        "",
        "#GTA6 #GTAVI #Shorts #Gaming",
    ]).strip()
    return (
        f"TITLE (YouTube Shorts):\n{title[:95]} #GTA6 #Shorts\n\n"
        f"DESCRIPTION:\n{desc}\n\n"
        f"CLIP INFO:\n"
        f"- Duration: {cand['end'] - cand['start']:.1f}s\n"
        f"- Score: {cand.get('score')}\n"
        f"- Signals: {', '.join(cand.get('why') or [])}\n"
        f"- Transcript: {cand.get('text', '')}\n"
    )


def process_video_source(video_path, channel, source_url, args, folder_id, backend, state):
    print(f"[moments] analyzing {video_path} ...")
    r = run([py(), str(SCRIPTS / "moments.py"), str(video_path),
             "--top", str(max(args.max_clips * 2, 4)),
             "--min", str(args.min_secs), "--max", str(args.max_secs)], check=False)
    if r.returncode != 0:
        print(f"[warn] moments.py failed on {video_path}")
        return []
    jd = job_dir(str(video_path))
    cands_file = jd / "candidates.json"
    if not cands_file.exists():
        print(f"[warn] no candidates.json for {video_path}")
        return []
    data = load_json(cands_file)
    cands = sorted(data.get("candidates", []), key=lambda x: x.get("score", 0), reverse=True)
    if not cands:
        print(f"[warn] 0 candidate moments in {video_path}")
        return []

    credit = f"@{channel.lstrip('@')}"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    uploaded = []

    for cand in cands:
        if len(uploaded) >= args.max_clips:
            break
        n = cand["n"]
        title = sanitize(make_title(channel, cand.get("text", "")))
        cut_cmd = [
            py(), str(SCRIPTS / "cut.py"), str(jd),
            "--pick", str(n),
            "--layout", args.layout,
            "--cam", "auto",
            "--credit", credit,
            "--captions", args.captions,
        ]
        if args.layout == "react":
            cut_cmd += ["--title", title, "--title-style", args.title_style]
        else:
            cut_cmd += ["--hook", title]
        print(f"[cut] cutting candidate #{n} ({cand['start']}s-{cand['end']}s, score={cand['score']}) ...")
        rc = run(cut_cmd, check=False)
        if rc.returncode != 0:
            print(f"[warn] cut.py failed on candidate #{n}")
            continue

        clip_mp4 = jd / "edits" / f"clip_{n}.mp4"
        if not clip_mp4.exists():
            continue

        chk = run([py(), str(SCRIPTS / "check.py"), str(clip_mp4), "--caption", title], check=False, capture=True)
        print(chk.stdout.strip())
        if chk.returncode != 0:
            print(f"[skip] clip_{n}.mp4 failed check.py")
            continue

        out_dir = ROOT / "ready_shorts"
        out_dir.mkdir(exist_ok=True)
        base_name = f"{stamp}_{slugify(channel)}_short{len(uploaded) + 1}"
        final_mp4 = out_dir / f"{base_name}.mp4"
        final_txt = out_dir / f"{base_name}.txt"
        shutil.copy2(clip_mp4, final_mp4)
        final_txt.write_text(build_shorts_metadata(title, channel, source_url, cand), encoding="utf-8")

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "channel": channel,
            "source_url": source_url,
            "title": title,
            "local_mp4": str(final_mp4),
            "local_txt": str(final_txt),
        }

        if backend != "none" and folder_id:
            print(f"[drive:{backend}] uploading {final_mp4.name} + {final_txt.name} -> folder {folder_id} ...")
            fid_mp4, url_mp4, _ = upload_to_drive(final_mp4, folder_id, backend)
            fid_txt, url_txt, _ = upload_to_drive(final_txt, folder_id, backend)
            entry["drive_mp4_id"] = fid_mp4
            entry["drive_mp4_url"] = url_mp4
            entry["drive_txt_id"] = fid_txt
            entry["drive_txt_url"] = url_txt
            print(f"[done] uploaded: {final_mp4.name} -> {url_mp4 or fid_mp4}")
        else:
            print(f"[done] saved locally (no Drive upload backend): {final_mp4}")

        state.setdefault("uploaded", []).append(entry)
        save_state(state)
        uploaded.append(entry)

    if args.cleanup:
        shutil.rmtree(jd, ignore_errors=True)
    return uploaded


def run_once(args, state, folder_id, backend, skip_channels, max_for_stream):
    if args.url:
        url = args.url
        m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", url)
        channel = args.channel or (m.group(1) if m else "creator")
    else:
        stream = pick_live_stream(args.platform, args.query, args.pick_stream, skip_channels=skip_channels)
        if not stream:
            print("[warn] no live streams found.")
            return []
        url = stream["url"]
        channel = stream["channel"]
        skip_channels.append(channel)

    print(f"[live] recording {url} for {args.minutes:g} min ...")
    rl = run([py(), str(SCRIPTS / "live.py"), url, "--minutes", str(args.minutes),
              "--before", "15", "--after", "20"], check=False)
    if rl.returncode != 0:
        print(f"[warn] live.py exited with {rl.returncode} for {url}")

    stream_job = job_dir(url)
    ld = stream_job / "live"
    spikes = sorted(ld.glob("spike_*.mp4"))
    sources = []
    if spikes:
        print(f"[live] caught {len(spikes)} chat spike clip(s)!")
        sources = spikes[: max_for_stream]
    else:
        print("[live] no chat spikes triggered in this window; analyzing full recorded segments instead ...")
        fallback = concat_segments(ld, ld / "recorded_full.mp4")
        if fallback:
            sources = [fallback]

    if not sources:
        print("[warn] no recorded video found for this stream.")
        if args.cleanup:
            shutil.rmtree(stream_job, ignore_errors=True)
        return []

    all_uploaded = []
    for src in sources:
        if len(all_uploaded) >= max_for_stream:
            break
        remaining = max_for_stream - len(all_uploaded)
        sub_args = argparse.Namespace(**vars(args))
        sub_args.max_clips = remaining
        all_uploaded += process_video_source(src, channel, url, sub_args, folder_id, backend, state)

    if args.cleanup:
        shutil.rmtree(stream_job, ignore_errors=True)
    return all_uploaded


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", choices=["twitch", "youtube", "both"], default="both")
    ap.add_argument("--query", default="gta 6")
    ap.add_argument("--url", help="specific Twitch or YouTube live stream URL (skips discovery)")
    ap.add_argument("--channel", help="creator handle when using --url")
    ap.add_argument("--pick-stream", type=int, default=1, help="which ranked live stream to pick (1 = highest viewers)")
    ap.add_argument("--minutes", type=float, default=4, help="minutes to record per stream")
    ap.add_argument("--max-clips", type=int, default=2, help="max YouTube Shorts to cut per stream")
    ap.add_argument("--target-clips", type=int, default=0, help="keep sampling streams until N total Shorts are produced (overrides --loops)")
    ap.add_argument("--max-attempts", type=int, default=10, help="max streams to try when --target-clips is set")
    ap.add_argument("--min-secs", type=float, default=15)
    ap.add_argument("--max-secs", type=float, default=45)
    ap.add_argument("--layout", choices=["react", "blur", "fill", "facecam"], default="react")
    ap.add_argument("--title-style", choices=["box", "outline"], default="box")
    ap.add_argument("--captions", choices=["word", "bubble", "none"], default="word")
    ap.add_argument("--drive-folder", default=DEFAULT_DRIVE_FOLDER, help="Google Drive folder name")
    ap.add_argument("--drive-folder-id", default="", help="explicit Google Drive folder ID")
    ap.add_argument("--upload-backend", choices=["auto", "gdrive", "rclone", "none"], default="auto")
    ap.add_argument("--no-upload", action="store_true", help="keep clips local only, skip Drive upload")
    ap.add_argument("--cleanup", action="store_true", help="remove temporary jobs/ files after each stream")
    ap.add_argument("--loops", type=int, default=1, help="how many stream rounds to run (when --target-clips is 0)")
    ap.add_argument("--sleep", type=float, default=10, help="seconds to wait between streams")
    args = ap.parse_args()

    state = load_state()
    backend = "none" if args.no_upload else detect_upload_backend(args.upload_backend)
    folder_id = resolve_drive_folder(args.drive_folder, args.drive_folder_id, state, backend)
    if backend != "none" and folder_id:
        print(f"[drive] backend={backend} | target folder: {args.drive_folder} (https://drive.google.com/drive/folders/{folder_id})")
    else:
        print("[drive] upload disabled or no backend configured; clips will be saved in ready_shorts/")

    skip_channels = []
    total = []

    if args.target_clips > 0:
        attempt = 0
        while len(total) < args.target_clips and attempt < args.max_attempts:
            attempt += 1
            need = min(args.max_clips, args.target_clips - len(total))
            print(f"\n=== TARGET {len(total)}/{args.target_clips} | STREAM ATTEMPT {attempt}/{args.max_attempts} ===")
            total += run_once(args, state, folder_id, backend, skip_channels, need)
            if len(total) < args.target_clips and attempt < args.max_attempts:
                time.sleep(args.sleep)
    else:
        for i in range(1, args.loops + 1):
            print(f"\n=== LOOP {i}/{args.loops} ===")
            total += run_once(args, state, folder_id, backend, skip_channels, args.max_clips)
            if i < args.loops:
                time.sleep(args.sleep)

    print(f"\n=== SUMMARY: {len(total)} Short(s) ready ===")
    for item in total:
        print(f"- {Path(item['local_mp4']).name} | {item['title']} | {item.get('drive_mp4_url') or item['local_mp4']}")


if __name__ == "__main__":
    main()
