"""
Main agent executor with LangGraph implementation.
This is the core file you'll customise for your specific agent logic.
"""

import asyncio
from contextlib import nullcontext
from typing import Annotated, Any, Dict, List, Optional
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from a2a.server.agent_execution import AgentExecutor as A2AAgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils import new_agent_text_message


from .nodes import AgentNode, ToolNode, should_continue
from src.tools import get_available_tools
from src.config.agent_config import load_agent_config, get_secrets
from src.config.settings import settings
from cams_otel_lib import (
    Logger as logger,
    otel_trace,
    MessageCategory,
    AgentMemorySubtype,
    LogLevel,
)
from langgraph.checkpoint.memory import MemorySaver


def _extract_text(content) -> str:
    """Normalize an AIMessage's .content into plain text.

    Most models return a plain string. Some (e.g. Gemini 2.5 "thinking"
    models via Vertex AI) return a list of content blocks instead — text
    blocks plus non-text ones like thought_signature — so pull out just the
    text parts rather than let a raw block list leak through as the answer.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)


class AgentState(TypedDict):
    """
    Agent state definition — customise this for your agent's needs.
    """
    messages: Annotated[List[BaseMessage], add_messages]
    final_response: str
    tenant_id: str
    thread_id: str
    # Langfuse trace context (zebra pattern); present but None when langfuse disabled
    _langfuse_trace_id: Optional[str]
    _langfuse_span_id: Optional[str]
    # LiteLLM custom headers forwarded from the frontend
    litellm_headers: Optional[dict]


class AgentExecutor(A2AAgentExecutor):
    """
    A basic chat bot agent: an LLM that can call tools when it needs to.

    Key customisation points:
    1. Modify build_graph() to change the execution flow.
    2. Add new tools under src/tools/ — they're auto-discovered.
    3. Update AgentState to track additional information.
    """

    @otel_trace
    def __init__(self):
        self.config = load_agent_config()
        self.tools = get_available_tools()

        secrets = get_secrets("default", self.config)

        self.observability = None   # default; overridden below when langfuse is selected
        self.error_tracker = None   # default; overridden below when sentry is selected



        logger.info(f"Agent executor initialised with {len(self.tools)} local tools")

        # MCP tools from agent_config.tools are loaded lazily on first request.
        self._mcp_initialized = False
        self._mcp_init_lock = asyncio.Lock()

        # Checkpointing — MemorySaver gives multi-turn memory within a session.
        self._checkpointer = MemorySaver()

        # Compiled graph cache — keyed by tenant_id to avoid rebuilding LLM clients per request
        self._compiled_graphs: Dict[str, Any] = {}
        self._graph_cache_lock = asyncio.Lock()


    @otel_trace
    def build_graph(self, tenant_id: str = "default") -> StateGraph:
        """
        Build a ReAct-style LangGraph where the LLM iteratively calls tools.

        Flow: agent → (tool_calls?) → tools → agent → ... → END

        Extend this by:
        - Adding new tools in src/tools/ (auto-discovered)
        - Adding more nodes for additional processing steps
        - Adding conditional edges for different execution paths
        """
        workflow = StateGraph(AgentState)

        agent_node = AgentNode(self.config, self.tools, tenant_id)
        tool_node = ToolNode(self.tools)

        workflow.add_node("agent", agent_node.execute)
        workflow.add_node("tools", tool_node.execute)

        workflow.set_entry_point("agent")
        workflow.add_conditional_edges(
            "agent",
            should_continue,
            {"tools": "tools", "end": END},
        )
        workflow.add_edge("tools", "agent")

        return workflow

    @otel_trace
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """
        Execute the agent — handles A2A protocol integration with observability.
        """
        tenant_id = self._extract_tenant_id(context)
        thread_id = self._extract_thread_id(context)

        user_input: Optional[str] = None
        try:
            user_input = context.get_user_input()
            if not user_input:
                logger.warning("Empty user input received")
                await event_queue.enqueue_event(
                    new_agent_text_message("Please provide a question.")
                )
                return

            logger.info(f"Processing agent request: input_length={len(user_input)}")

            obs_ctx = nullcontext(None)  # default; overridden below when langfuse is selected

            with obs_ctx as agent_span:
                trace_id = getattr(agent_span, "trace_id", None) if agent_span else None
                span_id = getattr(agent_span, "id", None) if agent_span else None

                logger.info(f"Trace context established: trace_id={trace_id}, observability_enabled={agent_span is not None}")

                litellm_headers = self._extract_litellm_headers(context)

                initial_state: AgentState = {
                    "messages": [HumanMessage(content=user_input)],
                    "final_response": "",
                    "tenant_id": tenant_id,
                    "thread_id": thread_id,
                    "_langfuse_trace_id": trace_id,
                    "_langfuse_span_id": span_id,
                    "litellm_headers": litellm_headers,
                }

                final_state = await self._run_graph(initial_state, tenant_id, thread_id)

                if agent_span:
                    try:
                        agent_span.update(
                            output={"final_response": final_state.get("final_response", "")}
                        )
                    except Exception:
                        pass

                response = final_state.get("final_response", "I couldn't generate a response.")

                logger.info(f"Agent execution completed: response_length={len(response)}")

                if settings.streaming_enabled:
                    await self._stream_response(response, event_queue)
                else:
                    await event_queue.enqueue_event(new_agent_text_message(response))

        except Exception as e:
            logger.error(f"Error processing agent request: {type(e).__name__}: {str(e)}")


            await event_queue.enqueue_event(
                new_agent_text_message(
                    "Sorry, I encountered an error processing your request."
                )
            )

    @otel_trace
    async def _run_graph(
        self, initial_state: AgentState, tenant_id: str, thread_id: str
    ) -> AgentState:
        """Compile and run the LangGraph workflow with checkpointing for multi-turn memory."""
        # Lazy-load config-based MCP tools once before the first graph compile.
        if not self._mcp_initialized:
            async with self._mcp_init_lock:
                if not self._mcp_initialized:
                    try:
                        from src.tools.mcp_loader import load_mcp_tools
                        mcp_tools = await load_mcp_tools()
                        if mcp_tools:
                            self.tools = self.tools + mcp_tools
                            logger.info(
                                f"Added {len(mcp_tools)} MCP tools from config. "
                                f"Total tools: {len(self.tools)}"
                            )
                    except Exception as _mcp_err:
                        logger.warning(f"MCP tool init skipped: {_mcp_err}")
                    self._mcp_initialized = True

        tenant_config = self.config.get("default", {})
        max_steps = tenant_config.get("max_steps", 10)
        run_config = {"configurable": {"thread_id": thread_id}, "recursion_limit": max_steps}

        # Compile once per tenant; reuse cached graph on subsequent requests
        if tenant_id not in self._compiled_graphs:
            async with self._graph_cache_lock:
                if tenant_id not in self._compiled_graphs:
                    graph = self.build_graph(tenant_id)
                    self._compiled_graphs[tenant_id] = graph.compile(checkpointer=self._checkpointer)

        compiled_graph = self._compiled_graphs[tenant_id]

        logger.log_event(
            message_category=MessageCategory.AGENT_MEMORY,
            message=f"Loading checkpointed state for thread {thread_id}",
            message_subtype=AgentMemorySubtype.MEMORY_READ,
            log_level=LogLevel.INFO,
            attributes={"thread_id": thread_id, "tenant_id": tenant_id},
        )

        final_state = await compiled_graph.ainvoke(initial_state, run_config)

        logger.log_event(
            message_category=MessageCategory.AGENT_MEMORY,
            message=f"Persisted checkpointed state for thread {thread_id}",
            message_subtype=AgentMemorySubtype.MEMORY_WRITE,
            log_level=LogLevel.INFO,
            attributes={"thread_id": thread_id, "tenant_id": tenant_id, "message_count": len(final_state.get("messages", []))},
        )

        # Extract the final answer from the last non-tool-calling AI message
        if not final_state.get("final_response") and final_state.get("messages"):
            from langchain_core.messages import AIMessage as _AIMessage
            for msg in reversed(final_state["messages"]):
                if isinstance(msg, _AIMessage) and not getattr(msg, "tool_calls", None):
                    final_state = dict(final_state)
                    final_state["final_response"] = _extract_text(msg.content)
                    break

        return final_state

    @otel_trace
    async def _stream_response(self, response: str, event_queue: EventQueue) -> None:
        """Send response through the event queue (A2A handles protocol-level streaming)."""
        if response:
            await event_queue.enqueue_event(new_agent_text_message(response))

    @otel_trace
    def _extract_tenant_id(self, context: RequestContext) -> str:
        try:
            metadata = getattr(context, "metadata", {}) or {}
            return metadata.get("tenant_uuid") or metadata.get("tenant_id") or "unknown"
        except Exception as e:
            logger.warning(f"Error extracting tenant_uuid: {str(e)}")
            return "unknown"

    @otel_trace
    def _extract_thread_id(self, context: RequestContext) -> str:
        try:
            import uuid
            metadata = getattr(context, "metadata", {})
            thread_id = metadata.get("thread_id")
            if not thread_id:
                thread_id = f"thread_{uuid.uuid4().hex[:16]}"
                logger.debug(f"Generated new thread_id: {thread_id}")
            return thread_id
        except Exception as e:
            import uuid
            logger.warning(f"Error extracting thread_id, generating new one: {str(e)}")
            return f"thread_{uuid.uuid4().hex[:16]}"

    @otel_trace
    def _extract_litellm_headers(self, context: RequestContext) -> Optional[dict]:
        """
        Extract LiteLLM custom headers from request metadata.

        Expected metadata keys: tenantid, userid, appid, agentname, workspaceid.
        sessionid is always auto-generated per invocation.
        """
        try:
            import uuid
            metadata = getattr(context, "metadata", {})
            headers = {}
            for key in ("tenantid", "userid", "appid", "agentname", "workspaceid"):
                if metadata.get(key):
                    headers[key] = metadata[key]
            headers["sessionid"] = f"S-{uuid.uuid4()}"
            if headers:
                logger.debug(f"Extracted LiteLLM headers: {list(headers.keys())}")
            return headers or None
        except Exception as e:
            logger.warning(f"Error extracting LiteLLM headers: {str(e)}")
            return None

    @otel_trace
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Handle request cancellation."""
        await event_queue.enqueue_event(new_agent_text_message("Request cancelled."))
