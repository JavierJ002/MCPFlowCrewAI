### Información del Esquema de la Base de Datos 'sofaproject_schema': ###

Tabla principal de equipos: `teams`

Columnas relevantes:
- `id` (integer): El ID numérico único del equipo.
- `name` (varchar): El nombre completo oficial del equipo (ej. 'FC Barcelona').
- `slug` (varchar): Un identificador corto usado en URLs (ej. 'fc-barcelona').
- `country` (varchar): El país del equipo.

Ejemplo de consulta exitosa:
SELECT id FROM sofaproject_schema.teams WHERE name ILIKE '%barca%';
