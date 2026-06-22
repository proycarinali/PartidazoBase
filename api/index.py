import requests
import psycopg2
from psycopg2 import extras
from datetime import datetime, timedelta
import time

# --- 1. CONFIGURACIÓN DE TU BASE DE DATOS EN SUPABASE ---
# Copia estos datos desde el panel de Configuración -> Database de Supabase

DB_HOST = "db.vlndghikrjvxmiibbqbo.supabase.co" # Cambia esto
DB_NAME = "postgres"                     # Por defecto en Supabase siempre es 'postgres'
DB_USER = "postgres"                     # Por defecto es 'postgres'
DB_PASS = "Lif#Cari.Fuk"    # La contraseña que creaste al inicio
DB_PORT = "5432"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def conectar_supabase():
    """Establece la conexión con la base de datos PostgreSQL de Supabase"""
    return psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        port=DB_PORT
    )

def iniciar_tablas_supabase():
    """Crea la estructura de tablas relacionales en Supabase si no existen"""
    conn = conectar_supabase()
    cursor = conn.cursor()
    
    # Tabla Partidos
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
    
    # Tabla Jugadores
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
    
    # Tabla Eventos (Goles / Penales)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS eventos_partido (
            id_evento VARCHAR(50) PRIMARY KEY,
            id_partido VARCHAR(50) REFERENCES partidos(id_partido) ON DELETE CASCADE,
            id_equipo VARCHAR(50),
            id_jugador VARCHAR(50),
            nombre_jugador VARCHAR(150),
            tipo_evento VARCHAR(50),
            minuto INT,
            periodo VARCHAR(20)
        );
    ''')
    
    conn.commit()
    cursor.close()
    conn.close()
    print("✓ Estructura de base de datos verificada en Supabase.")

def obtener_partidos_dia_anterior():
    ayer = datetime.now() - timedelta(days=1)
    fecha_str = ayer.strftime("%Y%m%d")
    url_agenda = f"https://espn.com{fecha_str}"
    
    try:
        respuesta = requests.get(url_agenda, headers=HEADERS, timeout=15)
        if respuesta.status_code != 200: return []
        datos = respuesta.json()
        return [evento.get('id') for evento in datos.get('events', [])]
    except Exception as e:
        print(f"Error al obtener agenda: {e}")
        return []

def procesar_y_guardar_en_supabase(id_partido):
    url_detalle = f"https://espn.com{id_partido}"
    
    try:
        respuesta = requests.get(url_detalle, headers=HEADERS, timeout=15)
        if respuesta.status_code != 200: return
        
        datos = respuesta.json()
        header = datos.get('header', {})
        competitions = header.get('competitions', [{}])[0]
        competitors = competitions.get('competitors', [])
        if len(competitors) < 2: return

        local = competitors[0] if competitors[0].get('homeAway') == 'home' else competitors[1]
        visitante = competitors[1] if competitors[0].get('homeAway') == 'home' else competitors[0]
        
        # Procesar Ganador
        g_local, g_vis = int(local.get('score', 0)), int(visitante.get('score', 0))
        ganador = 'local' if g_local > g_vis else ('visitante' if g_vis > g_local else 'empate')
        hubo_penales = 'shootout' in competitions

        conn = conectar_supabase()
        cursor = conn.cursor()
        
        # Guardar Partido (Sintaxis UPSERT para PostgreSQL)
        cursor.execute('''
            INSERT INTO partidos (id_partido, fecha_partido, liga_nombre, equipo_local_id, equipo_local_nombre, equipo_local_goles, equipo_visitante_id, equipo_visitante_nombre, equipo_visitante_goles, ganador, tanda_penales)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id_partido) DO UPDATE SET
                equipo_local_goles = EXCLUDED.equipo_local_goles,
                equipo_visitante_goles = EXCLUDED.equipo_visitante_goles,
                ganador = EXCLUDED.ganador;
        ''', (id_partido, header.get('date'), header.get('league', {}).get('name'), local.get('team', {}).get('id'), local.get('team', {}).get('name'), g_local, visitante.get('team', {}).get('id'), visitante.get('team', {}).get('name'), g_vis, ganador, hubo_penales))

        # Guardar Jugadores
        for equipo_roster in datos.get('rosters', []):
            id_equipo = equipo_roster.get('team', {}).get('id')
            for j in equipo_roster.get('roster', []):
                id_j = j.get('athlete', {}).get('id')
                id_reg = f"{id_partido}_{id_j}"
                cursor.execute('''
                    INSERT INTO jugadores_partido (id_registro, id_partido, id_equipo, id_jugador, nombre_jugador, posicion, titular)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id_registro) DO NOTHING;
                ''', (id_reg, id_partido, id_equipo, id_j, j.get('athlete', {}).get('displayName'), j.get('athlete', {}).get('position', {}).get('name'), j.get('starter', True)))

        # Guardar Eventos (Goles)
        for detalle in competitions.get('details', []):
            tipo_detalle = detalle.get('type', {}).get('text', '')
            if 'Goal' in tipo_detalle or 'Penalty' in tipo_detalle:
                id_ev = detalle.get('id', f"{id_partido}_{time.time_ns()}")
                minuto = int(''.join(filter(str.isdigit, detalle.get('clock', {}).get('displayValue', '0'))))
                
                cursor.execute('''
                    INSERT INTO eventos_partido (id_evento, id_partido, id_equipo, id_jugador, nombre_jugador, tipo_evento, minuto, periodo)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id_evento) DO NOTHING;
                ''', (id_ev, id_partido, detalle.get('team', {}).get('id'), "0", detalle.get('athletesInvolved', [{}])[0].get('displayName', 'Desconocido'), tipo_detalle, minuto, "REGULAR"))

        conn.commit()
        cursor.close()
        conn.close()
        print(f"-> Sincronizado en la nube: {local.get('team', {}).get('name')} vs {visitante.get('team', {}).get('name')}")
        
    except Exception as e:
        print(f"Error procesando partido {id_partido}: {e}")

def ejecutar_cron_diario():
    iniciar_tablas_supabase()
    partidos = obtener_partidos_dia_anterior()
    print(f"Procesando {len(partidos)} partidos de ayer...")
    for id_p in partidos:
        procesar_y_guardar_en_supabase(id_p)
        time.sleep(2)

if __name__ == "__main__":
    ejecutar_cron_diario()
