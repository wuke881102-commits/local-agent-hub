from .base import Agent, AgentContext, AgentResult, AGENT_REGISTRY, register_agent, get_agent
from . import html_page  # noqa: F401 — side-effect: register
from . import document_map  # noqa: F401
from . import index_enrich  # noqa: F401
from . import knowledge_governance  # noqa: F401
from . import base_analysis  # noqa: F401
from . import pdf_recognition  # noqa: F401
from . import meeting_minutes  # noqa: F401
from . import collab_dispatch  # noqa: F401
from . import local_image  # noqa: F401

__all__ = ["Agent", "AgentContext", "AgentResult", "AGENT_REGISTRY", "register_agent", "get_agent"]
