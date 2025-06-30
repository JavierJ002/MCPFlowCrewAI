import logging
import asyncio
import asyncpg
from pathlib import Path
from typing import Sequence, Any, Dict, List
from mcp.server import Server
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server
from mcp.types import (
    ClientCapabilities,
    TextContent,
    Tool,
    ListRootsResult,
    RootsCapability,
)
from enum import Enum
from pydantic import BaseModel, Field
import json
import os
from dotenv import load_dotenv

class DatabaseConnection(BaseModel):
    host: str = "localhost"
    port: int = 5432
    database: str
    username: str
    password: str = ""

    @classmethod
    def from_env(cls) -> "DatabaseConnection":
        """Crear configuración desde variables de entorno"""
        config = cls(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            database=os.getenv("POSTGRES_DB") or cls._require_env("POSTGRES_DB"),
            username=os.getenv("POSTGRES_USER") or cls._require_env("POSTGRES_USER"),
            # Exige la contraseña para evitar conexiones con password vacía
            password=os.getenv("POSTGRES_PASSWORD") or cls._require_env("POSTGRES_PASSWORD")
        )
        
        # Log de la configuración (sin password)
        logger = logging.getLogger(__name__)
        logger.info(f"Configuración de DB cargada: host={config.host}, port={config.port}, database={config.database}, username={config.username}")
        
        return config
    
    @staticmethod
    def _require_env(var_name: str) -> str:
        """Requiere que una variable de entorno esté definida"""
        value = os.getenv(var_name)
        if not value:
            raise ValueError(f"La variable de entorno {var_name} es requerida")
        return value

class QueryDatabase(BaseModel):
    query: str = Field(description="SQL query to execute")
    params: List[Any] = Field(default=[], description="Parameters for the query")

class ListTables(BaseModel):
    schema_name: str = Field(default="sofaproject_schema", description="Schema name to list tables from")

class DescribeTable(BaseModel):
    table_name: str = Field(description="Name of the table to describe")
    schema_name: str = Field(default="sofaproject_schema", description="Schema name")

class ListSchemas(BaseModel):
    pass

class GetTableData(BaseModel):
    table_name: str = Field(description="Name of the table")
    schema_name: str = Field(default="sofaproject_schema", description="Schema name")
    limit: int = Field(default=100, description="Maximum number of rows to return")
    offset: int = Field(default=0, description="Number of rows to skip")

class ExecuteTransaction(BaseModel):
    queries: List[str] = Field(description="List of SQL queries to execute in a transaction")

class PostgreSQLTools(str, Enum):
    QUERY = "psql_query"
    LIST_TABLES = "psql_list_tables"
    DESCRIBE_TABLE = "psql_describe_table"
    LIST_SCHEMAS = "psql_list_schemas"
    GET_TABLE_DATA = "psql_get_table_data"
    EXECUTE_TRANSACTION = "psql_execute_transaction"

class PostgreSQLServer:
    def __init__(self, connection_config: DatabaseConnection):
        self.config = connection_config
        self.pool = None
        self.logger = logging.getLogger(__name__)

    async def initialize_pool(self, max_retries: int = 3):
        """Initialize the connection pool with retry logic"""
        for attempt in range(max_retries):
            try:
                self.logger.info(f"Intento {attempt + 1} de conexión a PostgreSQL...")
                
                # Primero probamos una conexión individual
                await self._test_single_connection()
                
                # Si la conexión individual funciona, creamos el pool
                self.pool = await asyncpg.create_pool(
                    host=self.config.host,
                    port=self.config.port,
                    database=self.config.database,
                    user=self.config.username,
                    password=self.config.password,
                    min_size=1,
                    max_size=10,
                    command_timeout=30,
                    server_settings={
                        'jit': 'off',
                        'application_name': 'mcp-postgresql'
                    },
                    init=self._init_connection
                )
                
                self.logger.info(f"¡Pool de conexiones inicializado exitosamente en {self.config.host}:{self.config.port}/{self.config.database}!")
                return
                
            except Exception as e:
                self.logger.error(f"Intento {attempt + 1} fallido: {str(e)}")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    self.logger.info(f"Esperando {wait_time} segundos antes del siguiente intento...")
                    await asyncio.sleep(wait_time)
                else:
                    self.logger.error("Todos los intentos de conexión fallaron")
                    raise

    async def _test_single_connection(self):
        """Test a single connection before creating pool"""
        self.logger.info("Probando conexión individual...")
        conn = await asyncpg.connect(
            host=self.config.host,
            port=self.config.port,
            database=self.config.database,
            user=self.config.username,
            password=self.config.password,
            command_timeout=10
        )
        await conn.close()
        self.logger.info("Conexión individual exitosa")

    async def _init_connection(self, conn):
        """Initialize each connection in the pool"""
        await conn.execute("SET timezone = 'UTC'")

    async def close_pool(self):
        """Close the connection pool"""
        if self.pool:
            await self.pool.close()
            self.logger.info("Pool de conexiones cerrado")

    async def execute_query(self, query: str, params: List[Any] = None) -> Dict[str, Any]:
        """Execute a SELECT query and return results"""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        
        async with self.pool.acquire() as conn:
            try:
                if params:
                    result = await conn.fetch(query, *params)
                else:
                    result = await conn.fetch(query)
                
                # Convert result to list of dictionaries
                rows = [dict(row) for row in result]
                
                return {
                    "success": True,
                    "rows": rows,
                    "row_count": len(rows)
                }
            except Exception as e:
                self.logger.error(f"Error ejecutando query: {str(e)}")
                return {
                    "success": False,
                    "error": str(e),
                    "row_count": 0
                }

    async def execute_command(self, query: str, params: List[Any] = None) -> Dict[str, Any]:
        """Execute a command query (INSERT, UPDATE, DELETE) and return results"""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        
        async with self.pool.acquire() as conn:
            try:
                if params:
                    result = await conn.execute(query, *params)
                else:
                    result = await conn.execute(query)
                
                return {
                    "success": True,
                    "message": f"Command executed successfully: {result}",
                    "result": result
                }
            except Exception as e:
                self.logger.error(f"Error ejecutando comando: {str(e)}")
                return {
                    "success": False,
                    "error": str(e)
                }

    async def list_tables(self, schema_name: str = "sofaproject_schema") -> List[Dict[str, str]]:
        """List all tables in a schema"""
        query = """
        SELECT table_name, table_type 
        FROM information_schema.tables 
        WHERE table_schema = $1
        ORDER BY table_name
        """
        result = await self.execute_query(query, [schema_name])
        return result["rows"] if result["success"] else []

    async def describe_table(self, table_name: str, schema_name: str = "sofaproject_schema") -> List[Dict[str, Any]]:
        """Get table structure information"""
        query = """
        SELECT 
            column_name,
            data_type,
            is_nullable,
            column_default,
            character_maximum_length,
            numeric_precision,
            numeric_scale
        FROM information_schema.columns 
        WHERE table_schema = $1 AND table_name = $2
        ORDER BY ordinal_position
        """
        result = await self.execute_query(query, [schema_name, table_name])
        return result["rows"] if result["success"] else []

    async def list_schemas(self) -> List[Dict[str, str]]:
        """List all schemas"""
        query = """
        SELECT schema_name 
        FROM information_schema.schemata 
        WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast')
        ORDER BY schema_name
        """
        result = await self.execute_query(query)
        return result["rows"] if result["success"] else []

    async def get_table_data(self, table_name: str, schema_name: str = "sofaproject_schema", 
                           limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        """Get data from a table with pagination"""
        query = f"""
        SELECT * FROM {schema_name}.{table_name}
        LIMIT $1 OFFSET $2
        """
        return await self.execute_query(query, [limit, offset])

    async def execute_transaction(self, queries: List[str]) -> Dict[str, Any]:
        """Execute multiple queries in a transaction"""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                try:
                    results = []
                    for query in queries:
                        result = await conn.execute(query)
                        results.append(result)
                    
                    return {
                        "success": True,
                        "results": results,
                        "message": f"Transaction completed successfully. {len(queries)} queries executed."
                    }
                except Exception as e:
                    self.logger.error(f"Error en transacción: {str(e)}")
                    return {
                        "success": False,
                        "error": str(e),
                        "message": "Transaction rolled back due to error."
                    }

def format_query_result(result: Dict[str, Any]) -> str:
    """Format query result for display"""
    if not result["success"]:
        return f"Error: {result['error']}"
    
    if result["row_count"] == 0:
        return "No rows returned."
    
    # Format as a simple table
    rows = result["rows"]
    if not rows:
        return "No data found."
    
    # Get column names
    columns = list(rows[0].keys())
    
    # Create header
    output = []
    output.append(" | ".join(columns))
    output.append("-" * len(output[0]))
    
    # Add rows
    for row in rows:
        output.append(" | ".join(str(row.get(col, "")) for col in columns))
    
    output.append(f"\n({result['row_count']} rows)")
    return "\n".join(output)

async def serve(connection_config: DatabaseConnection) -> None:
    logger = logging.getLogger(__name__)
    
    psql_server = PostgreSQLServer(connection_config)
    
    logger.info("Intentando inicializar el pool de conexiones a la base de datos...")
    try:
        await psql_server.initialize_pool()
        logger.info("¡Pool de conexiones inicializado con ÉXITO!")
    except Exception as e:
        logger.error(f"¡¡¡FALLO CRÍTICO al inicializar el pool de conexiones!!! Error: {e}", exc_info=True)
        raise

    server = Server("mcp-postgresql")
    
    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=PostgreSQLTools.QUERY,
                description="Execute a SQL query (SELECT, INSERT, UPDATE, DELETE)",
                inputSchema=QueryDatabase.model_json_schema(),
            ),
            Tool(
                name=PostgreSQLTools.LIST_TABLES,
                description="List all tables in a schema",
                inputSchema=ListTables.model_json_schema(),
            ),
            Tool(
                name=PostgreSQLTools.DESCRIBE_TABLE,
                description="Get the structure/schema of a table",
                inputSchema=DescribeTable.model_json_schema(),
            ),
            Tool(
                name=PostgreSQLTools.LIST_SCHEMAS,
                description="List all schemas in the database",
                inputSchema=ListSchemas.model_json_schema(),
            ),
            Tool(
                name=PostgreSQLTools.GET_TABLE_DATA,
                description="Get data from a table with pagination",
                inputSchema=GetTableData.model_json_schema(),
            ),
            Tool(
                name=PostgreSQLTools.EXECUTE_TRANSACTION,
                description="Execute multiple queries in a transaction",
                inputSchema=ExecuteTransaction.model_json_schema(),
            ),
        ]
    
    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            match name:
                case PostgreSQLTools.QUERY:
                    query = arguments["query"]
                    params = arguments.get("params", [])
                    
                    # Determine if it's a SELECT or command query
                    if query.strip().upper().startswith("SELECT"):
                        result = await psql_server.execute_query(query, params)
                        formatted_result = format_query_result(result)
                    else:
                        result = await psql_server.execute_command(query, params)
                        if result["success"]:
                            formatted_result = result["message"]
                        else:
                            formatted_result = f"Error: {result['error']}"
                    
                    return [TextContent(type="text", text=formatted_result)]
                
                case PostgreSQLTools.LIST_TABLES:
                    schema = arguments.get("schema_name", "sofaproject_schema")
                    tables = await psql_server.list_tables(schema)
                    
                    if tables:
                        table_list = "\n".join([f"- {t['table_name']} ({t['table_type']})" for t in tables])
                        result = f"Tables in schema '{schema}':\n{table_list}"
                    else:
                        result = f"No tables found in schema '{schema}'"
                    
                    return [TextContent(type="text", text=result)]
                
                case PostgreSQLTools.DESCRIBE_TABLE:
                    table_name = arguments["table_name"]
                    schema_name = arguments.get("schema_name", "sofaproject_schema")
                    columns = await psql_server.describe_table(table_name, schema_name)
                    
                    if columns:
                        col_info = []
                        for col in columns:
                            nullable = "NULL" if col["is_nullable"] == "YES" else "NOT NULL"
                            default = f" DEFAULT {col['column_default']}" if col["column_default"] else ""
                            col_info.append(f"- {col['column_name']}: {col['data_type']} {nullable}{default}")
                        
                        result = f"Structure of table '{schema_name}.{table_name}':\n" + "\n".join(col_info)
                    else:
                        result = f"Table '{schema_name}.{table_name}' not found"
                    
                    return [TextContent(type="text", text=result)]
                
                case PostgreSQLTools.LIST_SCHEMAS:
                    schemas = await psql_server.list_schemas()
                    
                    if schemas:
                        schema_list = "\n".join([f"- {s['schema_name']}" for s in schemas])
                        result = f"Available schemas:\n{schema_list}"
                    else:
                        result = "No schemas found"
                    
                    return [TextContent(type="text", text=result)]
                
                case PostgreSQLTools.GET_TABLE_DATA:
                    table_name = arguments["table_name"]
                    schema_name = arguments.get("schema_name", "sofaproject_schema")
                    limit = arguments.get("limit", 100)
                    offset = arguments.get("offset", 0)
                    
                    result = await psql_server.get_table_data(table_name, schema_name, limit, offset)
                    formatted_result = f"Data from {schema_name}.{table_name}:\n" + format_query_result(result)
                    
                    return [TextContent(type="text", text=formatted_result)]
                
                case PostgreSQLTools.EXECUTE_TRANSACTION:
                    queries = arguments["queries"]
                    result = await psql_server.execute_transaction(queries)
                    
                    if result["success"]:
                        formatted_result = result["message"]
                    else:
                        formatted_result = f"Transaction failed: {result['error']}"
                    
                    return [TextContent(type="text", text=formatted_result)]
                
                case _:
                    raise ValueError(f"Unknown tool: {name}")
        
        except Exception as e:
            logger.error(f"Error executing tool {name}: {str(e)}")
            return [TextContent(type="text", text=f"Error: {str(e)}")]
    
    try:
        options = server.create_initialization_options()
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, options, raise_exceptions=True)
    finally:
        await psql_server.close_pool()

async def main():
    script_path = Path(__file__).resolve()
    project_root = script_path.parent.parent.parent.parent 
    env_path = project_root / ".env"
    
    load_dotenv(dotenv_path=env_path, override=True)
    
    
    # Verificar que el archivo .env existe
    if not os.path.exists(env_path):
        print(f"ADVERTENCIA: No se encontró el archivo .env en {env_path}")
    
    try:
        config = DatabaseConnection.from_env()
    except ValueError as e:
        print(f"Error de configuración: {e}")
        print("Asegúrate de que tu archivo .env contenga todas las variables requeridas:")
        print("- POSTGRES_DB")
        print("- POSTGRES_USER")
        return
    
    await serve(config)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    asyncio.run(main())