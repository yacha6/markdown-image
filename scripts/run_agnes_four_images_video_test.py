#!/usr/bin/env python3
"""Run one Agnes Video 2.5 reference test with four images and one video.

The API key is read only from AGNES_API_KEY. The creation response's `id` is
used directly as `video_id` when polling the Agnes result endpoint.
"""

import argparse
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    import certifi
except ImportError:
    certifi = None


MODEL = "agnes-video-2.5"
DEFAULT_API_ROOT = "https://apihub.agnes-ai.com"
DEFAULT_IMAGE_URL = (
    "https://dramacdn.lingjuta.com/character/image/2026/05/20/"
    "91105e8d-87a1-47c9-95d5-0f22652412cd.png"
)
TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504, 520, 522, 524}
SSL_CONTEXT = ssl.create_default_context(
    cafile=certifi.where() if certifi is not None else None
)


class HttpFailure(Exception):
    def __init__(self, status_code, body):
        super().__init__(f"HTTP {status_code}: {body}")
        self.status_code = status_code
        self.body = body


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def request_json(method, url, api_key, body=None, attempts=5):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "agnes-four-images-video-test/1.0",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"

    for attempt in range(attempts):
        request = urllib.request.Request(
            url, data=payload, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                request, timeout=90, context=SSL_CONTEXT
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
                try:
                    return response.status, json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise HttpFailure(response.status, raw) from exc
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code not in TRANSIENT_HTTP_CODES or attempt == attempts - 1:
                raise HttpFailure(exc.code, raw) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts - 1:
                raise

        delay = min(16.0, 2**attempt) + random.uniform(0, 0.25)
        time.sleep(delay)

    raise RuntimeError("request failed")


def extract_error(data):
    if isinstance(data, dict):
        for key in ("error", "message", "detail", "reason"):
            if data.get(key):
                return data[key]
    return data


def parse_args():
    parser = argparse.ArgumentParser(
        description="Test agnes-video-2.5 with four identical images and one video."
    )
    parser.add_argument("--video-url", required=True, help="Public MP4 URL")
    parser.add_argument(
        "--image-url", default=DEFAULT_IMAGE_URL, help="Image URL repeated four times"
    )
    parser.add_argument(
        "--api-root",
        default=os.environ.get("AGNES_API_ROOT", DEFAULT_API_ROOT),
        help="Agnes API root without /v1",
    )
    parser.add_argument("--seconds", default="5", choices=[str(i) for i in range(4, 13)])
    parser.add_argument("--poll-interval", type=float, default=1.5)
    parser.add_argument("--timeout", type=float, default=1200.0)
    parser.add_argument("--start-seconds", type=float, default=0.0)
    parser.add_argument(
        "--require-audio",
        action="store_true",
        help="Require the reference video's audio track",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    api_key = os.environ.get("AGNES_API_KEY")
    if not api_key:
        print("AGNES_API_KEY is required", file=sys.stderr)
        return 2

    api_root = args.api_root.rstrip("/")
    create_url = f"{api_root}/v1/videos"
    poll_url = f"{api_root}/agnesapi"
    prompt = (
        "Create a calm, family-friendly cinematic scene featuring the fully clothed "
        "adult woman shown in <Picture 1>, <Picture 2>, <Picture 3>, and <Picture 4>. "
        "Preserve her elegant light-blue traditional-style gown, hairstyle, ornaments, "
        "and mature facial appearance. She walks slowly through a bright blossom garden "
        "while a gentle breeze moves her sleeves and hair. Use <Video 1> only as a "
        "reference for smooth camera movement, natural motion pacing, and shot timing. "
        "Soft daylight, peaceful atmosphere, graceful movement, polished cinematic quality."
    )
    body = {
        "model": MODEL,
        "mode": "reference",
        "prompt": prompt,
        "seconds": args.seconds,
        "images": [args.image_url] * 4,
        "videos": [
            {
                "url": args.video_url,
                "start_seconds": args.start_seconds,
                "require_audio": args.require_audio,
            }
        ],
        "size": "720P",
        "aspect_ratio": "16:9",
        "n": 1,
    }

    started = time.monotonic()
    video_id = None
    request_seconds = None
    print(
        json.dumps(
            {
                "event": "request_start",
                "started_at": utc_now(),
                "model": MODEL,
                "video_url": args.video_url,
                "image_url": args.image_url,
                "image_count": 4,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    try:
        create_status, create_data = request_json(
            "POST", create_url, api_key, body
        )
        request_seconds = time.monotonic() - started
        if not isinstance(create_data, dict) or not create_data.get("id"):
            raise RuntimeError(
                "Creation response did not contain id: "
                + json.dumps(create_data, ensure_ascii=False)
            )

        # agnes-video-2.5 returns the polling video_id in the `id` field.
        video_id = str(create_data["id"])
        print(
            json.dumps(
                {
                    "event": "video_id_received",
                    "http_status": create_status,
                    "video_id": video_id,
                    "request_seconds": round(request_seconds, 3),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        query = urllib.parse.urlencode(
            {"video_id": video_id, "model_name": MODEL}
        )
        last_status = None
        while time.monotonic() - started < args.timeout:
            _, poll_data = request_json("GET", f"{poll_url}?{query}", api_key)
            status = str(poll_data.get("status", "")).lower()
            if status != last_status:
                print(
                    json.dumps(
                        {
                            "event": "status",
                            "video_id": video_id,
                            "status": status or "missing",
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                last_status = status

            if status in {"completed", "failed"}:
                total_seconds = time.monotonic() - started
                result = {
                    "outcome": status,
                    "model": MODEL,
                    "video_id": video_id,
                    "request_seconds": round(request_seconds, 3),
                    "generation_seconds": round(total_seconds - request_seconds, 3),
                    "total_seconds": round(total_seconds, 3),
                    "finished_at": utc_now(),
                }
                if status == "completed":
                    result["url"] = poll_data.get("url")
                else:
                    result["error"] = extract_error(poll_data)
                print(
                    json.dumps({"event": "final", "result": result}, ensure_ascii=False),
                    flush=True,
                )
                return 0 if status == "completed" else 1

            time.sleep(args.poll_interval)

        raise TimeoutError(
            f"Polling exceeded {args.timeout} seconds; last status: {last_status}"
        )
    except Exception as exc:
        total_seconds = time.monotonic() - started
        if request_seconds is None:
            request_seconds = total_seconds
        result = {
            "outcome": "error",
            "model": MODEL,
            "video_id": video_id,
            "request_seconds": round(request_seconds, 3),
            "generation_seconds": round(total_seconds - request_seconds, 3),
            "total_seconds": round(total_seconds, 3),
            "finished_at": utc_now(),
        }
        if isinstance(exc, HttpFailure):
            result["status_code"] = exc.status_code
            result["error"] = exc.body
        else:
            result["error"] = f"{type(exc).__name__}: {exc}"
        print(
            json.dumps({"event": "final", "result": result}, ensure_ascii=False),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
