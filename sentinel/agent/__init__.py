"""The ADK agent — Phase 2 (investigate) and Phase 3 (decide).

- :mod:`schemas`      the structured decision the model must return
- :mod:`tools`        read tools over the farm data (names in, names out)
- :mod:`prompts`      the agent's instruction
- :mod:`investigator` wires agent + runner; ``investigate(candidate)`` returns
                      a :class:`InvestigationResult` with the decision, the
                      real tool trace, latency and token usage
"""
