"""Apartment package.

Each sub-package is one agent's "apartment": its own folder, its own
``config.json`` (permissions + limits), its own ``train.json`` (editable
training data), its own ``memory.db`` and its own ``models/`` directory.
"""

from .base_apartment import (  # noqa: F401
    ApartmentDB,
    BaseApartment,
    ModelMissing,
    PermissionDenied,
    TrainingError,
)

__all__ = ["BaseApartment", "ApartmentDB", "PermissionDenied", "ModelMissing", "TrainingError"]
