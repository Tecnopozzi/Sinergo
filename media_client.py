"""
Client HTTP per il servizio Media Enhancement su LocalRouter (10b).

Uso locale o via tunnel Cloudflare — stessa API_KEY del gateway.

Esempio:

    from media_client import MediaClient

    client = MediaClient(
        base_url="https://localrouter.example.com",
        api_key="your-key",
    )
    png_bytes = client.enhance_path("foto.jpg", mode="restore+upscale", upscale=2)
    Path("out.png").write_bytes(png_bytes)
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any, Literal

import httpx

MediaMode = Literal["restore", "upscale", "restore+upscale"]


class MediaClientError(Exception):
    """Errore client media (HTTP o job fallito)."""


class MediaClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8088",
        api_key: str = "",
        *,
        timeout_s: float = 600.0,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def submit(
        self,
        image_path: str | Path,
        *,
        mode: MediaMode = "restore+upscale",
        upscale: int = 2,
        fidelity: float = 0.5,
        face_only: bool = False,
    ) -> str:
        path = Path(image_path)
        if not path.is_file():
            raise MediaClientError(f"File non trovato: {path}")

        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(path.suffix.lower(), "application/octet-stream")

        with httpx.Client(timeout=self.timeout_s) as client:
            with path.open("rb") as fh:
                resp = client.post(
                    f"{self.base_url}/media/process",
                    headers=self._headers(),
                    files={"file": (path.name, fh, mime)},
                    data={
                        "mode": mode,
                        "upscale": str(upscale),
                        "fidelity": str(fidelity),
                        "face_only": "1" if face_only else "0",
                    },
                )
        if resp.status_code == 404:
            raise MediaClientError("Modulo media non abilitato sul server (MEDIA_ENABLED=0)")
        if resp.status_code >= 400:
            raise MediaClientError(f"POST /media/process failed: {resp.status_code} {resp.text}")
        data = resp.json()
        job_id = str(data.get("job_id") or "")
        if not job_id:
            raise MediaClientError("Risposta senza job_id")
        return job_id

    def wait(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_s
        with httpx.Client(timeout=30.0) as client:
            while time.monotonic() < deadline:
                resp = client.get(
                    f"{self.base_url}/media/status/{job_id}",
                    headers=self._headers(),
                )
                if resp.status_code == 404:
                    raise MediaClientError(f"Job non trovato: {job_id}")
                resp.raise_for_status()
                data = resp.json()
                status = data.get("status")
                if status == "done":
                    return data
                if status == "error":
                    raise MediaClientError(data.get("error") or "job error")
                time.sleep(self.poll_interval_s)
        raise MediaClientError(f"Timeout attesa job {job_id}")

    def fetch_png(self, job_id: str) -> bytes:
        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.get(
                f"{self.base_url}/media/result/{job_id}",
                headers=self._headers("image/png"),
            )
        if resp.status_code == 202:
            raise MediaClientError("Risultato non ancora pronto")
        if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
            return resp.content
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "error":
                raise MediaClientError(data.get("error") or "job error")
            b64 = data.get("image_base64")
            if b64:
                return base64.b64decode(b64)
        raise MediaClientError(f"GET /media/result failed: {resp.status_code} {resp.text[:200]}")

    def enhance_bytes(
        self,
        image_bytes: bytes,
        filename: str = "upload.png",
        *,
        mode: MediaMode = "restore+upscale",
        upscale: int = 2,
        fidelity: float = 0.5,
        face_only: bool = False,
    ) -> bytes:
        mime = "image/png"
        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.post(
                f"{self.base_url}/media/process",
                headers=self._headers(),
                files={"file": (filename, image_bytes, mime)},
                data={
                    "mode": mode,
                    "upscale": str(upscale),
                    "fidelity": str(fidelity),
                    "face_only": "1" if face_only else "0",
                },
            )
            if resp.status_code >= 400:
                raise MediaClientError(f"POST /media/process failed: {resp.status_code} {resp.text}")
            job_id = str(resp.json().get("job_id") or "")
            self.wait(job_id)
            return self.fetch_png(job_id)

    def enhance_path(
        self,
        image_path: str | Path,
        output_path: str | Path | None = None,
        **kwargs: Any,
    ) -> bytes:
        job_id = self.submit(image_path, **kwargs)
        self.wait(job_id)
        png = self.fetch_png(job_id)
        if output_path is not None:
            Path(output_path).write_bytes(png)
        return png


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Client Media Enhancement LocalRouter")
    parser.add_argument("input", help="Immagine sorgente")
    parser.add_argument("-o", "--output", required=True, help="PNG risultato")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--mode", default="restore+upscale")
    parser.add_argument("--upscale", type=int, default=2)
    parser.add_argument("--fidelity", type=float, default=0.5)
    parser.add_argument("--face-only", action="store_true")
    args = parser.parse_args()

    client = MediaClient(base_url=args.base_url, api_key=args.api_key)
    client.enhance_path(
        args.input,
        args.output,
        mode=args.mode,
        upscale=args.upscale,
        fidelity=args.fidelity,
        face_only=args.face_only,
    )
    print(f"Salvato: {args.output}")


if __name__ == "__main__":
    _cli()
