"""Personal Assistant — a private, per-user command centre over EXISTING Talentgram operations.

  parser     deterministic multi-command grammar (no LLM, no network)
  resolve    talent / project / template resolution (reuses the casting-pipeline NLU matchers)
  planner    parsed segments → persisted task objects (clarification slots, dependencies, confirmation)
  executors  explicit allowlist of existing operations (add / move / send / share / lookups) + delivery read-back
  store      user-scoped persistence (tasks, commands, conversation, audit)
  access     who may use it (enforced on the backend)
  router     /api/personal-assistant/*

It never calls an external AI-model API and never talks to WhatsApp directly: sends only create jobs in the
existing WhatsApp batch/job queue.
"""
