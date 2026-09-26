"""Hugging Face provider adapters.

The families the OCaml router selects but no in-repo PyTorch module serves —
``transformer`` for the embedding task above all — come from the Hub. That makes
provisioning a first-class concern rather than a detail: weights have to be
present and `transformers` has to be installed, and neither is true by default.
This module makes both explicit and refuses rather than guessing.

Two rules shape the whole file:

- **Nothing heavy is imported at module import time.** `transformers` and
  `torch` are imported inside `load`, so listing a capability, or resolving one
  this deployment will never run, costs nothing.
- **Downloads are opt-in and off by default.** A `load` that quietly fetched
  several hundred megabytes on first request would make latency unpredictable
  and make any test touching this path need the network. The caller passes
  `allow_download=True` when that is acceptable; otherwise it gets a typed
  refusal naming the checkpoint to pre-fetch.

`services/nl2sql-demo/backend/app/services/model_adapters.py` is the same shape
applied to one app's fine-tuned artifacts. This is the framework-level,
family-driven version of it.
"""

from __future__ import annotations

import os
from typing import Any

from app.ml.inventory import (
    MissingModelDependencyError,
    ModelCapability,
    ModelDownloadNotPermittedError,
)


#: Environment switch a deployment sets to permit Hub downloads. Kept in
#: configuration rather than as a code default, so one image can be strict in
#: one environment and permissive in another.
ALLOW_DOWNLOAD_ENV = "DAGENTS_ALLOW_MODEL_DOWNLOAD"


def _download_allowed_by_default() -> bool:
    """Whether the environment has opted into Hub downloads."""
    return os.getenv(ALLOW_DOWNLOAD_ENV, "").strip().lower() in {"1", "true", "yes"}


def _import_backends() -> tuple[Any, Any, Any]:
    """Import the Hub stack.

    Isolated into one function so the dependency failure has a single place to
    be raised from, and so a test can exercise the "not installed" path without
    uninstalling anything.

    Returns:
    - `(torch, AutoTokenizer, AutoModel)`.
    """
    import torch
    from transformers import AutoModel, AutoTokenizer

    return torch, AutoTokenizer, AutoModel


class TransformerEmbeddingAdapter:
    """Mean-pooled sentence embeddings from a Hugging Face encoder.

    Params:
    - `capability`: the inventory entry that selected this adapter.
    - `checkpoint`: Hub id or local directory; defaults to the capability's.
    - `device`: torch device string.
    - `allow_download`: whether `load` may fetch missing weights. Defaults to
      the `DAGENTS_ALLOW_MODEL_DOWNLOAD` switch, which is off.
    - `cache_dir`: where to look for, and store, weights.
    - `max_length`: tokenizer truncation length.

    What it does:
    - Holds only configuration until `load` is called.
    - On `load`, imports the Hub stack and resolves the checkpoint under the
      declared download policy.
    - On `embed`, tokenizes, runs the encoder, and mean-pools over the attention
      mask so padding does not dilute the vectors.

    Returns:
    - Consumed through `load` and `embed`.
    """

    def __init__(
        self,
        capability: ModelCapability,
        *,
        checkpoint: str | None = None,
        device: str = "cpu",
        allow_download: bool | None = None,
        cache_dir: str | None = None,
        max_length: int = 256,
    ) -> None:
        self.capability = capability
        self.checkpoint = checkpoint or capability.default_checkpoint
        if not self.checkpoint:
            raise ValueError(
                f"{capability.family}/{capability.task} names no default checkpoint and "
                "none was supplied"
            )
        self.device = device
        self.allow_download = (
            _download_allowed_by_default() if allow_download is None else allow_download
        )
        self.cache_dir = cache_dir
        self.max_length = max_length
        self._tokenizer: Any = None
        self._model: Any = None
        self._torch: Any = None

    @property
    def is_loaded(self) -> bool:
        """Whether the encoder is resident."""
        return self._model is not None

    def load(self) -> None:
        """Make the encoder resident, honouring the download policy.

        Raises:
        - `MissingModelDependencyError` when `transformers` is not installed,
          naming the requirements to install.
        - `ModelDownloadNotPermittedError` when the weights are not already
          local and downloading was not permitted, naming the checkpoint so a
          deployment can pre-fetch it or set the environment switch.
        """
        if self.is_loaded:
            return
        try:
            torch, tokenizer_cls, model_cls = _import_backends()
        except ImportError as exc:
            raise MissingModelDependencyError(
                f"{self.capability.family}/{self.capability.task} needs the Hugging Face "
                f"stack, which is not installed: {exc}. Install "
                f"{list(self.capability.extra_requirements)} — see "
                "services/model-service/requirements-optional-models.txt.",
                family=self.capability.family,
                task=self.capability.task,
            ) from exc

        shared = {"local_files_only": not self.allow_download}
        if self.cache_dir:
            shared["cache_dir"] = self.cache_dir
        try:
            tokenizer = tokenizer_cls.from_pretrained(self.checkpoint, **shared)
            model = model_cls.from_pretrained(self.checkpoint, **shared)
        except OSError as exc:
            # `local_files_only` turns a cache miss into an OSError. Translating it
            # keeps the distinction a caller cares about — "weights are not here"
            # versus "the model is broken" — instead of surfacing a Hub error.
            if not self.allow_download:
                raise ModelDownloadNotPermittedError(
                    f"{self.checkpoint!r} is not available locally and downloads are "
                    f"disabled. Pre-fetch it into the model cache, or set "
                    f"{ALLOW_DOWNLOAD_ENV}=1 / pass allow_download=True to permit the "
                    f"fetch. Underlying error: {exc}",
                    family=self.capability.family,
                    task=self.capability.task,
                ) from exc
            raise

        model.to(self.device)
        model.eval()
        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model

    def embed(self, texts: list[str]) -> Any:
        """Return one mean-pooled vector per input text.

        Params:
        - `texts`: input strings.

        What it does:
        - Tokenizes with padding and truncation, runs the encoder, then averages
          the token vectors weighted by the attention mask, so padded positions
          contribute nothing.

        Returns:
        - A `numpy` array shaped `[len(texts), hidden_size]`.
        """
        if not self.is_loaded:
            raise RuntimeError("load() must be called before embed()")
        if not texts:
            raise ValueError("embed() needs at least one text")
        encoded = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        ).to(self.device)
        with self._torch.no_grad():
            hidden = self._model(**encoded).last_hidden_state
        mask = encoded["attention_mask"].unsqueeze(-1).to(hidden.dtype)
        pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
        return pooled.cpu().numpy()


def build_embedding_adapter(
    capability: ModelCapability, **options: Any
) -> TransformerEmbeddingAdapter:
    """Inventory entrypoint for the Hugging Face embedding family."""
    return TransformerEmbeddingAdapter(capability, **options)
