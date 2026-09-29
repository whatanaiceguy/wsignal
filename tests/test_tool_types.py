import pytest

from wsignal.inference.orchestration import ORCHESTRATOR_TOOLS
from wsignal.inference.tools import tools_for


@pytest.mark.parametrize("role", ["assistant", "researcher", "refuter", "orchestrator"])
def test_every_tool_a_role_sends_is_a_function(role):
    tools = tools_for(role) + (ORCHESTRATOR_TOOLS if role == "orchestrator" else [])
    wrong = [tool["function"]["name"] for tool in tools if tool.get("type") != "function"]
    assert wrong == []
