import requests
import psycopg2
from psycopg2 import extras
from datetime import datetime, timedelta
import time

DB_HOST = "aws-1-us-east-2.pooler.supabase.com"
DB_NAME = "postgres"
DB_USER = "postgres.vlndghikrjvxmiibbqbo"
DB_PASS = "Lif#Cari.Fuk"
DB_PORT = "6543"

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
        connect_timeout=10  # ✅ timeout en la conexión también
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

        # Mapa de type.type de ESPN → categoría normalizada que guardamos
        # Ignoramos kickoff, halftime, start-2nd-half, end-regular-time (sin participants)
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

            # Solo procesamos tipos que están en nuestro mapa
            if tipo_type not in TIPO_MAP:
                continue

            # Si no hay participants, no hay jugador — igual insertamos el evento
            participantes = evento.get('participants', [])
            if not participantes:
                continue

            id_ev = evento.get('id', f"{id_partido}_{time.time_ns()}")

            # Minuto desde displayValue: "6'" → 6, "45+2'" → 45
            clock_display = evento.get('clock', {}).get('displayValue', '0')
            minuto = int(''.join(filter(str.isdigit, clock_display.split('+')[0].split(':')[0])) or 0)

            # Periodo
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

            # participants[0] = actor principal (goleador, amonestado, sale en sust.)
            # participants[1] = secundario (asistente en gol, entra en sust.)
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
                atleta_0.get('id'),          # goleador / amonestado / sale
                atleta_0.get('displayName'),
                tipo_limpio, minuto, periodo,
                atleta_1.get('id') or None,          # asistente / entra (None si no hay)
                atleta_1.get('displayName') or None,
                texto_evento
            ))

        conn.commit()
        cursor.close()
        print(f"  ✓ {local.get('team',{}).get('name')} vs {visitante.get('team',{}).get('name')}")

    except Exception as e:
        print(f"  ✗ Error en partido {id_partido}: {e}")
        conn.rollback()  # ✅ evita dejar la transacción rota

def ejecutar_cron_diario():
    print(f"=== CRON iniciado: {datetime.now()} ===")
    
    # ✅ Una sola conexión para todo el proceso
    conn = conectar_supabase()
    try:
        iniciar_tablas_supabase(conn)
        partidos = obtener_partidos_dia_anterior()
        print(f"Procesando {len(partidos)} partidos...")
        for i, id_p in enumerate(partidos, 1):
            print(f"[{i}/{len(partidos)}]", end=" ")
            procesar_y_guardar_en_supabase(id_p, conn)
            time.sleep(2)
    finally:
        conn.close()  # ✅ siempre cierra aunque falle
    
    print(f"=== CRON finalizado: {datetime.now()} ===")

if __name__ == "__main__":
    ejecutar_cron_diario()
