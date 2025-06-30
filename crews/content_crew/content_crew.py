from crewai import Agent, Crew, Process, Task, LLM
from crewai.project import CrewBase, agent, crew, task
from crewai.agents.agent_builder.base_agent import BaseAgent
from typing import List
import os
from dotenv import load_dotenv
from crewai import Crew, Process, CrewBase, LLM
from langchain_openai import ChatOpenAI
from mcp import StdioServerParameters
from crewai_tools.adapters.mcp_adapter import MCPServerAdapter
load_dotenv()

class TeamIDCrew(CrewBase):
    def __init__(self):
        server_params = StdioServerParameters(
            command="python",
            args=["./servers/psql_server.py"],
            env={**os.environ, "PYTHONUTF8": "1"}
        )

        with MCPServerAdapter(serverparams=server_params) as mcp_tools:
            if not mcp_tools:
                raise RuntimeError("Error: No se pudieron cargar las herramientas del servidor MCP.")
            
            super().__init__(
                agents_config_path="config/agents.yaml",
                tasks_config_path="config/tasks.yaml",
                process=Process.sequential,
                verbose=True,
                tools=mcp_tools,
                llm=LLM(
                    model="gemini/gemini-2.0-flash",
                    verbose=True,
                    temperature=0.5,
                    google_api_key=os.getenv("GEMINI_API_KEY")
                )
            )