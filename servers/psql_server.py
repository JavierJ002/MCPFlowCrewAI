import logging
import asyncio
import asyncpg
import os
import csv
import json # <--- IMPORTANTE: Importar json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence, Any, Dict, List
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from enum import Enum
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# --- (El código de las secciones 1, 2, 3 y 4 hasta la función find_team_id_by_name no cambia) ---
# ... (código existente sin cambios) ...

# --- 1. Modelo de Configuración de la Base de Datos ---
class DatabaseConnection(BaseModel):
    host: str = "localhost"
    port: int = 5432
    database: str
    username: str
    password: str = ""
    @classmethod
    def from_env(cls) -> "DatabaseConnection":
        config = cls(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            database=os.getenv("POSTGRES_DB") or cls._require_env("POSTGRES_DB"),
            username=os.getenv("POSTGRES_USER") or cls._require_env("POSTGRES_USER"),
            password=os.getenv("POSTGRES_PASSWORD") or cls._require_env("POSTGRES_PASSWORD")
        )
        logger = logging.getLogger(__name__)
        logger.info(f"Configuración de DB cargada: host={config.host}, port={config.port}, database={config.database}, username={config.username}")
        return config
    @staticmethod
    def _require_env(var_name: str) -> str:
        value = os.getenv(var_name)
        if not value: raise ValueError(f"La variable de entorno {var_name} es requerida.")
        return value

# --- 2. Modelos Pydantic para los Inputs de las Herramientas ---
class QueryDatabase(BaseModel):
    query: str = Field(description="La consulta SQL completa a ejecutar.")
    params: List[Any] = Field(default=[], description="Una lista de parámetros para la consulta SQL.")
class ListTables(BaseModel):
    schema_name: str = Field(default="public", description="El nombre del esquema.")
class DescribeTable(BaseModel):
    table_name: str = Field(description="El nombre de la tabla.")
    schema_name: str = Field(default="public", description="El esquema al que pertenece la tabla.")
class ListSchemas(BaseModel):
    pass
class GetTableData(BaseModel):
    table_name: str = Field(description="El nombre de la tabla.")
    schema_name: str = Field(default="public", description="El esquema.")
    limit: int = Field(default=10, description="El número máximo de filas a devolver.")
    offset: int = Field(default=0, description="El número de filas a omitir.")
class ExecuteTransaction(BaseModel):
    queries: List[str] = Field(description="Una lista de consultas SQL para ejecutar.")
class GetLastMatches(BaseModel):
    team_id: str = Field(description="El ID del equipo.")
    num_matches: int = Field(default=5, ge=1, le=50, description="El número de partidos.")
class FindTeamIdByName(BaseModel):
    team_name: str = Field(description="El nombre canónico del equipo a buscar.")

# --- 3. Enumeración de Herramientas ---
class PostgreSQLTools(str, Enum):
    QUERY = "psql_query"
    LIST_TABLES = "psql_list_tables"
    DESCRIBE_TABLE = "psql_describe_table"
    LIST_SCHEMAS = "psql_list_schemas"
    GET_TABLE_DATA = "psql_get_table_data"
    EXECUTE_TRANSACTION = "psql_execute_transaction"
    GET_LAST_MATCHES = "psql_get_last_matches"
    FIND_TEAM_ID_BY_NAME = "psql_find_team_id_by_name"

# --- 4. Clase de Lógica del Servidor PostgreSQL ---
class PostgreSQLServer:
    def __init__(self, connection_config: DatabaseConnection):
        self.config = connection_config
        self.pool = None
        self.logger = logging.getLogger(__name__)

    async def initialize_pool(self, max_retries: int = 3):
        for attempt in range(max_retries):
            try:
                self.logger.info(f"Intento {attempt + 1} de conexión a PostgreSQL...")
                await self._test_single_connection()
                self.pool = await asyncpg.create_pool(host=self.config.host, port=self.config.port, database=self.config.database, user=self.config.username, password=self.config.password, min_size=1, max_size=10, command_timeout=30, server_settings={'jit': 'off', 'application_name': 'mcp-postgresql'}, init=self._init_connection)
                self.logger.info(f"¡Pool de conexiones inicializado exitosamente!")
                return
            except Exception as e:
                self.logger.error(f"Intento {attempt + 1} fallido: {str(e)}")
                if attempt < max_retries - 1: await asyncio.sleep(2 ** attempt)
                else: self.logger.error("Todos los intentos de conexión fallaron"); raise

    async def _test_single_connection(self):
        conn = await asyncpg.connect(host=self.config.host, port=self.config.port, database=self.config.database, user=self.config.username, password=self.config.password, command_timeout=10)
        await conn.close()

    async def _init_connection(self, conn):
        await conn.execute("SET timezone = 'UTC'")

    async def close_pool(self):
        if self.pool: await self.pool.close()

    async def execute_query(self, query: str, params: List[Any] = None) -> Dict[str, Any]:
        if not self.pool: raise RuntimeError("El pool de la base de datos no está inicializado")
        async with self.pool.acquire() as conn:
            try:
                result = await conn.fetch(query, *params) if params else await conn.fetch(query)
                return {"success": True, "rows": [dict(row) for row in result], "row_count": len(result)}
            except Exception as e:
                return {"success": False, "error": str(e), "row_count": 0}

    async def list_tables(self, schema_name: str = "public") -> Dict[str, Any]:
        query = "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = $1 ORDER BY table_name"
        return await self.execute_query(query, [schema_name])

    async def describe_table(self, table_name: str, schema_name: str = "public") -> Dict[str, Any]:
        query = "SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2 ORDER BY ordinal_position"
        return await self.execute_query(query, [schema_name, table_name])
    
    async def list_schemas(self) -> Dict[str, Any]:
        query = "SELECT schema_name FROM information_schema.schemata WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast') ORDER BY schema_name"
        return await self.execute_query(query)

    async def find_team_id_by_name(self, team_name: str) -> Dict[str, Any]:
        if not self.pool: raise RuntimeError("El pool de la base de datos no está inicializado")
        clean_team_name = team_name.strip()
        words = clean_team_name.split()
        significant_word = max(words, key=len) if words else clean_team_name
        search_patterns = [
            (clean_team_name, "coincidencia exacta (nombre completo)"),
            (f"%{significant_word}%", f"contiene la palabra significativa '{significant_word}'"),
            (f"%{clean_team_name}%", "la DB contiene el nombre completo")
        ]
        query = "SELECT team_id, name FROM public.teams WHERE name ILIKE $1"
        for pattern, strategy_name in search_patterns:
            self.logger.info(f"Buscando con estrategia '{strategy_name}': ILIKE '{pattern}'")
            result = await self.execute_query(query, [pattern])
            if not result["success"]: return {"success": False, "error": result["error"]}
            if result["row_count"] == 1:
                team_id = result["rows"][0]["team_id"]
                found_name = result["rows"][0]["name"]
                self.logger.info(f"Éxito con '{strategy_name}': Se encontró un único equipo. ID: {team_id}, Nombre: {found_name}")
                return {"success": True, "team_id": team_id}
            if result["row_count"] > 1:
                found_teams = [row['name'] for row in result['rows']]
                error_msg = f"Búsqueda ambigua con '{strategy_name}'. Se encontraron {result['row_count']} equipos: {', '.join(found_teams)}"
                self.logger.warning(error_msg)
                return {"success": False, "error": error_msg}
        error_msg = f"No se encontró ningún equipo que coincida con '{team_name}' después de intentar todas las estrategias."
        self.logger.warning(error_msg)
        return {"success": False, "error": error_msg}

# --- 5. Función Auxiliar de Formateo ---
def format_query_result(result: Dict[str, Any]) -> str:
    if not result.get("success"): return f"Error: {result.get('error', 'Error desconocido')}"
    if result.get("row_count", 0) == 0: return "La consulta no devolvió filas."
    rows = result.get("rows", [])
    if not rows: return "No se encontraron datos."
    columns = list(rows[0].keys())
    header = " | ".join(columns)
    output = [header, "-" * len(header)]
    for row in rows: output.append(" | ".join(str(row.get(col, "")) for col in columns))
    output.append(f"\n({result['row_count']} filas)")
    return "\n".join(output)

# --- 6. Lógica Principal del Servidor MCP ---
async def serve(connection_config: DatabaseConnection) -> None:
    logger = logging.getLogger(__name__)
    psql_server = PostgreSQLServer(connection_config)
    await psql_server.initialize_pool()
    server = Server("mcp-postgresql")
    
    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(name=PostgreSQLTools.QUERY, description="Ejecuta una única consulta SQL.", inputSchema=QueryDatabase.model_json_schema()),
            Tool(name=PostgreSQLTools.LIST_TABLES, description="Lista todas las tablas y vistas.", inputSchema=ListTables.model_json_schema()),
            Tool(name=PostgreSQLTools.DESCRIBE_TABLE, description="Muestra la estructura de una tabla.", inputSchema=DescribeTable.model_json_schema()),
            Tool(name=PostgreSQLTools.LIST_SCHEMAS, description="Lista todos los esquemas.", inputSchema=ListSchemas.model_json_schema()),
            Tool(name=PostgreSQLTools.GET_LAST_MATCHES, description="Genera un informe CSV con los últimos partidos.", inputSchema=GetLastMatches.model_json_schema()),
            Tool(name=PostgreSQLTools.FIND_TEAM_ID_BY_NAME, description="Herramienta de alto nivel para encontrar el ID de un equipo por su nombre. Usa una búsqueda por prioridad para asegurar la precisión.", inputSchema=FindTeamIdByName.model_json_schema()),
        ]
    
    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == PostgreSQLTools.FIND_TEAM_ID_BY_NAME:
                team_name = arguments.get("team_name")
                if not team_name:
                    error_json = json.dumps({"team_id": None, "error": "El argumento 'team_name' es requerido."})
                    return [TextContent(type="text", text=error_json)]
                
                result = await psql_server.find_team_id_by_name(team_name)
                
                # --- COMIENZO DE CAMBIOS: Formatear la salida como JSON ---
                if result["success"]:
                    # Crear un diccionario que coincida con el modelo Pydantic
                    output_data = {"team_id": str(result["team_id"]), "error": None}
                else:
                    # Crear un diccionario de error que coincida con el modelo Pydantic
                    output_data = {"team_id": None, "error": result["error"]}
                
                # Convertir el diccionario a una cadena JSON
                json_output = json.dumps(output_data)
                return [TextContent(type="text", text=json_output)]
                # --- FIN DE CAMBIOS ---

            elif name == PostgreSQLTools.QUERY:
                # ... (resto de las llamadas a herramientas sin cambios)
                result = await psql_server.execute_query(arguments["query"], arguments.get("params", []))
                return [TextContent(type="text", text=format_query_result(result))]
            elif name == PostgreSQLTools.LIST_TABLES:
                result = await psql_server.list_tables(arguments.get("schema_name", "public"))
                return [TextContent(type="text", text=format_query_result(result))]
            elif name == PostgreSQLTools.DESCRIBE_TABLE:
                result = await psql_server.describe_table(arguments["table_name"], arguments.get("schema_name", "public"))
                return [TextContent(type="text", text=format_query_result(result))]
            elif name == PostgreSQLTools.LIST_SCHEMAS:
                result = await psql_server.list_schemas()
                return [TextContent(type="text", text=format_query_result(result))]
            elif name == PostgreSQLTools.GET_LAST_MATCHES:
                team_id_str = arguments.get("team_id")
                num_matches = arguments.get("num_matches", 5)
                if not team_id_str or not team_id_str.isdigit(): return [TextContent(type="text", text=f"Error: ID de equipo inválido: '{team_id_str}'.")]
                team_id = int(team_id_str)
                now_utc = datetime.now(timezone.utc)
                sql_query = """
                    SELECT m.match_id, m.match_datetime_utc, t.name AS tournament_name, m.round_number,
                           home_team.name AS home_team_name, away_team.name AS away_team_name,
                           m.home_score, m.away_score, v.name AS venue_name
                    FROM public.matches AS m
                    JOIN public.teams AS home_team ON m.home_team_id = home_team.team_id
                    JOIN public.teams AS away_team ON m.away_team_id = away_team.team_id
                    JOIN public.seasons AS s ON m.season_id = s.season_id
                    JOIN public.tournaments AS t ON s.tournament_id = t.tournament_id
                    LEFT JOIN public.venues AS v ON m.venue_id = v.venue_id
                    WHERE (m.home_team_id = $1 OR m.away_team_id = $1) AND m.match_datetime_utc < $2
                    ORDER BY m.match_datetime_utc DESC
                    LIMIT $3;
                """
                query_result = await psql_server.execute_query(sql_query, [team_id, now_utc, num_matches])
                if not query_result["success"]: return [TextContent(type="text", text=f"Error en la base de datos: {query_result['error']}")]
                if not query_result["rows"]: return [TextContent(type="text", text=f"No se encontraron partidos pasados para el equipo ID {team_id}.")]
                output_dir = "reports"
                os.makedirs(output_dir, exist_ok=True)
                timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                filepath = Path(output_dir) / f"ultimos_partidos_{team_id}_{timestamp_str}.csv"
                with open(filepath, 'w', newline='', encoding='utf-8') as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=query_result["rows"][0].keys())
                    writer.writeheader()
                    writer.writerows(query_result["rows"])
                return [TextContent(type="text", text=f"Archivo CSV generado exitosamente en: {filepath.resolve()}")]
            else:
                raise ValueError(f"Herramienta desconocida: {name}")
        except Exception as e:
            logger.error(f"Error ejecutando la herramienta {name}: {str(e)}", exc_info=True)
            return [TextContent(type="text", text=f"Error interno del servidor: {str(e)}")]
    
    try:
        options = server.create_initialization_options()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, options, raise_exceptions=True)
    finally:
        await psql_server.close_pool()

# --- 7. Punto de Entrada del Script ---
async def main():
    load_dotenv()
    try:
        config = DatabaseConnection.from_env()
        await serve(config)
    except ValueError as e:
        print(f"Error de configuración: {e}")
        print("Asegúrate de que tu archivo .env contenga todas las variables requeridas.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    asyncio.run(main())