import requests
import re
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

# ✅ URLs globales unificadas para evitar fallos de competidores vacíos en ligas específicas
URL_ESPN_TODOS = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
URL_ESPN_FIFA  = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
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

def _procesar_partidos(conn):
    """
    Lógica compartida dinámica y robusta con URLs globales de ESPN:
    1. Obtiene la fecha máxima guardada en la BD para filtrar a partir de ayer.
    2. Consulta ESPN Scoreboard global para ayer y hoy.
    3. Guarda nuevos partidos, sus plantillas y eventos clave.
    4. SINO tiene preguntas previas, genera nueva trivia con Gemini.
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
    
    ahora = datetime.now(timezone.utc)
    ayer = ahora - timedelta(days=1)
    fechas_a_revisar = list({ayer.strftime("%Y%m%d"), ahora.strftime("%Y%m%d")})
    
    print(f"Buscando partidos nuevos desde la fecha base: {ultima_fecha} en las fechas ESPN: {fechas_a_revisar}")
    partidos_candidatos = []
    
    try:
        for f_str in fechas_a_revisar:
            resp = requests.get(URL_ESPN_TODOS, params={"dates": f_str}, headers=HEADERS, timeout=15)
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
                    if fecha_inicio > ultima_fecha:
                        if id_evento not in partidos_candidatos:
                            partidos_candidatos.append(id_evento)
                except Exception:
                    if id_evento not in partidos_candidatos:
                        partidos_candidatos.append(id_evento)
                        
        # Procesamiento individual de los partidos encontrados
        partidos_procesados_exito = []
        for id_partido in partidos_candidatos:
            try:
                # 1. Guarda el partido, jugadores y eventos en la BD
                procesar_y_guardar_en_supabase(id_partido, conn)
                
                # 2. Genera la trivia con Gemini únicamente si no existe
                generar_y_guardar_trivia_partido(id_partido, conn)
                
                partidos_procesados_exito.append(id_partido)
            except Exception as ex_partido:
                print(f"  ❌ Error procesando el partido individual {id_partido}: {ex_partido}")
                
        return partidos_procesados_exito

    except Exception as e:
        print(f"Error obteniendo agenda incremental: {e}")
        return []

def procesar_y_guardar_en_supabase(id_partido, conn):
    print(f"  Procesando partido {id_partido}...")
    try:
        respuesta = requests.get(ESPN_SUMMARY, params={"event": id_partido}, headers=HEADERS, timeout=15)
        
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
        print(f"  ✓ {local.get('team',{}).get('name')} vs {visitante.get('team',{}).get('name')} guardado.")

    except Exception as e:
        print(f"  ✗ Error en partido {id_partido}: {e}")
        conn.rollback()

def _construir_contexto_partido(id_partido, conn):
    cursor = conn.cursor()
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

    cursor.execute('''
        SELECT tipo_evento, minuto, periodo, nombre_jugador, nombre_asistente, texto_evento
        FROM eventos_partido
        WHERE id_partido = %s
        ORDER BY minuto;
    ''', (id_partido,))
    eventos = cursor.fetchall()

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
                "responseMimeType": "application/json",
            },
        }
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        contenido = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        datos = json.loads(contenido)

        preguntas = datos if isinstance(datos, list) else datos.get("preguntas", list(datos.values())[0])
        print(f"  ✓ {len(preguntas)} preguntas generadas.")
        return preguntas

    except Exception as e:
        print(f"  ✗ Error al llamar a Gemini: {e}")
        return []

def guardar_preguntas_en_bd(id_partido, preguntas, conn):
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
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM preguntas_partido WHERE id_partido = %s;", (id_partido,))
    (cantidad,) = cursor.fetchone()
    cursor.close()

    if cantidad >= 10:
        print(f"  ℹ Partido {id_partido} ya tiene {cantidad} preguntas, se omite.")
        return

    preguntas = generar_preguntas_partido(id_partido, conn)
    guardar_preguntas_en_bd(id_partido, preguntas, conn)

def ejecutar_cron_diario():
    print(f"=== CRON iniciado: {datetime.now()} ===")
    try:
        conn = conectar_supabase()
    except Exception as e:
        print(f"ERROR conexión DB: {e}")
        traceback.print_exc()
        return
    try:
        
        cargar_ultimos_mundiales_en_bd()
        generar_trivias_todos_los_mundiales()
        partidos = _procesar_partidos(conn)
        print(f"=== CRON finalizado: {len(partidos)} partido(s) procesados. {datetime.now()} ===")
    except Exception as e:
        print(f"ERROR en cron: {e}")
        traceback.print_exc()
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# 🚀 SERVIDOR API REST (FLASK)
# ──────────────────────────────────────────────────────────────────────
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

def _verificar_token(datos):
    token_esperado = os.environ.get("ADMIN_TOKEN", "carinal1712")
    usuario = (datos.get("usuario") or datos.get("token") or "").strip()
    return usuario == token_esperado

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
            "partidos_processed": partidos,
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/actualizar-db', methods=['POST'])
def api_actualizar_db():
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
                "partidos_procesados": [],
            }), 200
        return jsonify({
            "status": "success",
            "message": f"Base de datos actualizada manualmente: {len(partidos)} partido(s) evaluados.",
            "partidos_processed": partidos,
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()

@app.route('/cargar-partido-manual', methods=['POST'])
def api_cargar_partido_manual():
    datos = request.get_json() or {}
    if not _verificar_token(datos):
        return jsonify({"status": "denied", "message": "Acceso prohibido."}), 403

    id_partido = str(datos.get("id_partido") or "").strip()
    if not id_partido:
        return jsonify({"status": "error", "message": "Falta especificar el parámetro 'id_partido' en el cuerpo JSON."}), 400

    conn = conectar_supabase()
    try:
        procesar_y_guardar_en_supabase(id_partido, conn)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM preguntas_partido WHERE id_partido = %s;", (id_partido,))
        (cantidad_preguntas,) = cursor.fetchone()
        cursor.close()

        ia_invocada = False
        if cantidad_preguntas > 0:
            msg_trivia = f"El partido ya poseía {cantidad_preguntas} preguntas. No se llamó a la IA."
        else:
            preguntas = generar_preguntas_partido(id_partido, conn)
            if preguntas:
                guardar_preguntas_en_bd(id_partido, preguntas, conn)
                ia_invocada = True
                msg_trivia = "Trivia generada exitosamente con Gemini."
            else:
                msg_trivia = "No se pudieron generar preguntas para el partido."

        return jsonify({
            "status": "success",
            "message": f"Partido {id_partido} procesado manualmente.",
            "trivia_status": msg_trivia,
            "ia_invocada": ia_invocada
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()
        
def test_diagnostico():
    print("==================================================")
    print("🔍 INICIANDO DIAGNÓSTICO DE CARGA DE PARTIDOS")
    print("==================================================\n")
    if not DB_USER or not DB_PASS:
        print("❌ ERROR: USER_BASE o CLAVE_BASE no están definidas.")
    else:
        print("✅ Variables de entorno para Base de Datos detectadas.")
    if not os.environ.get("GEMINI_API_KEY"):
        print("⚠️ ADVERTENCIA: GEMINI_API_KEY no está configurada.")
    else:
        print("✅ Variable GEMINI_API_KEY detectada.")

    print("\n2. [BASE DE DATOS] Conectando a Supabase...")
    conn = None
    ultima_fecha = None
    try:
        conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS, port=DB_PORT, connect_timeout=5)
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(fecha_partido) FROM partidos;")
        fila = cursor.fetchone()
        cursor.close()
        if fila and fila[0]:
            ultima_fecha = fila[0]
            print(f"✅ Conexión exitosa. Último partido registrado en BD: {ultima_fecha}")
        else:
            print("ℹ️ Conexión exitosa, pero la tabla 'partidos' está vacía.")
    except Exception as e:
        print(f"❌ ERROR al conectar o consultar Supabase: {e}")
    finally:
        if conn: conn.close()

    ahora = datetime.now(timezone.utc)
    ayer = ahora - timedelta(days=1)
    fechas_a_revisar = list({ayer.strftime("%Y%m%d"), ahora.strftime("%Y%m%d")})
    print(f"\n3. [TIEMPO] Fechas que se le enviarán a ESPN: {fechas_a_revisar}")

    print("\n4. [API ESPN] Probando respuestas globales...")
    for f_str in fechas_a_revisar:
        try:
            resp = requests.get(URL_ESPN_TODOS, params={"dates": f_str}, headers=HEADERS, timeout=10)
            print(f"      📅 Fecha {f_str} -> HTTP Status: {resp.status_code}")
            if resp.status_code == 200:
                eventos = resp.json().get('events', [])
                print(f"      Total partidos devueltos por ESPN: {len(eventos)}")
        except Exception as e:
            print(f"      ❌ Error con ESPN en fecha {f_str}: {e}")
    print("\n==================================================")

def cargar_ultimos_mundiales_en_bd():
    """
    Le pregunta a Gemini cuáles son los últimos mundiales de fútbol masculinos (p. ej. desde 1998 o 2002 en adelante)
    y los guarda en una tabla (por ejemplo, asumiendo una tabla llamada 'ligas' o adaptando el insert).
    """
    print("🔮 Consultando a Gemini sobre los últimos mundiales...")
    
    prompt_sistema = (
        "Sos un experto en historia del fútbol. Necesito una lista de los últimos mundiales de fútbol de la FIFA masculinos "
        "(por ejemplo, los últimos 6 o 7 mundiales). "
        "Respondé estrictamente en JSON puro, un array de strings con el nombre formateado de cada mundial. "
        "No uses markdown, no uses bloques de código (```json)."
    )
    
    prompt_usuario = "Dame un array JSON con los nombres oficiales de los últimos mundiales (Ejemplo: ['Copa Mundial de la FIFA Corea/Japón 2002', 'Copa Mundial de la FIFA Alemania 2006', ...])."

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": prompt_sistema + "\n\n" + prompt_usuario}]}
            ],
            "generationConfig": {
                "temperature": 0.3,
                "responseMimeType": "application/json",
            },
        }
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        contenido = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        mundiales = json.loads(contenido)
        
        if not isinstance(mundiales, list):
            print("❌ El formato devuelto por la IA no es una lista.")
            return

        print(f"✅ Se encontraron {len(mundiales)} mundiales. Guardando en la Base de Datos...")
        
        conn = conectar_supabase()
        cursor = conn.cursor()
        
        mundiales_insertados = 0
        mundiales_ya_existentes = 0
        
        for mundial_nombre in mundiales:
            # Extraemos el año (4 dígitos) del nombre del mundial, ej: "Copa Mundial ... 2002" -> 2002
            match_anio = re.search(r'(\d{4})', mundial_nombre)
            if not match_anio:
                print(f"⚠️ No se pudo determinar el año para '{mundial_nombre}'. Se omite.")
                continue
            
            anio_mundial = int(match_anio.group(1))
            
            # Buscamos si ya existe un mundial guardado con ese año
            cursor.execute('''
                SELECT id_mundial FROM mundial WHERE anio = %s;
            ''', (anio_mundial,))
            existente = cursor.fetchone()
            
            if existente:
                mundiales_ya_existentes += 1
                continue
            
            # No existe todavía: lo insertamos
            cursor.execute('''
                INSERT INTO mundial (detalle, anio)
                VALUES (%s, %s);
            ''', (mundial_nombre, anio_mundial))
            mundiales_insertados += 1
            
        conn.commit()
        cursor.close()
        conn.close()
        print(f"✓ Proceso finalizado. Insertados: {mundiales_insertados}. Ya existentes: {mundiales_ya_existentes}.")

    except Exception as e:
        print(f"❌ Error al cargar los mundiales en la base de datos: {e}")

def generar_trivias_todos_los_mundiales():
    """
    Recorre todos los mundiales guardados en la tabla 'mundial',
    y genera la trivia de 20 preguntas para cada uno, ignorando el del año en curso (2026).
    """
    print("🚀 Iniciando generación masiva de trivias de mundiales...")
    
    anio_actual = datetime.now().year  # En este caso detectará 2026
    
    try:
        conn = conectar_supabase()
        cursor = conn.cursor()
        
        # Buscamos todos los mundiales guardados, excepto el del año en curso
        cursor.execute('''
            SELECT detalle 
            FROM mundial 
            WHERE anio != %s;
        ''', (anio_actual,))
        filas = cursor.fetchall()
        cursor.close()
        conn.close()
        
        if not filas:
            print("⚠️ No se encontraron mundiales registrados en la tabla 'mundial'. Ejecutá primero el comando de carga.")
            return

        mundiales_procesados = 0
        for (nombre_mundial,) in filas:
            # Llamamos a la función que armamos antes para generar y guardar las 20 preguntas
            ObtenerTriviaMundialFinalizado(nombre_mundial)
            mundiales_procesados += 1
            
            # Una breve pausa de cortesía entre llamadas a la API de Gemini para evitar saturación (Rate Limits)
            time.sleep(2)
            
        print(f"🏁 Proceso masivo finalizado. Se generaron trivias para {mundiales_procesados} mundiales.")

    except Exception as e:
        print(f"❌ Error en el proceso masivo de trivias: {e}")

def ObtenerTriviaMundialFinalizado(nombre_mundial):
    """
    Recibe el nombre de un mundial, le pide a Gemini 20 preguntas avanzadas para fanáticos
    y las estructura/guarda en la base de datos junto a sus respuestas.
    """
    print(f"🎲 Generando 20 preguntas de trivia para: {nombre_mundial}...")
    
    prompt_sistema = (
        "Sos un historiador y experto en estadísticas de fútbol. Creá preguntas de trivia avanzadas "
        "para fanáticos exigentes sobre el mundial solicitado. "
        "Respondés siempre en JSON puro, sin markdown, sin bloques de código.\n"
        "El JSON debe ser un array de exactamente 20 objetos. "
        "Cada objeto tiene esta estructura:\n"
        '{"pregunta": "...", "opciones": ['
        '{"letra": "A", "texto": "...", "correcta": false},'
        '{"letra": "B", "texto": "...", "correcta": false},'
        '{"letra": "C", "texto": "...", "correcta": true},'
        '{"letra": "D", "texto": "...", "correcta": false}'
        "]}\n"
        "Exactamente una opción por pregunta debe tener correcta=true. "
        "Incluí variedad: campeones, subcampeones, goleadores, sorpresas, partidos épicos, sedes o hitos históricos de esa edición."
    )
    
    prompt_usuario = f"Generá la trivia de 20 preguntas difíciles para el torneo: {nombre_mundial}"

    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": prompt_sistema + "\n\n" + prompt_usuario}]}
            ],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 8000, 
                "responseMimeType": "application/json",
            },
        }
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        contenido = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        
        # Gemini a veces agrega texto extra después del JSON válido (aunque se le pida que no lo haga).
        # Nos quedamos solo con la porción entre el primer '[' y su ']' de cierre correspondiente.
        inicio = contenido.find('[')
        fin = contenido.rfind(']')
        if inicio != -1 and fin != -1 and fin > inicio:
            contenido = contenido[inicio:fin + 1]
        
        preguntas = json.loads(contenido)
        
        if not isinstance(preguntas, list):
            preguntas = preguntas.get("preguntas", list(preguntas.values())[0])

        print(f"🔥 Se generaron {len(preguntas)} preguntas de Gemini. Insertando en la BD...")
        
        id_partido_mundial = "".join(c if c.isalnum() else "_" for c in nombre_mundial.lower())
        
        conn = conectar_supabase()
        cursor = conn.cursor()
        
        # Primero guardamos el "partido" ficticio y hacemos COMMIT 
        # para que la FK en preguntas_partido no falle.
        cursor.execute('''
            INSERT INTO partidos (id_partido, fecha_partido, liga_nombre, equipo_local_nombre, equipo_visitante_nombre, ganador, tanda_penales)
            VALUES (%s, NOW(), %s, 'Historial', 'Mundial', 'empate', FALSE)
            ON CONFLICT (id_partido) DO NOTHING;
        ''', (id_partido_mundial, nombre_mundial))
        conn.commit()
        cursor.close()

        # Ahora sí, llamamos a tu función que guarda las preguntas Y las respuestas (opciones)
        guardar_preguntas_en_bd(id_partido_mundial, preguntas, conn)
        
        conn.close()
        print(f"✓ Trivia completa (preguntas y opciones) de '{nombre_mundial}' guardada correctamente.")
        return preguntas

    except Exception as e:
        print(f"❌ Error al generar u obtener la trivia del mundial: {e}")
        return []

def borrar_datos_temporada_2026():
    """
    Elimina en cascada todos los datos (respuestas, preguntas, eventos, jugadores y partidos)
    asociados a las ligas del año 2026 en la base de datos.
    """
    print("⚠️ Iniciando proceso de eliminación de datos de las ligas 2026...")
    
    # Confirmación de seguridad en consola antes de proceder
    confirmacion = input("¿Estás seguro de que querés borrar TODOS los datos de los partidos de 2026? (si/no): ")
    if confirmacion.lower() != 'si':
        print("❌ Operación cancelada por el usuario.")
        return

    conn = conectar_supabase()
    cursor = conn.cursor()
    
    try:
        # 1. Borrar opciones de respuesta asociadas a preguntas de partidos del 2026
        print(" -> Eliminando respuestas de preguntas de trivia (2026)...")
        cursor.execute('''
            DELETE FROM respuestas_preguntas 
            WHERE id_pregunta IN (
                SELECT id_pregunta FROM preguntas_partido 
                WHERE id_partido IN (
                    SELECT id_partido FROM partidos WHERE liga_nombre ILIKE '%2026%'
                )
            );
        ''')
        
        # 2. Borrar preguntas de trivia asociadas a partidos del 2026
        print(" -> Eliminando preguntas de trivia (2026)...")
        cursor.execute('''
            DELETE FROM preguntas_partido 
            WHERE id_partido IN (
                SELECT id_partido FROM partidos WHERE liga_nombre ILIKE '%2026%'
            );
        ''')
        
        # 3. Borrar eventos clave de los partidos del 2026
        print(" -> Eliminando eventos de partidos (2026)...")
        cursor.execute('''
            DELETE FROM eventos_partido 
            WHERE id_partido IN (
                SELECT id_partido FROM partidos WHERE liga_nombre ILIKE '%2026%'
            );
        ''')
        
        # 4. Borrar alineaciones/jugadores de los partidos del 2026
        print(" -> Eliminando jugadores por partido (2026)...")
        cursor.execute('''
            DELETE FROM jugadores_partido 
            WHERE id_partido IN (
                SELECT id_partido FROM partidos WHERE liga_nombre ILIKE '%2026%'
            );
        ''')
        
        # 5. Borrar finalmente los registros de la tabla principal de partidos
        print(" -> Eliminando registros principales de la tabla partidos (2026)...")
        cursor.execute('''
            DELETE FROM partidos 
            WHERE liga_nombre ILIKE '%2026%';
        ''', )
        
        # Si tenés las ligas del 2026 registradas de forma independiente en la tabla 'ligas'
        # y también querés removerlas, podés descomentar las siguientes líneas:
        # print(" -> Eliminando ligas de la temporada 2026...")
        # cursor.execute("DELETE FROM ligas WHERE nombre_liga ILIKE '%2026%';")

        conn.commit()
        print("✅ ¡Limpieza completada con éxito! Todos los datos de las ligas del 2026 han sido removidos.")
        
    except Exception as e:
        conn.rollback()
        print(f"❌ Error crítico durante la eliminación de datos: {e}")
    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    borrar_datos_temporada_2026()
    if not args:
        print("=== INICIANDO CRON DE PARTIDOS (RAILWAY) ===")
        try:
            ejecutar_cron_diario()
            print("✓ Procesamiento diario finalizado con éxito.")
        except Exception as e:
            print(f"❌ Error crítico en la ejecución del cron: {e}")
            sys.exit(1)

    elif args[0] == "test-trivia":
        id_test = args[1] if len(args) > 1 else None
        if not id_test:
            print("Indicá el id_partido: python main.py test-trivia <id_partido>")
            sys.exit(1)
        
        print(f"=== TEST: GENERANDO TRIVIA PARA PARTIDO {id_test} ===")
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
            
    elif args[0] == "test-fetch":
        print("=== INICIANDO PRUEBA DE OBTENCIÓN DE PARTIDOS ===")
        conn = conectar_supabase()
        try:
            max_fecha = obtener_ultima_fecha_partido(conn)
            print(f"-> Última fecha detectada en Base de Datos: {max_fecha}")
            ahora = datetime.now(timezone.utc)
            ayer = ahora - timedelta(days=1)
            fechas = [ayer.strftime("%Y%m%d"), ahora.strftime("%Y%m%d")]
            
            for f in fechas:
                r = requests.get(URL_ESPN_TODOS, params={"dates": f}, headers=HEADERS, timeout=15)
                print(f"   Scoreboard {f} | Status Code: {r.status_code}")
                if r.status_code == 200:
                    evs = r.json().get('events', [])
                    print(f"   Total eventos en JSON: {len(evs)}")
        except Exception as e:
            print(f"❌ Error en la prueba: {e}")
        finally:
            conn.close()
            print("=== PRUEBA FINALIZADA ===")
            
    else:
        print(f"Comando no reconocido: {args[0]}")
        sys.exit(1)
