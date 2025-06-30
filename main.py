import os
import yaml
from crewai import Agent, Task, Crew, Flow, LLM
from crewai.flow.flow import start, listen, start, and_, or_
from pathlib import Path
from typing import Dict
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from crewai_tools.adapters.mcp_adapter import MCPServerAdapter
from mcp import StdioServerParameters

# Cargar variables de entorno
load_dotenv()

# Configurar LLM
llm = LLM(
    model="gemini/gemini-2.0-flash",
    verbose=True,
    temperature=0.65,
    google_api_key=os.getenv("GEMINI_API_KEY")
)

# Cargar contexto de la base de datos
def load_context(filename: str) -> str:
    path = "./crews/content_crew/config/contexts/" + filename
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

DATABASE_SCHEMA_CONTEXT = load_context('db_context.md')

# Cargar configuración YAML de agentes y tareas
def load_yaml_config():
    files = {
        'agents_team_info': './crews/content_crew/config/agents_team_info.yaml',
        'info_team_tasks': './crews/content_crew/config/agents_team_task.yaml',
    }

    configs = {}
    for key, filepath in files.items():
        with open(filepath, 'r') as f:
            configs[key] = yaml.safe_load(f)

    return configs

configs = load_yaml_config()
agente_info_team = configs['agents_team_info']
tareas_info_team = configs['info_team_tasks']

# Definir clases Pydantic para salida de tareas
class InterpretacionExpertoComentarista(BaseModel):
    team_name: str = Field(..., description="Nombre del equipo referido por el usuario")

class ExtraccionIDTeam(BaseModel):
    team_id: str = Field(..., description="Identificador único del equipo escogido por el usuario")

# Crear servidor MCP
server_env = {**os.environ}
server_env["PYTHONUTF8"] = "1"

server_params = StdioServerParameters(
    command="python",
    args=["./servers/psql_server.py"],
    env=server_env
)

# Clase Flow principal
class ExtraerIDEquipoFlow(Flow):
    @start()
    def iniciar_flujo(self):
        team_name = self.state["team_name"]

        # Iniciar adaptador MCP
        with MCPServerAdapter(serverparams=server_params) as mcp_tools:
            print(f"Herramientas MCP disponibles: {[tool.name for tool in mcp_tools]}")

            # Crear agentes
            comentarista = Agent(config=agente_info_team["football_comentator"],
                                 llm = llm)
            extractor = Agent(
                config=agente_info_team["database_analyst"],
                tools=mcp_tools,
                llm=llm
            )

            # Crear tareas
            interpretar_tarea = Task(
                config=tareas_info_team["interpret_user_input"],
                agent=comentarista,
                output_pydantic=InterpretacionExpertoComentarista
            )

            extraer_tarea = Task(
                config=tareas_info_team["find_team_id_task"],
                agent=extractor,
                context=[interpretar_tarea],
                output_pydantic=ExtraccionIDTeam
            )

            crew = Crew(
                agents=[comentarista, extractor],
                tasks=[interpretar_tarea, extraer_tarea],
                verbose=True
            )

            resultado = crew.kickoff(inputs={"team_name": team_name, "DATABASE_SCHEMA_CONTEXT": DATABASE_SCHEMA_CONTEXT})
            self.state["team_id"] = resultado
            from datetime import datetime, timezone
            now_utc = datetime.now(timezone.utc)
            return resultado, now_utc
    
    @listen(iniciar_flujo)
    def ejecutar_query(self, resultado, now_utc):
        pass

        



# Ejecutar desde línea de comandos
if __name__ == "__main__":
    print("=== Búsqueda de ID de Equipo ===")
    nombre_equipo = input("Introduce el nombre del equipo: ").strip()


    if not nombre_equipo:
        print("Nombre no válido. Finalizando.")
    else:
        flujo = ExtraerIDEquipoFlow()
        resultado = flujo.kickoff(inputs={"team_name": nombre_equipo})
        print("\n--- Resultado ---")
        print(f"ID del equipo para '{nombre_equipo}': {resultado}")
