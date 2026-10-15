"""Internal routes of the app process (``HEALTHCARE_DEMO_ROLE=app``) for the hospitals' LMAs.

The LMAs call these; the frontend never does. They are left out of the OpenAPI
document, which describes the public API.

- ``GET /api/v1/registry/model-in-use`` and
  ``GET /api/v1/registry/models/{version}``: one model with its parameters,
  which a hospital checks against the digests a round names. 404 for an
  unknown version.
- ``POST /api/v1/federation/rounds/{round_id}/updates``: one hospital's
  bounded update (``UpdateSubmission``), stored in the app's runtime until the
  round is aggregated. 409 when the consortium refuses it (see
  ``Consortium.accept_update``).
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from agents.common.infrastructure.federation_http import UPDATE_PATH, UpdateReceipt, UpdateSubmission

from app.services.consortium import Consortium, UpdateRefused
from app.services.remote import MODEL_IN_USE_PATH, MODEL_PATH
from app.services.site_store import UnknownModelError


def build_coordinator_router(consortium: Callable[[], Consortium]) -> APIRouter:
    """The app's internal routes. ``consortium`` returns the process's consortium."""
    router = APIRouter(include_in_schema=False)

    @router.get(MODEL_IN_USE_PATH)
    def registry_model_in_use() -> dict[str, Any]:
        return consortium().registry.model_in_use().row()

    @router.get(MODEL_PATH)
    def registry_model(version: str) -> dict[str, Any]:
        model = consortium().registry.get(version)
        if model is None:
            raise UnknownModelError(version)
        return model.row()

    @router.post(UPDATE_PATH)
    def submit_update(round_id: str, submission: UpdateSubmission) -> dict[str, Any]:
        try:
            digest = consortium().accept_update(round_id, submission.job, submission.update)
        except UpdateRefused as error:
            raise HTTPException(status_code=409, detail=f"The update was not stored: {error}.") from None
        return UpdateReceipt(update_digest=digest).model_dump(mode="json")

    return router
