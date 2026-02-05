#!/usr/bin/env python3
"""
Whisper Transcribe Plugin

This plugin integrates with Whisper ASR Webservice or Whisper.cpp
to automatically generate subtitles (SRT) for video files when a scene is updated.
It follows the same structure as the example RenameFile plugin.
"""

import os
import sys
import traceback
import subprocess
import tempfile
import json
import urllib.request
import urllib.error

# Stash helper classes (same as in the RenameFile example)
try:
    from StashPluginHelper import StashPluginHelper, taskQueue
except Exception:
    # Fallback to local minimal helper if StashPluginHelper isn't available
    from stash_helper_fallback import StashPluginHelper, taskQueue  # type: ignore

# GraphQL helper utilities to fetch plugin settings when the helper can't.
def _build_graphql_url(server_connection: dict) -> str:
    scheme = (server_connection or {}).get("Scheme") or (server_connection or {}).get("scheme") or "http"
    host = (server_connection or {}).get("Host") or (server_connection or {}).get("host") or "127.0.01"
    port = (server_connection or {}).get("Port") or (server_connection or {}).get("port")
    if port:
        return f"{scheme}://{host}:{port}/graphql"
    return f"{scheme}://{host}/graphql"


def _cookie_header(session_cookie: object) -> str | None:
    # Expecting a dict-like cookie with Name/Value (case-insensitive)
    try:
        if isinstance(session_cookie, dict):
            name = session_cookie.get("Name") or session_cookie.get("name")
            value = session_cookie.get("Value") or session_cookie.get("value")
            if name and value:
                return f"{name}={value}"
    except Exception:
        pass
    return None


def _fetch_server_url_from_settings(json_input: dict) -> str | None:
    """
    Best-effort fetch of this plugin's saved 'serverUrl' from Stash via GraphQL.
    Works even when running with the minimal fallback helper.
    """
    try:
        conn = (json_input or {}).get("server_connection") or (json_input or {}).get("ServerConnection") or {}
        graphql_url = _build_graphql_url(conn)
        cookie = (conn.get("SessionCookie") or conn.get("session_cookie") or conn.get("sessionCookie"))
        cookie_hdr = _cookie_header(cookie)

        # Query the configuration endpoint, which includes plugin configuration values.
        query = """
            query($ids: [ID!]) {
                configuration {
                    plugins(include: $ids)
                }
            }
        """
        headers = {"Content-Type": "application/json"}
        if cookie_hdr:
            headers["Cookie"] = cookie_hdr
        # Ask for both common plugin IDs to maximise compatibility.
        variables = {"ids": ["whisper_transcribe", "WhisperTranscribe"]}
        payload = json.dumps({"query": query, "variables": variables})

        try:
            import requests  # type: ignore
        except Exception:
            requests = None

        if requests is not None:
            resp = requests.post(graphql_url, data=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        else:
            req = urllib.request.Request(graphql_url, data=payload.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        config_plugins = (((data or {}).get("data") or {}).get("configuration") or {}).get("plugins") or {}
        if not isinstance(config_plugins, dict):
            return None

        # Configuration plugins come back as a map of pluginID -> map of settings.
        for pid in variables["ids"]:
            settings_map = config_plugins.get(pid)
            if isinstance(settings_map, dict):
                v = settings_map.get("serverUrl")
                if isinstance(v, str) and v.strip():
                    return v.strip()

        return None
    except Exception:
        # Silent best-effort
        return None

# Self-contained transcription logic (no external imports).
def _post_whisper_audio(wav_path: str, server_url: str, translate: bool, backend_type: str = "whisper-asr") -> str:
    try:
        import requests  # type: ignore
    except Exception:
        requests = None

    # Build request based on backend type
    if backend_type == "whisper-cpp":
        # Whisper.cpp format
        url_with_params = server_url
        field_name = "file"
        use_form_data = True
    else:
        # Whisper ASR Webservice format (default)
        task = "translate" if translate else "transcribe"
        url_with_params = f"{server_url}?output=srt&task={task}&encode=true"
        field_name = "audio_file"
        use_form_data = False

    if requests is not None:
        with open(wav_path, "rb") as audio_file:
            files = {field_name: (os.path.basename(wav_path), audio_file, "audio/wav")}
            data = {}
            if use_form_data:
                data = {"response_format": "srt"}
                if translate:
                    data["translate"] = "true"
            try:
                resp = requests.post(url_with_params, files=files, data=data, timeout=3600)
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                raise RuntimeError(f"Error sending request to whisper server at {server_url}. Is it running and reachable? {e}") from e
    else:
        boundary = "----WhisperBoundary7MA4YWxkTrZu0gW"

        with open(wav_path, "rb") as f:
            file_content = f.read()

        def _encode_part(name: str, value: str) -> bytes:
            return (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")

        parts = []
        if use_form_data:
            parts.append(_encode_part("response_format", "srt"))
            if translate:
                parts.append(_encode_part("translate", "true"))
        
        file_header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; filename="{os.path.basename(wav_path)}"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
        parts.append(file_header)
        parts.append(file_content)
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        req = urllib.request.Request(
            url_with_params,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else str(e)
            raise RuntimeError(f"HTTP error from whisper server: {e.code} {e.reason}. {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error contacting whisper server at {server_url}: {e}") from e
        except Exception as e:
            raise RuntimeError(f"Unexpected error contacting whisper server: {e}") from e


def _check_whisper_server(server_url: str, timeout: float = 5.0) -> None:
    """
    Best-effort connectivity check. We only verify that a TCP connection can be made.
    Any HTTP status code is considered "reachable".
    """
    try:
        import requests  # type: ignore
    except Exception:
        requests = None

    if requests is not None:
        try:
            # OPTIONS is commonly allowed; even a 4xx/5xx means the server is reachable.
            requests.options(server_url, timeout=timeout)
        except Exception as e:
            raise RuntimeError(
                f"Cannot reach whisper server at {server_url}. "
                "Configure the 'Whisper Server URL' plugin setting or set WHISPER_SERVER_URL. "
                f"Underlying error: {e}"
            ) from e
    else:
        # Fallback to urllib request if `requests` is unavailable.
        req = urllib.request.Request(server_url, method="OPTIONS")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as _:
                return
        except urllib.error.HTTPError:
            # Server reachable (wrong method) – that's sufficient to proceed.
            return
        except Exception as e:
            raise RuntimeError(
                f"Cannot reach whisper server at {server_url}. "
                "Configure the 'Whisper Server URL' plugin setting or set WHISPER_SERVER_URL. "
                f"Underlying error: {e}"
            ) from e


def _build_caption_path(video_path: str, language_code: str | None = None) -> str:
    """
    Build an SRT path next to the video. If a language code is provided, suffix it
    (e.g. video.en.srt) so Stash can associate captions without a full rescan.
    """
    base, _ = os.path.splitext(video_path)
    lang = (language_code or "").strip().lower()
    if lang:
        return f"{base}.{lang}.srt"
    return f"{base}.srt"


def _trigger_metadata_scan(paths: list[str]) -> None:
    """
    Kick off a targeted metadata scan for the provided paths so Stash will register
    newly created caption files without needing a full library rescan.
    """
    if not paths:
        return

    mutation = """
    mutation ScanCaptions($input: ScanMetadataInput!) {
      metadataScan(input: $input)
    }
    """
    variables = {
        "input": {
            "paths": paths,
            "rescan": True,
            "scanGenerateCovers": False,
            "scanGeneratePreviews": False,
            "scanGenerateImagePreviews": False,
            "scanGenerateSprites": False,
            "scanGeneratePhashes": False,
            "scanGenerateThumbnails": False,
            "scanGenerateClipPreviews": False,
        }
    }

    try:
        resp = stash._graphql(mutation, variables)
        job_id = None
        if isinstance(resp, dict):
            data = resp.get("data") or {}
            job_id = data.get("metadataScan")
        if job_id:
            stash.Log(f"Triggered caption metadata scan (job {job_id}) for: {paths}")
        else:
            stash.Warn(f"Metadata scan triggered for captions but no job id returned. Paths={paths}")
    except Exception as e:
        stash.Warn(f"Failed to start metadata scan for captions: {e}")


def transcribe_video(video_path: str, translate: bool = False, server_url: str = "http://127.0.0.1:9000/asr", caption_language: str | None = None, backend_type: str = "whisper-asr") -> str:
    """
    Transcribes a video file using Whisper ASR Webservice or Whisper.cpp. Produces an .srt next to the video
    and returns the caption path.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found at '{video_path}'")

    # Verify server reachability before doing any work.
    _check_whisper_server(server_url, timeout)

    tmp_wav_path = None
    try:
        # 1. Extract audio to 16kHz mono WAV using ffmpeg
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav_file:
            tmp_wav_path = tmp_wav_file.name

        command = [
            "ffmpeg",
            "-i", video_path,
            "-ar", "16000",
            "-ac", "1",
            "-c:a", "pcm_s16le",
            "-y",
            "-loglevel", "error",
            tmp_wav_path,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, errors="ignore")
        except FileNotFoundError:
            raise RuntimeError("'ffmpeg' not found. Please ensure it is installed and in PATH.")
        except subprocess.CalledProcessError as e:
            stderr = getattr(e, "stderr", "") or ""
            raise RuntimeError(f"ffmpeg failed to extract audio: {stderr}") from e

        # 2. Send audio to whisper server
        response_text = _post_whisper_audio(tmp_wav_path, server_url, translate, backend_type)

    finally:
        # Clean up temporary WAV file
        if tmp_wav_path and os.path.exists(tmp_wav_path):
            try:
                os.remove(tmp_wav_path)
            except Exception:
                pass

    # 3. Save response to SRT file
    srt_path = _build_caption_path(video_path, caption_language)
    try:
        with open(srt_path, "w", encoding="utf-8") as srt_file:
            srt_file.write(response_text)
    except OSError as e:
        raise RuntimeError(f"Failed to write SRT file '{srt_path}': {e}") from e
    return srt_path
# ----------------------------------------------------------------------
# Minimal plugin settings – can be extended via a settings file if needed.
# ----------------------------------------------------------------------
settings = {
    # Defaults here are overridden by saved plugin settings from the UI.
    # The server URL is resolved dynamically; we do not set a hard‑coded default here
    # to avoid overriding a user‑provided UI setting. Fallbacks are handled in
    # _resolve_server_url() (environment variable, then built‑in default).
    "backendType": "whisper-asr",
    "translateToEnglish": False,
    "zzdebugTracing": False,
    "zzdryRun": False,
}
# Load optional configuration (currently empty, but kept for parity with RenameFile)
try:
    from whisper_transcribe_settings import config  # type: ignore
except Exception:
    config = {}

stash = StashPluginHelper(
    settings=settings,
    config=config,
    maxbytes=10 * 1024 * 1024,
)

# ----------------------------------------------------------------------
# Resolve the Whisper server URL – this logic works for every Stash payload
# format (dict settings, list of {key,value}, or ``pluginSettings``).
# ----------------------------------------------------------------------
def _resolve_server_url() -> str:
    """
    Resolve the Whisper server URL using the Stash helper's Setting() method,
    which already knows how to read UI settings in all supported formats.
    Precedence (high → low):
        1. Explicit ``serverUrl`` argument passed in ``args``.
        2. UI‑saved setting (dict, list of {key,value}, or ``pluginSettings``).
        3. Direct extraction from raw JSON payload (covers cases where the helper
           does not correctly read the UI settings).
        4. Fallback to GraphQL fetch of plugin settings (for edge cases).
        5. Environment variable ``WHISPER_SERVER_URL``.
        6. Built‑in default.
    """
    # 1️⃣ explicit arg
    arg_url = ((stash.JSON_INPUT or {}).get("args") or {}).get("serverUrl")
    if isinstance(arg_url, str) and arg_url.strip():
        return arg_url.strip()

    # 2️⃣ UI‑saved setting via the helper (covers dict, list, pluginSettings)
    ui_url = stash.Setting("serverUrl", None)
    if isinstance(ui_url, str) and ui_url.strip():
        return ui_url.strip()

    # 3️⃣ Direct extraction from raw JSON payload (fallback if helper fails)
    raw_url = None
    if isinstance(stash.JSON_INPUT, dict):
        # Check top‑level 'settings' (dict or list)
        settings_src = stash.JSON_INPUT.get("settings") or {}
        if isinstance(settings_src, dict):
            raw_url = settings_src.get("serverUrl")
        elif isinstance(settings_src, list):
            for item in settings_src:
                if isinstance(item, dict) and item.get("key") == "serverUrl":
                    raw_url = item.get("value")
                    break

        # If not found, check 'pluginSettings' (dict or list)
        if not raw_url:
            alt_src = stash.JSON_INPUT.get("pluginSettings") or {}
            if isinstance(alt_src, dict):
                raw_url = alt_src.get("serverUrl")
            elif isinstance(alt_src, list):
                for item in alt_src:
                    if isinstance(item, dict) and item.get("key") == "serverUrl":
                        raw_url = item.get("value")
                        break

    if isinstance(raw_url, str) and raw_url.strip():
        return raw_url.strip()

    # 4️⃣ Fallback to GraphQL fetch of plugin settings (covers cases where
    #    the helper cannot read the UI settings directly)
    fetched_url = _fetch_server_url_from_settings(stash.JSON_INPUT or {})
    if isinstance(fetched_url, str) and fetched_url.strip():
        return fetched_url.strip()

    # 5️⃣ environment variable
    env_url = os.getenv("WHISPER_SERVER_URL")
    if isinstance(env_url, str) and env_url.strip():
        return env_url.strip()

    # 6️⃣ built‑in default - choose based on backend type
    backend = stash.Setting("backendType", "whisper-asr")
    if backend == "whisper-cpp":
        return "http://127.0.0.1:9191/inference"
    else:
        return "http://127.0.0.1:9000/asr"

# Resolve once at import time (the value is immutable for the lifetime of the run)
server_url = _resolve_server_url()

backend_type = stash.Setting("backendType", "whisper-asr")
translate_to_english = stash.Setting("translateToEnglish", False)
dry_run = stash.Setting("zzdryRun", False)
# New timeout setting (seconds) – defaults to 3600 seconds if not configured.
timeout = stash.Setting("timeout", 3600.0)

# Optional debug trace of resolved server URL
try:
    if stash.Setting("zzdebugTracing", False):
        stash.Log(f"[WhisperTranscribe] Resolved serverUrl={server_url!r}")
except Exception:
    pass

# Detect if invoked by a Scene.Update.Post hook and capture scene ID if provided
inputToUpdateScenePost = False
hookSceneID = None
try:
    hook_ctx = stash.JSON_INPUT.get("args", {}).get("hookContext") if stash.JSON_INPUT else None
    if hook_ctx is not None:
        # When input is None, treat as no-op. Otherwise mark as hook trigger.
        if hook_ctx.get("input") is not None:
            inputToUpdateScenePost = True
            if hook_ctx.get("id") is not None:
                hookSceneID = int(hook_ctx.get("id"))
except Exception:
    # best-effort only
    pass

# ----------------------------------------------------------------------
# Helper: transcribe a single scene's primary video file.
# ----------------------------------------------------------------------
def transcribe_scene(scene_id: int):
    """Fetch the scene's video file and run transcription."""
    try:
        # Minimal fragment to get the needed fields.
        fragment = """
            id title files { id path }
        """
        scene = stash.find_scene(scene_id, fragment)
        if not scene:
            stash.Error(f"Scene {scene_id} not found.")
            return

        if not scene.get("files"):
            stash.Warn(f"Scene {scene_id} has no associated files.")
            return

        video_path = scene["files"][0]["path"]
        if not os.path.isfile(video_path):
            stash.Warn(f"Video file does not exist: {video_path}")
            return


        # Call the shared transcription helper.
        # Use configured translate/server_url settings, and support dry-run.
        caption_language = "en" if translate_to_english else None
        caption_path = _build_caption_path(video_path, caption_language)
        if dry_run:
            stash.Log(f"Dry-run: would transcribe '{video_path}' -> '{caption_path}' (translate={translate_to_english}, server_url={server_url})")
        else:
            # Queue metadata scan before transcription starts
            try:
                _trigger_metadata_scan([caption_path])
            except Exception as e:
                stash.Warn(f"Failed to start metadata scan for captions on scene {scene_id}: {e}")
            caption_path = transcribe_video(
                video_path,
                translate=translate_to_english,
                server_url=server_url,
                caption_language=caption_language,
                backend_type=backend_type,
            )

        stash.Log(f"Transcription completed for scene {scene_id} (file: {video_path})")
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"Exception in transcribe_scene: {e}\nTraceBack={tb}")

# ----------------------------------------------------------------------
# Task: transcribe the most recently updated scene.
# ----------------------------------------------------------------------
def transcribe_last_scene():
    """Find the latest updated scene and run transcription on it."""
    try:
        all_scenes = stash.get_all_scenes()["allScenes"]
        if not all_scenes:
            stash.Error("No scenes found.")
            return

        latest = max(all_scenes, key=lambda s: s["updated_at"])
        scene_id = latest.get("id")
        if scene_id is None:
            stash.Error("Latest scene has no ID.")
            return

        transcribe_scene(scene_id)
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"Exception in transcribe_last_scene: {e}\nTraceBack={tb}")

def transcribe_scene_task():
    """
    Entry point used by the UI button.
    Expects a `scene_id` argument in the plugin's JSON input.
    """
    try:
        scene_id = stash.JSON_INPUT.get("args", {}).get("scene_id")
        if scene_id is None:
            stash.Error("No scene_id supplied to transcribe_scene_task")
            return
        scene_id = int(scene_id)
        transcribe_scene(scene_id)
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"Exception in transcribe_scene_task: {e}\nTraceBack={tb}")

# ----------------------------------------------------------------------
# Main entry point – mirrors the pattern used by RenameFile.
# ----------------------------------------------------------------------
try:
    if stash.PLUGIN_TASK_NAME == "transcribe_last_scene":
        stash.Trace(f"PLUGIN_TASK_NAME={stash.PLUGIN_TASK_NAME}")
        transcribe_last_scene()
    elif stash.PLUGIN_TASK_NAME == "transcribe_scene_task":
        stash.Trace(f"PLUGIN_TASK_NAME={stash.PLUGIN_TASK_NAME}")
        transcribe_scene_task()
    elif stash.JSON_INPUT and stash.JSON_INPUT.get("args", {}).get("mode") == "transcribe_scene_task":
        stash.Trace("Dispatch via args.mode=transcribe_scene_task")
        transcribe_scene_task()
    elif 'inputToUpdateScenePost' in globals() and inputToUpdateScenePost:
        stash.Trace("Triggered by Scene.Update.Post hook")
        if 'hookSceneID' in globals() and hookSceneID is not None:
            transcribe_scene(hookSceneID)
        else:
            # Fallback to latest scene if no id in hook context
            transcribe_last_scene()
    else:
        stash.Trace(f"No task specified (PLUGIN_TASK_NAME={stash.PLUGIN_TASK_NAME}). Nothing to do.")
except Exception as e:
    tb = traceback.format_exc()
    stash.Error(f"Exception while running plugin: {e}\nTraceBack={tb}")

stash.Trace("\n*********************************\nEXITING   ***********************\n*********************************")
