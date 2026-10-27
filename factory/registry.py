"""Apartment registry: name -> module path -> class name.

The orchestrator imports lazily through this table so that a broken or
not-yet-written apartment can never take the whole factory down, and so that
startup RAM stays low (apartments are only imported when first routed to).
"""

#: apartment key -> (module suffix under ``factory.apartments``, class name)
APARTMENTS = {
    # --- pipeline -----------------------------------------------------------
    "hunter": ("apt_hunter.agent", "HunterAgent"),
    "enricher": ("apt_enricher.agent", "EnricherAgent"),
    "auditor": ("apt_auditor.agent", "AuditorAgent"),
    "outreach": ("apt_outreach.agent", "OutreachAgent"),
    "inbox": ("apt_inbox.agent", "InboxAgent"),
    # --- conversation -------------------------------------------------------
    "urdu_nlp": ("apt_urdu_nlp.agent", "UrduNlpAgent"),
    "requirements": ("apt_requirements.agent", "RequirementsAgent"),
    "convincer": ("apt_convincer.agent", "ConvincerAgent"),
    "closer": ("apt_closer.agent", "CloserAgent"),
    "followup": ("apt_followup.agent", "FollowupAgent"),
    # --- support ------------------------------------------------------------
    "builder": ("apt_builder.agent", "BuilderAgent"),
    "knowledge": ("apt_knowledge.agent", "KnowledgeAgent"),
    "self_trainer": ("apt_self_trainer.agent", "SelfTrainerAgent"),
    "security": ("apt_security.agent", "SecurityAgent"),
    # --- platforms ----------------------------------------------------------
    "platform_whatsapp": ("apt_platform_whatsapp.agent", "PlatformWhatsAppAgent"),
    "platform_linkedin": ("apt_platform_linkedin.agent", "PlatformLinkedInAgent"),
    "platform_facebook": ("apt_platform_facebook.agent", "PlatformFacebookAgent"),
    "platform_instagram": ("apt_platform_instagram.agent", "PlatformInstagramAgent"),
    "platform_github": ("apt_platform_github.agent", "PlatformGitHubAgent"),
    # --- supervisor ---------------------------------------------------------
    "manager": ("apt_manager.agent", "ManagerAgent"),
}

#: Apartments the router is allowed to hand a raw user message to.
#: Internal workers (auditor, enricher, builder, security) are driven by the
#: daily cycle or explicit commands rather than free-form chat.
CHATTABLE = (
    "hunter",
    "outreach",
    "inbox",
    "urdu_nlp",
    "requirements",
    "convincer",
    "closer",
    "followup",
    "knowledge",
    "manager",
    "self_trainer",
    "security",
    "builder",
    "platform_whatsapp",
    "platform_linkedin",
    "platform_facebook",
    "platform_instagram",
    "platform_github",
)

#: Order in which apartments must be trained (dependencies first).
TRAIN_ORDER = (
    "knowledge",
    "urdu_nlp",
    "hunter",
    "enricher",
    "auditor",
    "requirements",
    "convincer",
    "closer",
    "outreach",
    "inbox",
    "followup",
    "builder",
    "platform_whatsapp",
    "platform_linkedin",
    "platform_facebook",
    "platform_instagram",
    "platform_github",
    "self_trainer",
    "security",
    "manager",
)

#: The 12-step autonomous daily cycle, in order.  Each entry is
#: ``(apartment, method, kwargs)`` - the orchestrator logs every step.
DAILY_CYCLE = (
    ("security", "run", {}),
    ("inbox", "process_replies", {"limit": 30}),
    ("followup", "send_due", {"limit": 20}),
    ("hunter", "run", {"limit": 20}),
    ("enricher", "enrich_batch", {"limit": 25}),
    ("auditor", "audit_batch", {"limit": 15}),
    ("outreach", "run_batch", {"limit": 20}),
    ("platform_whatsapp", "run", {"limit": 10}),
    ("platform_linkedin", "run", {"limit": 8}),
    ("platform_facebook", "run", {"limit": 5}),
    ("platform_instagram", "run", {"limit": 8}),
    ("self_trainer", "learn", {}),
)


def apartment_names():
    """Return all apartment keys in declaration order."""
    return tuple(APARTMENTS.keys())


def worker_names():
    """Everything except the manager (the manager supervises, it is not a worker)."""
    return tuple(name for name in APARTMENTS if name != "manager")


def module_and_class(name):
    """Return ``(module_suffix, class_name)`` for an apartment key."""
    if name not in APARTMENTS:
        raise KeyError("unknown apartment: %r" % (name,))
    return APARTMENTS[name]
