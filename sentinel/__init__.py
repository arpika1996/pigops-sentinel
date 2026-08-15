"""PigOps Sentinel — an autonomous farm-monitoring agent.

Package layout:

- ``config``            every tunable, env-overridable, validated at import
- ``firestore_client``  one shared Firestore client (real project or emulator)
- ``schema``            the PigOps Firestore document shapes we mirror
- ``repository``        typed read/write access used by the scanner, the
                        agent tools and the console
- ``scanner``           Phase 1 — deterministic candidate detection (no LLM)
- ``seed``              demo dataset generator (``python -m sentinel.seed``)
"""

__version__ = "0.1.0"
