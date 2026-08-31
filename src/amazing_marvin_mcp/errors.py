"""The failures whose message is meant for the caller.

mcp's rule (its *Handling errors* page): a tool keeps the message of a
``ToolError``, a resource read keeps the message of a ``ResourceError``, and
anything else is a crash -- the caller is told just ``Error executing tool
<name>`` and the traceback goes to the log at ERROR. Their test is "could a
smarter model have avoided this?", and everything we raise on purpose passes
it: an unknown name, a missing token, an unreachable API.

Which of the two applies is decided by the raise site, not by the failure, and
the mirror, the API clients and ``fresh()`` are reached from tools *and* from
``marvin://structure``. So these are both, and a new resource over the same
code keeps its message without a translation at the boundary. A resource-only
lookup raises mcp's own ``ResourceNotFoundError`` (see :meth:`Workflow.section`).
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ResourceError, ToolError


class MarvinError(ToolError, ResourceError):
    """A failure the caller can act on: unknown name, missing token, API refusal."""
