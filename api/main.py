import requests
import traceback
import psycopg2
from psycopg2 import extras
from datetime import datetime, timedelta, timezone
import time
import json
import uuid
import os

DB_HOST = "aws-1-us-east-2.pooler.supabase.com"
DB_NAME = "postgres"
DB_USER = os.environ.get("USER_BASE")
DB_PASS = os.environ.get("CLAVE_BASE")
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

def obtener_ultima_fecha_partido(conn):
    """Consulta la fecha del último partido registrado en la base de datos."""
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT MAX(fecha_partido) FROM partidos;")
        fila = cursor.fetchone()
        if fila and fila[0]:
            return fila[0]
    except Exception as e:
        print(f"Error al obtener última fecha de la BD: {e}")
    finally:
        cursor.close()
    return None

def obtener_partidos_ultimas_6_horas():
    ahora = datetime.now(timezone.utc)
    # Ampliamos a 12 horas para tener un margen seguro y no perder partidos
    hace_12_horas = ahora - timedelta(hours=12)

    # Consultamos hoy, ayer y anteayer para evitar desfases por zonas horarias o medianoches
    fechas = list({
        ahora.strftime("%Y%m%d"), 
        (ahora - timedelta(days=1)).strftime("%Y%m%d"), 
        (ahora - timedelta(days=2)).strftime("%Y%m%d")
    })

    print(f"Consultando partidos finalizados en la ventana de tiempo para las fechas: {', '.join(fechas)}...")
    ids = []
    
    try:
        for fecha_str in fechas:
            respuesta = requests.get(
                ESPN_SCOREBOARD,
                params={"dates": fecha_str},
                headers=HEADERS,
                timeout=15
            )
            print(f"  ESPN scoreboard {fecha_str}: HTTP {respuesta.status_code}")
            
            if respuesta.status_code != 200:
                continue

            datos_json = respuesta.json()
            eventos = datos_json.get('events', [])
            
            for evento in eventos:
                id_evento = evento.get('id')
                estado = evento.get('status', {})
                tipo = estado.get('type', {})

                # 1. Filtro estricto: Solo partidos que ya terminaron
                if not tipo.get('completed', False):
                    continue

                # 2. Filtro de ventana de tiempo
                fecha_evento_str = evento.get('date', '')
                try:
                    # Parsear la fecha en formato ISO (UTC)
                    fecha_inicio = datetime.fromisoformat(fecha_evento_str.replace('Z', '+00:00'))
                    # Estimamos que el partido dura aprox. 2 horas
                    fecha_fin_estimada = fecha_inicio + timedelta(hours=2)
                    
                    # Si el partido terminó dentro de las últimas 12 horas, lo agregamos
                    if fecha_fin_estimada >= hace_12_horas:
                        if id_evento not in ids:
                            ids.append(id_evento)
                except Exception:
                    # Si falla el parseo de la fecha, lo agregamos igual por seguridad si está 'completed'
                    if id_evento not in ids:
                        ids.append(id_evento)

        print(f"  -> Total de partidos encontrados y listos para procesar: {len(ids)}")
        return ids
        
    except Exception as e:
        print(f"Error crítico al obtener agenda de ESPN: {e}")
        return []

        
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
        "El JSON debe ser un array de exactamente 10 objetos. "
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
        f"Generá 10 preguntas de trivia sobre este partido:\n\n"
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

    if cantidad >= 10:
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
    try:
        conn = conectar_supabase()
    except Exception as e:
        print(f"ERROR conexión DB: {e}")
        traceback.print_exc()
        return
    try:
        partidos = _procesar_partidos(conn)
        print(f"=== CRON finalizado: {len(partidos)} partido(s) procesados. {datetime.now()} ===")
    except Exception as e:
        print(f"ERROR en cron: {e}")
        traceback.print_exc()
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# 🚀 AGREGADO SEGURO: SERVIDOR API REST CON TOKEN DE ENTORNO
# ──────────────────────────────────────────────────────────────────────
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

def _verificar_token(datos):
    """Valida el token de administrador contra la variable de entorno ADMIN_TOKEN."""
    token_esperado = os.environ.get("ADMIN_TOKEN", "carinal1712")
    usuario = (datos.get("usuario") or datos.get("token") or "").strip()
    return usuario == token_esperado


def _procesar_partidos(conn):
    """
    Lógica compartida entre el cron y los endpoints manuales:
    1. Obtiene la fecha máxima guardada en la BD para filtrar a partir de ayer.
    2. Consulta ESPN Scoreboard para el día de ayer y hoy.
    3. Guarda nuevos partidos.
    4. SINO tiene preguntas previas, genera nueva trivia con la IA (evita duplicar/gastar tokens).
    """
    ultima_fecha_str = obtener_ultima_fecha_partido(conn)
    ultima_fecha = None
    
    if ultima_fecha_str:
        try:
            if isinstance(ultima_fecha_str, datetime):
                ultima_fecha = ultima_fecha_str.astimezone(timezone.utc)
            else:
                ultima_fecha = datetime.fromisoformat(ultima_fecha_str.replace('Z', '+00:00'))
        except Exception:
            pass

    # Si no hay registros previos, se calcula por defecto desde las últimas 24 horas
    if not ultima_fecha:
        ultima_fecha = datetime.now(timezone.utc) - timedelta(days=1)
    
    # Nos aseguramos de revisar siempre desde el día de ayer y hoy
    ahora = datetime.now(timezone.utc)
    ayer = ahora - timedelta(days=1)
    fechas_a_revisar = list({ayer.strftime("%Y%m%d"), ahora.strftime("%Y%m%d")})
    
    print(f"Buscando partidos nuevos desde la fecha base: {ultima_fecha} en las fechas ESPN: {fechas_a_revisar}")
    partidos_candidatos = []
    
    try:
        for f_str in fechas_a_revisar:
            resp = requests.get(ESPN_SCOREBOARD, params={"dates": f_str}, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            
            eventos = resp.json().get('events', [])
            for evento in eventos:
                id_evento = evento.get('id')
                estado = evento.get('status', {})
                tipo = estado.get('type', {})
                
                if not tipo.get('completed', False):
                    continue
                
                fecha_evento_str = evento.get('date', '')
                try:
                    fecha_inicio = datetime.fromisoformat(fecha_evento_str.replace('Z', '+00:00'))
                    # Filtrado por fecha y hora exactas
                    if fecha_inicio > ultima_fecha:
                        if id_evento not in partidos_candidatos:
                            partidos_candidatos.append(id_evento)
                except Exception:
                    if id_evento not in partidos_candidatos:
                        partidos_candidatos.append(id_evento)
    except Exception as e:
        print(f"Error obteniendo agenda incremental: {e}")
        return []

    if not partidos_candidatos:
        print("No se encontraron nuevos partidos finalizados desde el último chequeo.")
        return []

    cursor = conn.cursor()
    for id_p in partidos_candidatos:
        try:
            # 1. Guardar o actualizar datos básicos del partido
            procesar_y_guardar_en_supabase(id_p, conn)
            
            # 2. CONTROL DE TOKENS: Verificar si ya existen preguntas creadas para este partido
            cursor.execute("SELECT COUNT(*) FROM preguntas_partido WHERE id_partido = %s;", (id_p,))
            (cantidad_preguntas,) = cursor.fetchone()
            
            if cantidad_preguntas > 0:
                print(f"  ℹ El partido {id_p} ya tiene {cantidad_preguntas} preguntas registradas. Se omite el llamado a la IA.")
                continue
                
            # 3. Si no tiene preguntas, se invoca a Gemini de forma segura
            preguntas = generar_preguntas_partido(id_p, conn)
            guardar_preguntas_en_bd(id_p, preguntas, conn)
            
        except Exception as e:
            print(f"  ERROR procesando partido {id_p}: {e}")
            traceback.print_exc()
            try:
                conn.rollback()
            except Exception:
                pass
    
    cursor.close()
    return partidos_candidatos


@app.route('/regenerar-trivia', methods=['POST'])
def api_regenerar_trivia():
    datos = request.get_json() or {}

    if not _verificar_token(datos):
        return jsonify({"status": "denied", "message": "Acceso prohibido."}), 403

    conn = conectar_supabase()
    try:
        partidos = _procesar_partidos(conn)

        if not partidos:
            return jsonify({"status": "error", "message": "No se encontraron partidos nuevos para procesar."}), 404

        return jsonify({
            "status": "success",
            "message": f"Proceso completado. Se evaluaron {len(partidos)} partidos sin destruir trivias existentes.",
            "partidos_procesados": partidos,
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()


@app.route('/actualizar-db', methods=['POST'])
def api_actualizar_db():
    """
    Endpoint de actualización manual completa.
    Igual que el cron: obtiene partidos, guarda datos y genera trivia de los que no la posean.

    Uso:
        POST /actualizar-db
        Content-Type: application/json
        { "usuario": "<ADMIN_TOKEN>" }
    """
    datos = request.get_json() or {}

    if not _verificar_token(datos):
        return jsonify({"status": "denied", "message": "Acceso prohibido."}), 403

    conn = conectar_supabase()
    try:
        partidos = _procesar_partidos(conn)

        if not partidos:
            return jsonify({
                "status": "ok",
                "message": "No hay partidos nuevos para procesar.",
                "partidos_processed": [],
            }), 200

        return jsonify({
            "status": "success",
            "message": f"Base de datos actualizada: {len(partidos)} partido(s) procesados y trivia evaluada.",
            "partidos_procesados": partidos,
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

if __name__ == "__main__":
    import sys
    from apscheduler.schedulers.background import BackgroundScheduler

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
            
    elif args and args[0] == "test-fetch":
        print("=== INICIANDO PRUEBA DE OBTENCIÓN DE PARTIDOS ===")
        conn = conectar_supabase()
        try:
            max_fecha = obtener_ultima_fecha_partido(conn)
            print(f"-> Última fecha detectada en Base de Datos: {max_fecha}")
            
            ahora = datetime.now(timezone.utc)
            ayer = ahora - timedelta(days=1)
            fechas = [ayer.strftime("%Y%m%d"), ahora.strftime("%Y%m%d")]
            print(f"-> Evaluando API de ESPN para las fechas: {fechas}")
            
            for f in fechas:
                r = requests.get(ESPN_SCOREBOARD, params={"dates": f}, headers=HEADERS, timeout=15)
                print(f"   Scoreboard {f} | Status Code: {r.status_code}")
                if r.status_code == 200:
                    evs = r.json().get('events', [])
                    print(f"   Total eventos en JSON: {len(evs)}")
                    for ev in evs[:5]:
                        print(f"     - Partido ID: {ev.get('id')} | {ev.get('name')} | Fecha: {ev.get('date')} | Terminado: {ev.get('status', {}).get('type', {}).get('completed')}")
        except Exception as e:
            print(f"Error en la prueba: {e}")
        finally:
            conn.close()
            print("=== PRUEBA FINALIZADA ===")
    else:
        # Inicializar tablas al arrancar
        conn = conectar_supabase()
        conn.close()

        # Programar el cron cada 6 horas
        scheduler = BackgroundScheduler()
        scheduler.add_job(ejecutar_cron_diario, 'interval', hours=6, id='cron_partidos')
        scheduler.start()
        print("✓ Cron programado cada 6 horas.")

        # Levantar Flask (el cron corre en background)
        app.run(host="0.0.0.0", port=5000)
