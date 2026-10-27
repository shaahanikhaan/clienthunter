"""ClientHunter Factory v1.3.0.

A self-training, offline-first, multi-agent client acquisition factory for a
Pakistani web agency.

Design rules (see README.md):
  * Python 3.10+ standard library only - zero third-party dependencies.
  * No external AI/LLM APIs. Every "brain" is trained locally from JSON data.
  * Every agent lives in its own isolated "apartment" (folder + own SQLite).
  * Models are stored as JSON (never pickle).
  * Total RAM budget for the whole factory: 4 GB (default soft cap 512 MB).

Package layout:
    factory/
      orchestrator.py      master router + permission enforcer
      train_all.py         trains every apartment from local JSON
      store.py             shared lead/memory database (SQLite)
      config/              agency + permission configuration
      apartments/<apt>/    one folder per agent
      data/                user-editable training data
"""

__version__ = "1.3.0"
__codename__ = "Apartment Model"

from pathlib import Path

#: Absolute path of the ``factory/`` package directory.
FACTORY_ROOT = Path(__file__).resolve().parent
#: Absolute path of the git repository root (parent of ``factory/``).
REPO_ROOT = FACTORY_ROOT.parent

__all__ = ["__version__", "__codename__", "FACTORY_ROOT", "REPO_ROOT"]
