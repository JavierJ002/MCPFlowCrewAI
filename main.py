import os
import csv
import yaml
from datetime import datetime, timezone
from langchain.tools import Tool
from crewai import Agent, Task, Crew, Flow, LLM
from crewai.flow.flow import start, listen, and_, or_
from crewai_tools import tools
from pathlib import Path
from typing import Dict, Optional, List
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from crewai_tools.adapters.mcp_adapter import MCPServerAdapter
from mcp import StdioServerParameters
from tools.custom_tool import MyCustomDuckDuckGoTool
from datetime import datetime


# Cargar variables de entorno
load_dotenv()

# Configurar LLM
llm = LLM(
    model="gemini/gemini-2.0-flash",
    verbose=True,
    temperature=0.65,
    google_api_key=os.getenv("GEMINI_API_KEY")
)


llm2 = LLM(
    model="gemini/gemini-2.0-flash",
    verbose=True,
    temperature=0.01,
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
    """Modelo para la extracción del ID de equipo, que maneja tanto éxito como fracaso."""
    team_id: Optional[str] = Field(None, description="Identificador único del equipo si se encuentra.")
    error: Optional[str] = Field(None, description="Mensaje de error si no se pudo encontrar el ID del equipo.")

class ExtraccionNPartidos(BaseModel):
    path_file: str = Field(..., description="Direccion del archivo .csv creado")

class RivalName(BaseModel):
    rival_name: str = Field(..., description="Nombre del equipo rival encontrado.")

# Crear servidor MCP
server_env = {**os.environ}
server_env["PYTHONUTF8"] = "1"

server_params = StdioServerParameters(
    command="python",
    args=["./servers/psql_server.py"],
    env=server_env
)

def _create_find_id_crew(mcp_tools: List[Tool], interpretar_tarea: Task = None) -> Crew:
    """Crea y devuelve un Crew configurado para encontrar el ID de un equipo."""
    extractor = Agent(config=agente_info_team["database_analyst"], tools=mcp_tools, llm=llm)
    find_id_task = Task(
        config=tareas_info_team["find_team_id_task"],
        agent=extractor,
        #context = [interpretar_tarea], 
        output_pydantic=ExtraccionIDTeam
    )
    return Crew(agents=[extractor], tasks=[find_id_task], verbose=True)

def _create_generate_report_crew(mcp_tools: List[Tool]) -> Crew:
    """Crea y devuelve un Crew configurado para generar un informe de partidos."""
    executor = Agent(config=agente_info_team["query_executor"], tools=mcp_tools, llm=llm)
    generate_report_task = Task(config=tareas_info_team["generate_matches_report"], agent=executor)
    return Crew(agents=[executor], tasks=[generate_report_task], verbose=True)

# Clase Flow principal
class ExtraerInfoEquipoFlow(Flow):
    @start()
    def iniciar_flujo(self):
        team_name_inicial = self.state["team_name"]
        # Iniciar adaptador MCP
        with MCPServerAdapter(serverparams=server_params) as mcp_tools:
            print(f"Herramientas MCP disponibles: {[tool.name for tool in mcp_tools]}")

            # Crear agentes
            comentarista = Agent(config=agente_info_team["football_comentator"],
                                 llm = llm)
           
            interpretar_tarea = Task(
                config=tareas_info_team["interpret_user_input"],
                agent=comentarista,
                output_pydantic=InterpretacionExpertoComentarista
            )

            #find_id_crew = _create_find_id_crew(mcp_tools, interpretar_tarea)
            find_id_crew = _create_find_id_crew(mcp_tools)

            crew = Crew(agents=[comentarista, find_id_crew.agents[0]], tasks=[interpretar_tarea, find_id_crew.tasks[0]], verbose=True)
            crew_output = crew.kickoff(inputs={"team_name": team_name_inicial, "DATABASE_SCHEMA_CONTEXT": DATABASE_SCHEMA_CONTEXT})
            output_tarea = crew_output
            #self.state["team_name"] = crew_output["team_name"]
            print('DEBUG4')
            print(crew_output)
            print(self.state['team_name'])  ##ESTA LINEA DEBERIA SER EL OUTPUT DEL INTERPRETAR TAREA, EN MI CASO Barcelona en lugar de Barca
            print(output_tarea['team_id'])
            print(output_tarea['error'])

            if output_tarea['error'] == 'null' or output_tarea['error'] == 'None':
                self.state['team_id'] = 'ID_NOT_FOUND'
                return self

            self.state['team_id'] = output_tarea['team_id']
            return self

    @listen(iniciar_flujo)
    def get_last_matches_listener(self):
        print(f"\n--- Iniciando listener para obtener últimos partidos con ID: {self.state['team_id']} ---")

        if not self.state['team_id'] or self.state['team_id'] == 'ID_NOT_FOUND':
            error_msg = f"ID de equipo inválido ('{self.state['team_id']}') recibido. No se pueden obtener los partidos."
            print(error_msg)
            self.state["last_matches_csv_path"] = error_msg
            return error_msg
        
        # El objeto MCPServerAdapter es en sí mismo la colección de herramientas.
        with MCPServerAdapter(serverparams=server_params) as mcp_tools:
            report_crew = _create_generate_report_crew(mcp_tools)
            print('EJECUTANDO EXTRACCIÓN DE PARTIDOS!!!')
            inputs_listener = {"team_id": self.state['team_id'], "num_matches": self.state.get("num_matches", 5)}
            filepath = report_crew.kickoff(inputs=inputs_listener)
            
            self.state['last_matches_csv_path'] = filepath
            return self
    
    @listen(get_last_matches_listener)
    def find_rival_and_its_id_listener(self):
        original_team_name = self.state['team_name']
        if not original_team_name:
            print("\n--- [Rival] No hay nombre de equipo normalizado para buscar rival. Omitiendo.")
            self.state['rival_id'] = 'ID_NOT_FOUND'
            return
        
        print(f"\n--- [Rival] Buscando próximo rival de: {original_team_name} ---")
        search_tool = MyCustomDuckDuckGoTool()
        scout = Agent(config=agente_info_team["rival_scout"], tools=[search_tool], llm=llm2)
        find_rival_task = Task(config=tareas_info_team["find_rival_task"], agent=scout, output_pydantic=RivalName)
        rival_crew = Crew(agents=[scout], tasks=[find_rival_task], verbose=True)
        fecha_actual = datetime.now().strftime("%Y-%m-%d")
        rival_name_output = rival_crew.kickoff(inputs={"team_name": original_team_name, "fecha": fecha_actual})

        self.state["rival_name"] = rival_name_output["rival_name"]

        if not self.state["rival_name"]:
            print(f"--- [Rival] No se pudo encontrar un rival para {original_team_name}. ---")
            self.state['rival_id'] = 'ID_NOT_FOUND'
            return

        print(f'DEBUG: RIVAL ENCONTRADO: {self.state["rival_name"]}, BUSCANDO ID....')
        with MCPServerAdapter(serverparams=server_params) as mcp_tools:
            find_id_crew = _create_find_id_crew(mcp_tools)
            id_output = find_id_crew.kickoff(inputs={"team_name": self.state["rival_name"], "DATABASE_SCHEMA_CONTEXT": DATABASE_SCHEMA_CONTEXT})
            self.state["rival_id"] = id_output["team_id"]

            if not self.state["rival_id"]:
                self.state['rival_id'] = 'ID_NOT_FOUND'

        return self

if __name__ == "__main__":
    print("=== Búsqueda de Últimos Partidos Jugados ===")
    nombre_equipo = input("Introduce el nombre del equipo: ").strip()

    if not nombre_equipo:
        print("Nombre no válido. Finalizando.")
    else:
        flujo = ExtraerInfoEquipoFlow()
        print(flujo)
        # Puedes pasar parámetros iniciales al estado del flujo
        initial_state = {"team_name": nombre_equipo, "num_matches": 5}
        resultado_final_estado = flujo.kickoff(inputs=initial_state)
        
        
        print("\n--- Flujo Completado ---")
        
        
        team_id_result = flujo.state['team_id']
        print(f'DEBUG11: {team_id_result}')
        report_path_result = flujo.state["last_matches_csv_path"]
        print(f'DEBUG11: {report_path_result}')
        # --- FIN DE CAMBIOS ---

        if team_id_result and team_id_result != "ID_NOT_FOUND":
            print(f"ID del equipo encontrado: {team_id_result}")
        else:
            print("No se pudo determinar el ID del equipo.")

        if report_path_result:
            if isinstance(report_path_result, str) and ("Error" in report_path_result or "inválido" in report_path_result or "ID_NOT_FOUND" in report_path_result):
                 print(f"Error al generar el reporte: {report_path_result}")
            else:
                 print(f"Reporte de últimos partidos generado en: {report_path_result}")
        else:
            print("No se generó el reporte de últimos partidos.")

        print("\n--- Resultados para el Equipo Rival ---")
        rival_id = flujo.state["rival_id"]
        print(f"El rival_id es: {rival_id}")