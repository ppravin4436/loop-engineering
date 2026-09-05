"""
edgedash.query package — Natural language query interface (rules 40–46).
"""

from edgedash.query.ask import Answer, ask
from edgedash.query.tools import TOOLS, tool

__all__ = ["Answer", "TOOLS", "ask", "tool"]
