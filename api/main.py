import requests
import psycopg2
from psycopg2 import extras
from datetime import datetime, timedelta
import time
import json
import uuid
import os

DB_HOST = "aws-1-us-east-2.pooler.supabase.com"
DB_NAME = "postgres"
DB_USER = "postgres.vlndghikrjvxmiibbqbo"
DB_PASS = "Lif#Cari.Fuk"
DB_PORT = "6543"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")   # ← tu clave de Gemini (gratis en aistudio.google.com)
GEMINI_MODEL   = "gemini-flash-lite-latest"  # rápido y gratuito

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ✅ URLs correctas de la API pública de ESPN
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
ESPN_SUMMARY   = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/summary"

def conectar_supabase():
    return psycopg2.connect(
        host=DB_HOST, database=DB_NAME,
        user=DB_USER, password=DB_PASS, port=DB_PORT,
        connect_timeout=10
    )

def iniciar_tablas_supabase(conn):
    """Recibe la conexión en vez de crear una nueva"""
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS partidos (
            id_partido VARCHAR(50) PRIMARY KEY,
            fecha_partido TIMESTAMP,
            liga_nombre VARCHAR(100),
            equipo_local_id VARCHAR(50),
            equipo_local_nombre VARCHAR(100),
            equipo_local_goles INT DEFAULT 0,
            equipo_visitante_id VARCHAR(50),
            equipo_visitante_nombre VARCHAR(100),
            equipo_visitante_goles INT DEFAULT 0,
            ganador VARCHAR(20),
            tanda_penales BOOLEAN DEFAULT FALSE,
            fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jugadores_partido (
            id_registro VARCHAR(100) PRIMARY KEY,
            id_partido VARCHAR(50) REFERENCES partidos(id_partido) ON DELETE CASCADE,
            id_equipo VARCHAR(50),
            id_jugador VARCHAR(50),
            nombre_jugador VARCHAR(150),
            posicion VARCHAR(50),
            titular BOOLEAN DEFAULT TRUE
        );
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS eventos_partido (
            id_evento VARCHAR(50) PRIMARY KEY,
            id_partido VARCHAR(50) REFERENCES partidos(id_partido) ON DELETE CASCADE,
            id_equipo VARCHAR(50),
            id_jugador VARCHAR(50),
            nombre_jugador VARCHAR(150),
            tipo_evento VARCHAR(50),
            minuto INT,
            periodo VARCHAR(20),
            id_asistente VARCHAR(50),
            nombre_asistente VARCHAR(150),
            texto_evento TEXT
        );
    ''')
    # Migración no destructiva: agrega columnas si la tabla ya existía sin ellas
    for col, definition in [
        ("id_asistente",    "VARCHAR(50)"),
        ("nombre_asistente","VARCHAR(150)"),
        ("texto_evento",    "TEXT"),
    ]:
        cursor.execute(f"ALTER TABLE eventos_partido ADD COLUMN IF NOT EXISTS {col} {definition};")
    conn.commit()
    cursor.close()
    print("✓ Tablas verificadas.")

def obtener_partidos_dia_anterior():
    ayer = datetime.now() - timedelta(days=1)
    fecha_str = ayer.strftime("%Y%m%d")

    print(f"Consultando partidos del {fecha_str}...")
    try:
        respuesta = requests.get(
            ESPN_SCOREBOARD,
            params={"dates": fecha_str},
            headers=HEADERS,
            timeout=15
        )
        print(f"  ESPN scoreboard status: {respuesta.status_code}")
        if respuesta.status_code != 200:
            return []
        datos = respuesta.json()
        ids = [evento.get('id') for evento in datos.get('events', [])]
        print(f"  Encontrados {len(ids)} partidos.")
        return ids
    except Exception as e:
        print(f"Error al obtener agenda: {e}")
        return []

def procesar_y_guardar_en_supabase(id_partido, conn):
    """✅ Recibe la conexión compartida"""
    print(f"  Procesando partido {id_partido}...")
    try:
        respuesta = requests.get(
            ESPN_SUMMARY,
            params={"event": id_partido},
            headers=HEADERS,
            timeout=15
        )
        if respuesta.status_code != 200:
            print(f"  HTTP {respuesta.status_code} para partido {id_partido}, saltando.")
            return

        datos = respuesta.json()
        header = datos.get('header', {})
        competitions = header.get('competitions', [{}])[0]
        competitors = competitions.get('competitors', [])
        if len(competitors) < 2:
            print(f"  Sin competitors, saltando {id_partido}.")
            return

        local     = next((c for c in competitors if c.get('homeAway') == 'home'), competitors[0])
        visitante = next((c for c in competitors if c.get('homeAway') == 'away'), competitors[1])

        g_local = int(local.get('score', 0))
        g_vis   = int(visitante.get('score', 0))
        ganador = 'local' if g_local > g_vis else ('visitante' if g_vis > g_local else 'empate')
        hubo_penales = 'shootout' in competitions

        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO partidos (id_partido, fecha_partido, liga_nombre,
                equipo_local_id, equipo_local_nombre, equipo_local_goles,
                equipo_visitante_id, equipo_visitante_nombre, equipo_visitante_goles,
                ganador, tanda_penales)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id_partido) DO UPDATE SET
                equipo_local_goles = EXCLUDED.equipo_local_goles,
                equipo_visitante_goles = EXCLUDED.equipo_visitante_goles,
                ganador = EXCLUDED.ganador;
        ''', (
            id_partido, header.get('date'), header.get('league', {}).get('name'),
            local.get('team', {}).get('id'), local.get('team', {}).get('name'), g_local,
            visitante.get('team', {}).get('id'), visitante.get('team', {}).get('name'), g_vis,
            ganador, hubo_penales
        ))

        for equipo_roster in datos.get('rosters', []):
            id_equipo = equipo_roster.get('team', {}).get('id')
            for j in equipo_roster.get('roster', []):
                id_j   = j.get('athlete', {}).get('id')
                id_reg = f"{id_partido}_{id_j}"
                cursor.execute('''
                    INSERT INTO jugadores_partido
                        (id_registro, id_partido, id_equipo, id_jugador, nombre_jugador, posicion, titular)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id_registro) DO NOTHING;
                ''', (
                    id_reg, id_partido, id_equipo, id_j,
                    j.get('athlete', {}).get('displayName'),
                    j.get('athlete', {}).get('position', {}).get('name'),
                    j.get('starter', True)
                ))

        TIPO_MAP = {
            'goal':               'Gol',
            'own-goal':           'Gol en Propia',
            'penalty---scored':   'Penal Convertido',
            'penalty---missed':   'Penal Fallado',
            'penalty---saved':    'Penal Atajado',
            'yellow-card':        'Tarjeta Amarilla',
            'red-card':           'Tarjeta Roja',
            'yellow-red-card':    'Doble Amarilla',
            'substitution':       'Sustitución',
        }

        for evento in datos.get('keyEvents', []):
            tipo_type = evento.get('type', {}).get('type', '').lower()
            if tipo_type not in TIPO_MAP:
                continue

            participantes = evento.get('participants', [])
            if not participantes:
                continue

            id_ev = evento.get('id', f"{id_partido}_{time.time_ns()}")

            clock_display = evento.get('clock', {}).get('displayValue', '0')
            minuto = int(''.join(filter(str.isdigit, clock_display.split('+')[0].split(':')[0])) or 0)

            periodo_num = evento.get('period', {}).get('number', 1)
            if periodo_num <= 2:
                periodo = f"P{periodo_num}"
            elif periodo_num == 3:
                periodo = "ET1"
            elif periodo_num == 4:
                periodo = "ET2"
            else:
                periodo = "PS"

            id_equipo      = evento.get('team', {}).get('id')
            tipo_limpio    = TIPO_MAP[tipo_type]
            texto_evento   = evento.get('text', '')

            atleta_0 = participantes[0].get('athlete', {})
            atleta_1 = participantes[1].get('athlete', {}) if len(participantes) > 1 else {}

            cursor.execute('''
                INSERT INTO eventos_partido
                    (id_evento, id_partido, id_equipo, id_jugador, nombre_jugador,
                     tipo_evento, minuto, periodo,
                     id_asistente, nombre_asistente, texto_evento)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id_evento) DO NOTHING;
            ''', (
                id_ev, id_partido, id_equipo,
                atleta_0.get('id'),
                atleta_0.get('displayName'),
                tipo_limpio, minuto, periodo,
                atleta_1.get('id') or None,
                atleta_1.get('displayName') or None,
                texto_evento
            ))

        
        cursor.execute('''DELETE FROM RESPUESTAS_PREGUNTAS''')
        cursor.execute('''DELETE FROM PREGUNTAS_PARTIDO''')
    
        conn.commit()
        cursor.close()
        print(f"  ✓ {local.get('team',{}).get('name')} vs {visitante.get('team',{}).get('name')}")

    except Exception as e:
        print(f"  ✗ Error en partido {id_partido}: {e}")
        conn.rollback()

# ──────────────────────────────────────────────
# NUEVAS FUNCIONES: TRIVIA CON IA
# ──────────────────────────────────────────────

def _construir_contexto_partido(id_partido, conn):
    """
    Arma un texto resumido del partido leyendo las tablas ya cargadas.
    Devuelve un dict con los datos del partido, o None si no existe.
    """
    cursor = conn.cursor()

    # Datos generales del partido
    cursor.execute('''
        SELECT id_partido, fecha_partido, liga_nombre,
               equipo_local_nombre, equipo_local_goles,
               equipo_visitante_nombre, equipo_visitante_goles,
               ganador, tanda_penales
        FROM partidos WHERE id_partido = %s;
    ''', (id_partido,))
    fila = cursor.fetchone()
    if not fila:
        cursor.close()
        return None

    (id_p, fecha, liga, loc_nombre, loc_goles,
     vis_nombre, vis_goles, ganador, penales) = fila

    # Eventos del partido
    cursor.execute('''
        SELECT tipo_evento, minuto, periodo, nombre_jugador, nombre_asistente, texto_evento
        FROM eventos_partido
        WHERE id_partido = %s
        ORDER BY minuto;
    ''', (id_partido,))
    eventos = cursor.fetchall()

    # Titulares de cada equipo
    cursor.execute('''
        SELECT jp.nombre_jugador, jp.posicion, p.equipo_local_nombre,
               p.equipo_visitante_nombre,
               CASE WHEN jp.id_equipo = p.equipo_local_id THEN 'local' ELSE 'visitante' END AS bando
        FROM jugadores_partido jp
        JOIN partidos p ON p.id_partido = jp.id_partido
        WHERE jp.id_partido = %s AND jp.titular = TRUE;
    ''', (id_partido,))
    jugadores = cursor.fetchall()
    cursor.close()

    # Armar texto de contexto
    lineas = [
        f"Partido: {loc_nombre} {loc_goles} - {vis_goles} {vis_nombre}",
        f"Liga: {liga}",
        f"Fecha: {fecha}",
        f"Resultado: {ganador}" + (" (definido por penales)" if penales else ""),
        "",
        "EVENTOS CLAVE:",
    ]
    for (tipo, minuto, periodo, jugador, asistente, texto) in eventos:
        desc = f"  Min {minuto} ({periodo}) - {tipo}: {jugador or '?'}"
        if asistente:
            desc += f" (asistencia: {asistente})"
        if texto:
            desc += f" — {texto}"
        lineas.append(desc)

    lineas.append("")
    lineas.append("TITULARES:")
    for (nombre, posicion, loc_eq, vis_eq, bando) in jugadores:
        eq = loc_nombre if bando == 'local' else vis_nombre
        lineas.append(f"  [{eq}] {nombre} - {posicion or 'N/D'}")

    return {
        "id_partido": id_p,
        "loc_nombre": loc_nombre,
        "vis_nombre": vis_nombre,
        "contexto_texto": "\n".join(lineas),
    }


def generar_preguntas_partido(id_partido, conn):
    """
    Llama a OpenAI con el contexto del partido y genera 20 preguntas
    de opción múltiple (4 opciones cada una, una correcta). 
    Devuelve una lista de dicts con el formato:
      [{ "pregunta": str, "opciones": [{"letra": "A", "texto": str, "correcta": bool}, ...] }]
    o lista vacía si hay error.
    """
    contexto = _construir_contexto_partido(id_partido, conn)
    if not contexto:
        print(f"  ✗ No se encontró el partido {id_partido} en la BD para generar preguntas.")
        return []

    print(f"  Generando preguntas para {contexto['loc_nombre']} vs {contexto['vis_nombre']}...")

    prompt_sistema = (
        "Sos un experto en fútbol que crea preguntas de trivia sobre partidos. "
        "Respondés siempre en JSON puro, sin markdown, sin bloques de código. "
        "Por favor no hagas preguntas que indiquen minutos en el que ocurrio un evento, si puede ser al estilo primer timpo."
        "El JSON debe ser un array de exactamente 20 objetos. "
        "Cada objeto tiene esta estructura:\n"
        '{"pregunta": "...", "opciones": ['
        '{"letra": "A", "texto": "...", "correcta": false},'
        '{"letra": "B", "texto": "...", "correcta": false},'
        '{"letra": "C", "texto": "...", "correcta": true},'
        '{"letra": "D", "texto": "...", "correcta": false}'
        "]}\n"
        "Exactamente una opción por pregunta debe tener correcta=true. "
        "Las preguntas deben ser variadas: goles, asistencias, tarjetas, "
        "minutos de gol, titulares, resultado final, estadísticas, etc. "
        "Usá solo información del contexto provisto; no inventes datos."
    )

    prompt_usuario = (
        f"Generá 20 preguntas de trivia sobre este partido:\n\n"
        f"{contexto['contexto_texto']}"
    )

    try:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        )
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": prompt_sistema + "\n\n" + prompt_usuario}]}
            ],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 4000,
                "responseMimeType": "application/json",  # fuerza JSON puro
            },
        }
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        contenido = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        datos = json.loads(contenido)

        # El modelo puede devolver {"preguntas": [...]} o directamente [...]
        preguntas = datos if isinstance(datos, list) else datos.get("preguntas", list(datos.values())[0])

        print(f"  ✓ {len(preguntas)} preguntas generadas.")
        return preguntas

    except Exception as e:
        print(f"  ✗ Error al llamar a Gemini: {e}")
        return []


def guardar_preguntas_en_bd(id_partido, preguntas, conn):
    """
    Guarda la lista de preguntas (y sus opciones) en las tablas
    preguntas_partido y respuestas_preguntas.
    Usa ON CONFLICT DO NOTHING para idempotencia.
    """
    if not preguntas:
        return

    cursor = conn.cursor()
    try:
        for nro, item in enumerate(preguntas, start=1):
            id_pregunta = f"{id_partido}_{nro:02d}"
            cursor.execute('''
                INSERT INTO preguntas_partido (id_pregunta, id_partido, nro_pregunta, pregunta)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id_pregunta) DO NOTHING;
            ''', (id_pregunta, id_partido, nro, item.get("pregunta", "")))

            for opcion in item.get("opciones", []):
                letra      = opcion.get("letra", "A")
                id_resp    = f"{id_pregunta}_{letra}"
                cursor.execute('''
                    INSERT INTO respuestas_preguntas
                        (id_respuesta, id_pregunta, letra, texto_opcion, es_correcta)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (id_respuesta) DO NOTHING;
                ''', (
                    id_resp, id_pregunta, letra,
                    opcion.get("texto", ""),
                    opcion.get("correcta", False),
                ))

        conn.commit()
        print(f"  ✓ Preguntas guardadas en BD para partido {id_partido}.")
    except Exception as e:
        conn.rollback()
        print(f"  ✗ Error al guardar preguntas en BD: {e}")
    finally:
        cursor.close()


def generar_y_guardar_trivia_partido(id_partido, conn):
    """
    Orquesta la generación y persistencia de trivia para un partido.
    Verifica si ya existen preguntas para evitar regenerarlas.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM preguntas_partido WHERE id_partido = %s;",
        (id_partido,)
    )
    (cantidad,) = cursor.fetchone()
    cursor.close()

    if cantidad >= 20:
        print(f"  ℹ Partido {id_partido} ya tiene {cantidad} preguntas, se omite.")
        return

    preguntas = generar_preguntas_partido(id_partido, conn)
    guardar_preguntas_en_bd(id_partido, preguntas, conn)


def obtener_preguntas_partido(id_partido, conn):
    """
    Devuelve todas las preguntas con sus opciones para un partido dado.
    Formato de retorno:
    [
      {
        "id_pregunta": str,
        "nro_pregunta": int,
        "pregunta": str,
        "opciones": [
          {"id_respuesta": str, "letra": str, "texto": str, "es_correcta": bool},
          ...
        ]
      },
      ...
    ]
    Devuelve lista vacía si el partido no tiene preguntas cargadas.
    """
    cursor = conn.cursor()
    cursor.execute('''
        SELECT p.id_pregunta, p.nro_pregunta, p.pregunta,
               r.id_respuesta, r.letra, r.texto_opcion, r.es_correcta
        FROM preguntas_partido p
        JOIN respuestas_preguntas r ON r.id_pregunta = p.id_pregunta
        WHERE p.id_partido = %s
        ORDER BY p.nro_pregunta, r.letra;
    ''', (id_partido,))
    filas = cursor.fetchall()
    cursor.close()

    # Agrupar por pregunta
    preguntas_dict = {}
    for (id_preg, nro, texto_preg, id_resp, letra, texto_op, correcta) in filas:
        if id_preg not in preguntas_dict:
            preguntas_dict[id_preg] = {
                "id_pregunta": id_preg,
                "nro_pregunta": nro,
                "pregunta": texto_preg,
                "opciones": [],
            }
        preguntas_dict[id_preg]["opciones"].append({
            "id_respuesta": id_resp,
            "letra": letra,
            "texto": texto_op,
            "es_correcta": correcta,
        })

    return list(preguntas_dict.values())
def get_id_partido_por_nombre(partido_nombre, conn):
    """
    Busca el id_partido basándose en el nombre descriptivo del partido.
    Intenta separar los nombres si contienen 'vs' o ' - ' para buscar a ambos equipos.
    Devuelve el id_partido (str) si lo encuentra, o None si no hay coincidencias.
    """
    if not partido_nombre:
        return None

    cursor = conn.cursor()
    
    # 1. Intento de búsqueda directa por coincidencia parcial de todo el texto
    try:
        query_directa = """
            SELECT id_partido FROM partidos 
            WHERE equipo_local_nombre ILIKE %s 
               OR equipo_visitante_nombre ILIKE %s
            LIMIT 1;
        """
        param = f"%{partido_nombre.strip()}%"
        cursor.execute(query_directa, (param, param))
        fila = cursor.fetchone()
        if fila:
            cursor.close()
            return fila[0]

        # 2. Si no funcionó, separamos por los separadores comunes 'vs' o '-' 
        separadores = [" vs ", " - ", " vs. "]
        partes = []
        for sep in separadores:
            if sep in partido_nombre.lower():
                partes = partido_nombre.lower().split(sep)
                break
        
        if len(partes) >= 2:
            eq1 = partes[0].strip()
            eq2 = partes[1].strip()

            # Buscamos que un equipo sea local y el otro visitante (o viceversa)
            query_combinada = """
                SELECT id_partido FROM partidos 
                WHERE (equipo_local_nombre ILIKE %s AND equipo_visitante_nombre ILIKE %s)
                   OR (equipo_local_nombre ILIKE %s AND equipo_visitante_nombre ILIKE %s)
                LIMIT 1;
            """
            cursor.execute(query_combinada, (f"%{eq1}%", f"%{eq2}%", f"%{eq2}%", f"%{eq1}%"))
            fila = cursor.fetchone()
            if fila:
                cursor.close()
                return fila[0]

    except Exception as e:
        print(f"Error en get_id_partido_por_nombre: {e}")
    finally:
        cursor.close()

    return None

# ──────────────────────────────────────────────
# CRON PRINCIPAL
# ──────────────────────────────────────────────

def ejecutar_cron_diario():
    print(f"=== CRON iniciado: {datetime.now()} ===")

    conn = conectar_supabase()
    try:
        iniciar_tablas_supabase(conn)
        partidos = obtener_partidos_dia_anterior()
        print(f"Procesando {len(partidos)} partidos...")

        for i, id_p in enumerate(partidos, 1):
            print(f"[{i}/{len(partidos)}]", end=" ")
            procesar_y_guardar_en_supabase(id_p, conn)
            time.sleep(2)

        # ── Generación de trivia ──
        print("\n--- Generando trivia con IA ---")
        for i, id_p in enumerate(partidos, 1):
            print(f"[{i}/{len(partidos)}]", end=" ")
            generar_y_guardar_trivia_partido(id_p, conn)
            time.sleep(3)   # pausa para no saturar la API de OpenAI

    finally:
        conn.close()

    print(f"=== CRON finalizado: {datetime.now()} ===")
if __name__ == "__main__":
    import sys
    args = sys.argv[1:]

    if args and args[0] == "test-trivia":
        # Uso: python main.py test-trivia <id_partido>
        id_test = args[1] if len(args) > 1 else None
        if not id_test:
            print("Indicá el id_partido: python main.py test-trivia <id_partido>")
            sys.exit(1)
        conn = conectar_supabase()
        try:
            preguntas = generar_preguntas_partido(id_test, conn)
            if preguntas:
                print(f"\n{len(preguntas)} preguntas generadas:")
                for i, p in enumerate(preguntas, 1):
                    print(f"\n{i}. {p['pregunta']}")
                    for op in p['opciones']:
                        correcta = " (correcta)" if op['correcta'] else ""
                        print(f"   {op['letra']}) {op['texto']}{correcta}")
        finally:
            conn.close()
    else:
        ejecutar_cron_diario()
