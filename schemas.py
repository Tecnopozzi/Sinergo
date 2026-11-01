"""
Schemi Pydantic unificati per 10b.

Sezione A — Gateway OpenAI-compat (ereditata dal progetto 10):
  ChatMessage, ChatCompletionRequest, ModelEntry, ModelsResponse.

Sezione B — OCR Server (nuovi):
  OcrJobResponse, OcrStatusResponse, OcrBlock, OcrResultResponse.
"""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# A — OpenAI-compat (dal progetto 10, invariati)
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    content: Optional[Union[str, list[dict[str, Any]]]] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = Field(default=None, alias="max_tokens")
    stop: Optional[Union[str, list[str]]] = None
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Optional[Union[str, dict[str, Any]]] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    seed: Optional[int] = None
    response_format: Optional[dict[str, Any]] = None
    chat_template_kwargs: Optional[dict[str, Any]] = None
    # Budget di ragionamento per-richiesta (llama-server / llama-swap).
    # `thinking_budget_tokens` e' il nome che capisce llama.cpp; `budget_pensiero`
    # e' l'alias che Kairos mette in ctx.extra. Entrambi vengono inoltrati.
    thinking_budget_tokens: Optional[int] = None
    budget_pensiero: Optional[int] = None
    reasoning_budget: Optional[int] = None


class ModelEntry(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "ollama"
    context_length: Optional[int] = None
    architecture: Optional[dict[str, Any]] = None
    capabilities: Optional[list[str]] = None
    size: Optional[int] = None
    modified_at: Optional[str] = None


class ModelsResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelEntry]


class KvPrefissoRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    fino_a: str = Field(min_length=1)


class KvSlotRequest(BaseModel):
    model: str
    azione: Literal["save", "restore"]
    nome: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")

    @field_validator("nome")
    @classmethod
    def nome_file_ordinario(cls, nome: str) -> str:
        if nome in {".", ".."} or PureWindowsPath(nome).is_reserved():
            raise ValueError("Nome slot riservato dal filesystem")
        return nome


# ---------------------------------------------------------------------------
# B — OCR Server (nuovi)
# ---------------------------------------------------------------------------


class OcrJobResponse(BaseModel):
    """Risposta immediata di POST /process."""

    job_id: str


class OcrProgress(BaseModel):
    page: int
    total: int


class OcrStatusResponse(BaseModel):
    """Risposta di GET /status/{job_id}.

    ``status`` e' sempre uno tra queued|running|done|error nel JSON pubblico:
    gli annullamenti interni ``cancelled`` compaiono come ``status=error`` e
    ``error=cancelled_by_user`` per compatibilita' con vision.py (09).

    Campo ``internal_status`` opzionale: stato persistito nel server (es. cancelled).
    """

    job_id: str
    status: Literal["queued", "running", "done", "error"]
    progress: Optional[OcrProgress] = None
    error: Optional[str] = None
    cancel_requested: Optional[bool] = None
    internal_status: Optional[str] = None


class OcrCancelResponse(BaseModel):
    ok: bool
    job_id: str
    message: Optional[str] = None
    previous: Optional[str] = None
    state: Optional[str] = None
    cancel_requested: Optional[bool] = None


class OcrJobsListResponse(BaseModel):
    jobs: list[dict[str, Any]]


class OcrCancelAllResponse(BaseModel):
    ok: bool
    queued_cancelled: int = 0
    running_cancel_requested: int = 0


class OcrBlock(BaseModel):
    """Blocco di testo/formula restituito da /result/{job_id}.

    Compatibile con il parser di vision.py:
      b.get("content") or b.get("latex") or b.get("text")
    """

    type: str = "text"
    page: int = 1
    content: str = ""
    latex: Optional[str] = None
    text: Optional[str] = None


class OcrSource(BaseModel):
    filename: str
    mime: str
    num_pages: int


class OcrResultResponse(BaseModel):
    """Risposta di GET /result/{job_id} quando status == done."""

    job_id: str
    status: Literal["done"] = "done"
    model: str
    source: OcrSource
    blocks: list[OcrBlock]
    pages: list[dict[str, Any]] = Field(default_factory=list)


class PullRestartAcceptedResponse(BaseModel):
    """Risposta di POST /admin/pull-restart (202 Accepted)."""

    status: Literal["accepted"] = "accepted"
    message: str = Field(
        default="Operazione accodata: git pull + riavvio in background.",
    )


# ---------------------------------------------------------------------------
# C — Chat async jobs (nuovi)
# ---------------------------------------------------------------------------


class ChatJobAcceptedResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"] = "queued"


class ChatJobStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    model: str
    created_at: int
    updated_at: int
    started_at: Optional[int] = None
    finished_at: Optional[int] = None
    error: Optional[str] = None
    partial_text_len: int = 0
    request_id: Optional[str] = None


class ChatJobResultResponse(BaseModel):
    job_id: str
    status: Literal["completed"]
    result: dict[str, Any]


# ---------------------------------------------------------------------------
# D — Media Enhancement SUPIR (opzionale)
# ---------------------------------------------------------------------------


class MediaJobResponse(BaseModel):
    job_id: str


class MediaStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "done", "error"]
    mode: Optional[str] = None
    upscale: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    error: Optional[str] = None
    cancel_requested: Optional[bool] = None
    internal_status: Optional[str] = None


class MediaResultJsonResponse(BaseModel):
    job_id: str
    status: Literal["done"] = "done"
    mime: str = "image/png"
    image_base64: str
    width: int
    height: int
    mode: str
    upscale: int
    fidelity: float
    face_only: bool


class MediaCancelResponse(BaseModel):
    ok: bool
    job_id: str
    message: Optional[str] = None
    previous: Optional[str] = None
    state: Optional[str] = None
    cancel_requested: Optional[bool] = None


class MediaJobsListResponse(BaseModel):
    jobs: list[dict[str, Any]]


class MediaCancelAllResponse(BaseModel):
    ok: bool
    queued_cancelled: int = 0
    running_cancel_requested: int = 0
