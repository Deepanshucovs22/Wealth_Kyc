"""
Interactive local test for the eKYC agent — bypasses the HTTP/auth layer and
talks to the agent's LangGraph executor directly, so you can test it without
a login token.

Run:  py test_local_chat.py
Exit: type 'quit' or press Ctrl+C
"""

import asyncio
import os
import sys

# Quiet the console-spamming OTEL logs/traces for this interactive session so
# the chat prompts stay readable. Must be set before src.agent.executor (and
# everything it imports) is loaded.
os.environ["OTEL_CONSOLE_LOGGING"] = "false"
os.environ["OTEL_CONSOLE_TRACING"] = "false"

from langchain_core.messages import HumanMessage

from src.agent.executor import AgentExecutor


async def main():
    print("Loading agent (this takes a few seconds)...")
    executor = AgentExecutor()
    print("Ready. Ask about a KYC session (e.g. 'Check session KYC-20261008-36B9E4')")
    print("or 'What KYC sessions need attention?'. Type 'quit' to exit.\n")

    thread_id = "local-test-chat"
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not question or question.lower() in ("quit", "exit"):
            break

        state = {
            "messages": [HumanMessage(content=question)],
            "final_response": "",
            "tenant_id": "default",
            "thread_id": thread_id,
            "_langfuse_trace_id": None,
            "_langfuse_span_id": None,
            "litellm_headers": None,
        }
        final = await executor._run_graph(state, "default", thread_id)
        print(f"\nAgent: {final.get('final_response')}\n")


if __name__ == "__main__":
    asyncio.run(main())
