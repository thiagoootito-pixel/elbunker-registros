import base64
import calendar
import hashlib
import hmac
import importlib
import os
import re
import secrets
import time
import unicodedata
from datetime import date, datetime
from decimal import Decimal
from html import escape
from io import BytesIO
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import psycopg2
import streamlit as st
import database
import personal_management
from database import (
    COLUMNAS_REGISTRO,
    ITERACIONES_HASH_CONTRASENA,
    USUARIOS_PREDEFINIDOS,
    actualizar_fecha_registros,
    actualizar_registro,
    actualizar_ultimo_inicio_sesion,
    conectar,
    consultar_registros,
    consultar_usuarios,
    consultar_usuarios_eliminados,
    eliminar_registros,
    eliminar_registros_supervisor_en_conexion,
    guardar_usuarios as guardar_usuarios_en_db,
    guardar_usuarios_en_conexion,
    inicializar_base_datos,
    insertar_registro,
    normalizar_usuarios,
)

_database_module_mtime = os.stat(database.__file__).st_mtime_ns
_database_module_reloaded = (
    st.session_state.get("_database_module_mtime")
    != _database_module_mtime
)
if _database_module_reloaded:
    database = importlib.reload(database)
    st.session_state["_database_module_mtime"] = os.stat(
        database.__file__
    ).st_mtime_ns

_personal_management_mtime = os.stat(
    personal_management.__file__
).st_mtime_ns
if st.session_state.get(
    "_personal_management_module_mtime"
) != _personal_management_mtime or _database_module_reloaded:
    personal_management = importlib.reload(personal_management)
    st.session_state[
        "_personal_management_module_mtime"
    ] = os.stat(personal_management.__file__).st_mtime_ns
render_gestion_personal = personal_management.render_gestion_personal


# Configuración de la página
st.set_page_config(
    page_title="El Búnker - Sistema de Autolavado",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(show_spinner=False)
def cargar_imagen_base64(ruta, modificacion_ns, tamano):
    del modificacion_ns, tamano
    with open(ruta, "rb") as archivo:
        return base64.b64encode(archivo.read()).decode("ascii")


def obtener_imagen_base64(nombre_archivo):
    ruta = os.path.join(os.path.dirname(__file__), nombre_archivo)
    informacion = os.stat(ruta)
    return cargar_imagen_base64(
        ruta,
        informacion.st_mtime_ns,
        informacion.st_size,
    )


logo_base64 = obtener_imagen_base64("bunker_logo.png")
imagen_auto_sidebar_base64 = obtener_imagen_base64("banner_bunker.png")
estilo_banner_registros = ""
if (
    st.session_state.get("login_completado")
    and st.session_state.get("menu_activo", "Registros") == "Registros"
):
    banner_base64 = obtener_imagen_base64("teslavolador.png")
    estilo_banner_registros = (
        f'.st-key-registros_hero::before {{'
        f'background-image: url("data:image/png;base64,{banner_base64}");'
        "}"
    )

ROLES_USUARIO = ["Supervisor", "Administrador"]
FECHA_MINIMA_CALENDARIO = date(2026, 1, 1)
FECHA_MAXIMA_CALENDARIO = date(2036, 12, 31)
ZONA_HORARIA_PERU = ZoneInfo("America/Lima")


def construir_titulo_seccion(seccion):
    texto = unicodedata.normalize("NFKD", f"{seccion} - BUNKER")
    return texto.encode("ascii", "ignore").decode("ascii").upper()


def mostrar_titulo_seccion(seccion, descripcion=None):
    descripciones = {
        "Registros": "Registro y seguimiento de los servicios del autolavado.",
        "Gestión": "Administra usuarios, vehículos y servicios.",
        "Control": "Revisa los registros de servicios y gastos.",
        "Finanzas": "Consulta los ingresos, gastos y balances del negocio.",
        "Personal": "Servicios y montos producidos por cada lavador.",
    }
    titulo = construir_titulo_seccion(seccion)
    nombre_seccion = titulo.partition(" - ")[0]
    texto_descripcion = descripcion or descripciones.get(seccion, "")
    st.markdown(
        '<section class="bunker-page-heading">'
        f'<h1>{escape(nombre_seccion)} '
        '<span class="bunker-title-accent">- BUNKER</span></h1>'
        f'<p>{escape(texto_descripcion)}</p>'
        '</section>',
        unsafe_allow_html=True,
    )


def obtener_fecha_selector(clave, fecha_predeterminada):
    fecha = st.session_state.get(clave)
    if isinstance(fecha, datetime):
        fecha = fecha.date()
    if (
        not isinstance(fecha, date)
        or not FECHA_MINIMA_CALENDARIO
        <= fecha
        <= FECHA_MAXIMA_CALENDARIO
    ):
        st.session_state.pop(clave, None)
        fecha = fecha_predeterminada
    return min(max(fecha, FECHA_MINIMA_CALENDARIO), FECHA_MAXIMA_CALENDARIO)


def actualizar_periodo_calendario(
    clave_mes, clave_anio, clave_mes_guardado, clave_anio_guardado
):
    st.session_state[clave_mes_guardado] = st.session_state[clave_mes]
    st.session_state[clave_anio_guardado] = st.session_state[clave_anio]


def actualizar_fecha_calendario(
    clave,
    fecha,
    clave_mes,
    clave_anio,
    clave_mes_guardado,
    clave_anio_guardado,
    on_change,
):
    fecha_anterior = obtener_fecha_selector(clave, fecha)
    st.session_state[clave] = fecha
    st.session_state[clave_mes] = fecha.month
    st.session_state[clave_anio] = fecha.year
    st.session_state[clave_mes_guardado] = fecha.month
    st.session_state[clave_anio_guardado] = fecha.year
    st.session_state[f"{clave}_fecha_calendario_sincronizada"] = fecha
    if fecha != fecha_anterior and on_change is not None:
        on_change()


def seleccionar_fecha(etiqueta, clave, fecha_predeterminada, on_change=None):
    meses = (
        "Enero",
        "Febrero",
        "Marzo",
        "Abril",
        "Mayo",
        "Junio",
        "Julio",
        "Agosto",
        "Septiembre",
        "Octubre",
        "Noviembre",
        "Diciembre",
    )
    fecha_seleccionada = obtener_fecha_selector(clave, fecha_predeterminada)
    clave_mes = f"{clave}_mes_calendario_v2"
    clave_anio = f"{clave}_anio_calendario_v2"
    clave_mes_guardado = f"{clave}_mes_calendario_guardado"
    clave_anio_guardado = f"{clave}_anio_calendario_guardado"
    clave_popover = f"{clave}_popover_calendario"
    clave_fecha_sincronizada = f"{clave}_fecha_calendario_sincronizada"
    if (
        st.session_state.get(clave_fecha_sincronizada)
        != fecha_seleccionada
    ):
        st.session_state[clave_mes_guardado] = fecha_seleccionada.month
        st.session_state[clave_anio_guardado] = fecha_seleccionada.year
        st.session_state[clave_fecha_sincronizada] = fecha_seleccionada
    st.session_state.setdefault(
        clave_mes_guardado, fecha_seleccionada.month
    )
    st.session_state.setdefault(
        clave_anio_guardado, fecha_seleccionada.year
    )
    st.session_state[clave_mes] = st.session_state[clave_mes_guardado]
    st.session_state[clave_anio] = st.session_state[clave_anio_guardado]

    st.markdown(
        f'<div style="margin-bottom:0.25rem;font-size:0.875rem;">'
        f"{escape(etiqueta)}</div>",
        unsafe_allow_html=True,
    )
    with st.popover(
        f":material/calendar_month: {fecha_seleccionada:%d/%m/%Y}",
        key=clave_popover,
        width=340,
    ):
        with st.container(width=300):
            mes_visible = st.session_state[clave_mes]
            anio_visible = st.session_state[clave_anio]
            columnas_semana = st.columns(7, gap="small")
            for columna, dia_semana in zip(
                columnas_semana, ("Lu", "Ma", "Mi", "Ju", "Vi", "Sa", "Do")
            ):
                with columna:
                    st.markdown(
                        f'<div style="text-align:center;color:#9eaaa0;'
                        f'font-size:0.75rem;font-weight:600;">{dia_semana}</div>',
                        unsafe_allow_html=True,
                    )
            semanas = calendar.Calendar(firstweekday=0).monthdayscalendar(
                anio_visible, mes_visible
            )
            for semana in semanas:
                columnas_dias = st.columns(7, gap="small")
                for columna, numero_dia in zip(columnas_dias, semana):
                    with columna:
                        if numero_dia == 0:
                            st.markdown("&nbsp;", unsafe_allow_html=True)
                            continue
                        fecha_dia = date(anio_visible, mes_visible, numero_dia)
                        st.button(
                            str(numero_dia),
                            key=f"{clave}_dia_{fecha_dia.isoformat()}",
                            type=(
                                "primary"
                                if fecha_dia == fecha_seleccionada
                                else "secondary"
                            ),
                            use_container_width=True,
                            on_click=actualizar_fecha_calendario,
                            args=(
                                clave,
                                fecha_dia,
                                clave_mes,
                                clave_anio,
                                clave_mes_guardado,
                                clave_anio_guardado,
                                on_change,
                            ),
                        )

            columnas_navegacion = st.columns(2, gap="small")
            with columnas_navegacion[0]:
                st.selectbox(
                    "Mes",
                    options=range(1, 13),
                    format_func=lambda numero: meses[numero - 1],
                    key=clave_mes,
                    on_change=actualizar_periodo_calendario,
                    args=(
                        clave_mes,
                        clave_anio,
                        clave_mes_guardado,
                        clave_anio_guardado,
                    ),
                    label_visibility="collapsed",
                )
            with columnas_navegacion[1]:
                st.selectbox(
                    "Año",
                    options=range(
                        FECHA_MINIMA_CALENDARIO.year,
                        FECHA_MAXIMA_CALENDARIO.year + 1,
                    ),
                    key=clave_anio,
                    on_change=actualizar_periodo_calendario,
                    args=(
                        clave_mes,
                        clave_anio,
                        clave_mes_guardado,
                        clave_anio_guardado,
                    ),
                    label_visibility="collapsed",
                )

    return fecha_seleccionada


def verificar_contrasena(cuenta, contrasena):
    if not cuenta["salt"] or not cuenta["contrasena_hash"]:
        return False
    hash_ingresado = hashlib.pbkdf2_hmac(
        "sha256",
        contrasena.encode("utf-8"),
        bytes.fromhex(cuenta["salt"]),
        ITERACIONES_HASH_CONTRASENA,
    ).hex()
    return hmac.compare_digest(cuenta["contrasena_hash"], hash_ingresado)


def cargar_usuarios():
    if (
        "_usuarios_sesion" not in st.session_state
        or "_usuarios_eliminados_sesion" not in st.session_state
    ):
        st.session_state["_usuarios_sesion"] = consultar_usuarios()
        st.session_state["_usuarios_eliminados_sesion"] = (
            consultar_usuarios_eliminados()
        )
    return (
        st.session_state["_usuarios_sesion"],
        st.session_state["_usuarios_eliminados_sesion"],
    )


def _actualizar_usuarios_en_sesion(
    usuarios_actualizados, eliminados_actualizados
):
    global usuarios, usuarios_eliminados_definitivamente

    usuarios = {
        nombre: cuenta.copy()
        for nombre, cuenta in usuarios_actualizados.items()
    }
    usuarios_eliminados_definitivamente = set(eliminados_actualizados)
    st.session_state["_usuarios_sesion"] = usuarios
    st.session_state["_usuarios_eliminados_sesion"] = (
        usuarios_eliminados_definitivamente
    )


def guardar_usuarios(
    usuarios_actualizados, usuarios_eliminados_definitivamente=()
):
    usuarios_normalizados = normalizar_usuarios(usuarios_actualizados)
    guardar_usuarios_en_db(
        usuarios_normalizados,
        usuarios_eliminados_definitivamente,
    )
    _actualizar_usuarios_en_sesion(
        usuarios_normalizados, usuarios_eliminados_definitivamente
    )


@st.dialog("Agregar nuevo usuario")
def mostrar_formulario_nuevo_usuario():
    st.markdown("Registra una cuenta con uno de los roles disponibles.")
    with st.form("form_nuevo_usuario", clear_on_submit=True):
        nuevo_nombre = st.text_input("Nombre de usuario")
        nueva_contrasena = st.text_input("Contraseña", type="password")
        confirmar_contrasena = st.text_input(
            "Confirmar contraseña", type="password"
        )
        nuevo_rol = st.selectbox("Rol", ROLES_USUARIO)
        guardar_nuevo_usuario = st.form_submit_button(
            "Guardar y agregar usuario",
            type="primary",
            use_container_width=True,
        )
    if not guardar_nuevo_usuario:
        return

    nombre_limpio = nuevo_nombre.strip()
    if not nombre_limpio:
        st.warning("Escribe un nombre de usuario.")
    elif any(
        nombre_limpio.casefold() == existente.casefold()
        for existente in usuarios
    ):
        st.warning("Ese nombre de usuario ya está registrado.")
    elif any(
        nombre_limpio.casefold() == predefinido.casefold()
        for predefinido in USUARIOS_PREDEFINIDOS
    ):
        st.warning("Ese nombre está reservado y no se puede volver a registrar.")
    elif len(nueva_contrasena) < 8:
        st.warning("La contraseña debe tener al menos 8 caracteres.")
    elif nueva_contrasena != confirmar_contrasena:
        st.warning("Las contraseñas no coinciden.")
    else:
        salt = secrets.token_bytes(16)
        cuenta_nueva = {
            "contrasena": None,
            "contrasena_hash": hashlib.pbkdf2_hmac(
                "sha256",
                nueva_contrasena.encode("utf-8"),
                salt,
                ITERACIONES_HASH_CONTRASENA,
            ).hex(),
            "salt": salt.hex(),
            "rol": nuevo_rol,
            "ultimo_inicio_sesion": None,
            "activo": True,
            "predefinido": False,
        }
        usuarios_actualizados = dict(usuarios)
        usuarios_actualizados[nombre_limpio] = cuenta_nueva
        try:
            guardar_usuarios(
                usuarios_actualizados,
                usuarios_eliminados_definitivamente=(
                    usuarios_eliminados_definitivamente
                ),
            )
        except (psycopg2.Error, ValueError) as error:
            st.error(f"No se pudo guardar el nuevo usuario: {error}")
        else:
            st.session_state["mensaje_gestion_usuarios"] = (
                f"El usuario {nombre_limpio} fue agregado correctamente."
            )
            st.rerun()


@st.dialog("Editar usuario")
def mostrar_formulario_editar_usuario():
    cuentas_activas = [
        nombre for nombre, cuenta in usuarios.items() if cuenta["activo"]
    ]
    if not cuentas_activas:
        st.info("No hay cuentas activas para editar.")
        return

    usuario_seleccionado = st.selectbox(
        "Seleccionar usuario",
        cuentas_activas,
        key="usuario_a_editar",
    )
    cuenta_seleccionada = usuarios[usuario_seleccionado]
    clave_usuario = hashlib.sha256(
        usuario_seleccionado.casefold().encode("utf-8")
    ).hexdigest()[:12]
    rol_actual = cuenta_seleccionada["rol"]
    rol_elegido = st.selectbox(
        "Rol",
        ROLES_USUARIO,
        index=ROLES_USUARIO.index(
            rol_actual if rol_actual in ROLES_USUARIO else "Supervisor"
        ),
        key=f"rol_editar_usuario_{clave_usuario}",
    )

    if rol_elegido == "Supervisor":
        st.text_input(
            "Contraseña actual",
            value="Protegida: almacenada como hash seguro, no disponible en texto plano",
            disabled=True,
            key=f"contrasena_actual_editar_{clave_usuario}",
        )
        nueva_contrasena = st.text_input(
            "Nueva contraseña",
            type="password",
            key=f"nueva_contrasena_editar_{clave_usuario}",
            help="Deja este campo vacío si solo deseas cambiar el rol.",
        )
        confirmar_nueva_contrasena = st.text_input(
            "Confirmar nueva contraseña",
            type="password",
            key=f"confirmar_nueva_contrasena_editar_{clave_usuario}",
        )
        contrasena_admin = st.text_input(
            "Contraseña de Administrador",
            type="password",
            key=f"contrasena_admin_editar_{clave_usuario}",
        )
    else:
        nueva_contrasena = ""
        confirmar_nueva_contrasena = ""
        contrasena_admin = ""

    guardar_cambios = st.button(
        "Guardar cambios",
        type="primary",
        use_container_width=True,
    )
    if not guardar_cambios:
        return

    usuarios_actualizados = {
        nombre: cuenta.copy() for nombre, cuenta in usuarios.items()
    }
    cuenta_actualizada = usuarios_actualizados[usuario_seleccionado]
    cuenta_actualizada["rol"] = rol_elegido

    cambiar_contrasena = bool(nueva_contrasena or confirmar_nueva_contrasena)
    if cambiar_contrasena and rol_elegido == "Supervisor":
        cuenta_admin = usuarios.get(st.session_state.get("usuario_actual"))
        if (
            cuenta_admin is None
            or not cuenta_admin["activo"]
            or cuenta_admin["rol"] != "Administrador"
            or not verificar_contrasena(cuenta_admin, contrasena_admin)
        ):
            st.error("La contraseña de administrador no es válida.")
            return
        if len(nueva_contrasena) < 8:
            st.warning("La nueva contraseña debe tener al menos 8 caracteres.")
            return
        if nueva_contrasena != confirmar_nueva_contrasena:
            st.warning("Las contraseñas nuevas no coinciden.")
            return

        salt = secrets.token_bytes(16)
        cuenta_actualizada["contrasena"] = None
        cuenta_actualizada["salt"] = salt.hex()
        cuenta_actualizada["contrasena_hash"] = hashlib.pbkdf2_hmac(
            "sha256",
            nueva_contrasena.encode("utf-8"),
            salt,
            ITERACIONES_HASH_CONTRASENA,
        ).hex()

    try:
        guardar_usuarios(
            usuarios_actualizados,
            usuarios_eliminados_definitivamente=(
                usuarios_eliminados_definitivamente
            ),
        )
    except (psycopg2.Error, ValueError) as error:
        st.error(f"No se pudo guardar el usuario: {error}")
    else:
        mensaje = f"Los datos de {usuario_seleccionado} fueron actualizados."
        if cambiar_contrasena and rol_elegido == "Supervisor":
            mensaje = (
                f"La contraseña de {usuario_seleccionado} fue cambiada "
                "correctamente."
            )
        st.session_state["mensaje_gestion_usuarios"] = mensaje
        st.rerun()


@st.dialog("Dar de baja una cuenta")
def mostrar_formulario_baja_usuario():
    st.markdown("Selecciona la cuenta que deseas dar de baja y confirma con tu contraseña de administrador.")
    cuentas_eliminables = [
        nombre
        for nombre, cuenta in usuarios.items()
        if cuenta["activo"]
        and cuenta["rol"] != "Administrador"
        and nombre != st.session_state.usuario_actual
    ]
    with st.form("form_baja_usuario", clear_on_submit=True):
        usuario_seleccionado = st.selectbox(
            "Seleccionar usuario",
            cuentas_eliminables,
            index=None,
            placeholder="Selecciona un usuario",
            disabled=not cuentas_eliminables,
        )
        contrasena_admin = st.text_input(
            "Contraseña de administrador",
            type="password",
        )
        confirmar_baja = st.checkbox(
            "Confirmo que deseo dar de baja esta cuenta",
            disabled=not cuentas_eliminables,
        )
        dar_de_baja = st.form_submit_button(
            "Dar de baja esta cuenta",
            type="primary",
            use_container_width=True,
            disabled=not cuentas_eliminables,
        )

    if not dar_de_baja:
        if not cuentas_eliminables:
            st.info("No hay cuentas disponibles para dar de baja.")
        return

    cuenta_admin = usuarios.get(st.session_state.get("usuario_actual"))
    cuenta_objetivo = usuarios.get(usuario_seleccionado)
    if (
        cuenta_admin is None
        or not cuenta_admin["activo"]
        or cuenta_admin["rol"] != "Administrador"
        or not verificar_contrasena(cuenta_admin, contrasena_admin)
    ):
        st.error("La contraseña de administrador no es válida.")
    elif not confirmar_baja:
        st.warning("Confirma que deseas dar de baja esta cuenta.")
    elif (
        cuenta_objetivo is None
        or not cuenta_objetivo["activo"]
        or cuenta_objetivo["rol"] == "Administrador"
        or usuario_seleccionado == st.session_state.usuario_actual
    ):
        st.error("No está permitido dar de baja esta cuenta.")
    else:
        usuarios_actualizados = {
            nombre: cuenta.copy() for nombre, cuenta in usuarios.items()
        }
        usuarios_actualizados[usuario_seleccionado]["activo"] = False
        try:
            guardar_usuarios(
                usuarios_actualizados,
                usuarios_eliminados_definitivamente=(
                    usuarios_eliminados_definitivamente
                ),
            )
        except (psycopg2.Error, ValueError) as error:
            st.error(f"No se pudo dar de baja al usuario: {error}")
        else:
            st.session_state["mensaje_gestion_usuarios"] = (
                f"La cuenta {usuario_seleccionado} fue dada de baja."
            )
            st.rerun()


try:
    usuarios, usuarios_eliminados_definitivamente = cargar_usuarios()
except (psycopg2.Error, ValueError) as error:
    st.error(str(error))
    st.stop()

VERSION_AUTENTICACION = 3

if st.session_state.get("_version_autenticacion") != VERSION_AUTENTICACION:
    st.session_state.login_completado = False
    st.session_state.admin_autorizado = False
    st.session_state.pop("_registros_sesion", None)
    st.session_state.pop("_alcance_registros_sesion", None)
    st.session_state.pop("_gestion_personal_prefetch", None)
    st.session_state.pop("_gestion_personal_prefetch_future", None)
    st.session_state.pop("usuario_actual", None)
    st.session_state.pop("rol_usuario", None)
    st.session_state._version_autenticacion = VERSION_AUTENTICACION
elif "login_completado" not in st.session_state:
    st.session_state.login_completado = False

if not st.session_state.login_completado:
    st.markdown(
        """
        <style>
        body:has(.st-key-login_shell) [data-testid="stHeader"],
        body:has(.st-key-login_shell) [data-testid="stSidebar"] {
            display: none !important;
        }
        body:has(.st-key-login_shell) [data-testid="stMainBlockContainer"] {
            width: 100%;
            max-width: none !important;
            min-height: 100vh;
            padding: 0 !important;
        }
        body:has(.st-key-login_shell) [data-testid="stAppViewContainer"] {
            background: #07111e;
        }
        .st-key-login_shell {
            position: relative;
            isolation: isolate;
            display: flex;
            align-items: center;
            justify-content: center;
            box-sizing: border-box;
            width: 100%;
            min-height: 100vh;
            padding: 1.5rem;
            overflow: hidden;
            background-color: #07111e;
            background-image:
                radial-gradient(ellipse at 70% 10%, rgba(38, 70, 103, 0.2), transparent 42%),
                repeating-linear-gradient(0deg, transparent 0 31px, rgba(139, 164, 192, 0.025) 32px),
                repeating-linear-gradient(90deg, transparent 0 31px, rgba(139, 164, 192, 0.025) 32px);
        }
        .st-key-login_shell::after {
            position: absolute;
            top: 2rem;
            right: 2rem;
            width: 5rem;
            height: 3rem;
            pointer-events: none;
            background: linear-gradient(
                130deg,
                transparent 0 28%,
                #344b63 28% 43%,
                transparent 43% 54%,
                #ff3545 54% 65%,
                transparent 65% 100%
            );
            clip-path: polygon(32% 0, 100% 0, 67% 100%, 0 100%);
            content: "";
            opacity: 0.8;
        }
        .st-key-login_shell > [data-testid="stLayoutWrapper"] {
            width: min(420px, calc(100vw - 3rem));
            max-width: 420px;
            flex: 0 1 420px;
        }
        .st-key-login_card {
            position: relative;
            z-index: 1;
            box-sizing: border-box;
            width: 100%;
            padding: 2rem;
            border: 1px solid rgba(81, 108, 138, 0.46);
            border-top: 3px solid #ff3545;
            border-radius: 12px;
            background: linear-gradient(145deg, #0d1a2a, #091522);
            box-shadow: 0 22px 54px rgba(0, 0, 0, 0.34);
        }
        .st-key-login_card > [data-testid="stVerticalBlock"] {
            gap: 0.75rem;
        }
        .login-brand {
            display: flex;
            justify-content: center;
            margin: 0.25rem 0 1rem;
        }
        .login-brand img {
            display: block;
            width: 190px;
            max-width: 80%;
            height: auto;
        }
        .login-eyebrow {
            margin: 0;
            color: #ff5361;
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.12em;
            text-align: center;
        }
        .login-title {
            margin: 0 0 0.5rem;
            color: #f4f7fb;
            font-size: 1.55rem;
            font-weight: 650;
            text-align: center;
        }
        .st-key-login_card [data-testid="stTextInput"] label {
            color: #b5c4d7;
        }
        .st-key-login_card [data-testid="stTextInput"] input {
            min-height: 2.75rem;
            border-color: #30465f;
            background: #101f31;
            color: #f1f5fb;
            -webkit-text-fill-color: #f1f5fb;
        }
        .st-key-login_card [data-testid="stAlertContainer"][role="alert"] {
            border: 1px solid rgba(248, 113, 113, 0.35);
            background: rgba(120, 40, 50, 0.28);
            color: #ffd6d2;
        }
        .st-key-login_card [data-testid="stAlertContainer"][role="alert"] p,
        .st-key-login_card [data-testid="stAlertContainer"][role="alert"] svg {
            color: #ffd6d2;
            fill: #ffd6d2;
            stroke: #ffd6d2;
        }
        .st-key-login_form button {
            min-height: 2.8rem;
            border: 1px solid #ff3545;
            border-radius: 8px;
            background: linear-gradient(110deg, #e8273b, #ff3545);
            color: #fff;
            font-weight: 700;
        }
        .st-key-login_form button:hover {
            border-color: #ff6671;
            background: #ff4653;
            color: #fff;
        }
        @media (max-width: 480px) {
            .st-key-login_shell {
                padding: 1rem;
            }
            .st-key-login_card {
                padding: 1.5rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.container(key="login_shell"):
        with st.container(key="login_card"):
            st.markdown(
                f'<div class="login-brand"><img src="data:image/png;base64,{logo_base64}" alt="El Búnker"></div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<p class="login-eyebrow">ACCESO AL SISTEMA</p><h1 class="login-title">Iniciar sesión</h1>',
                unsafe_allow_html=True,
            )
            if not usuarios:
                st.warning(
                    "No hay cuentas cargadas en Supabase. Migra los usuarios "
                    "existentes con `python migrar_usuarios_a_supabase.py --apply`."
                )
            with st.form("login_form"):
                usuario_login = st.text_input("Usuario", key="usuario_login")
                contrasena_login = st.text_input(
                    "Contraseña", type="password", key="contrasena_login"
                )
                acceder = st.form_submit_button(
                    "Acceder", type="primary", use_container_width=True
                )
            if acceder:
                nombre_usuario_login = usuario_login.strip()
                cuenta = usuarios.get(nombre_usuario_login)
                if (
                    cuenta
                    and cuenta["activo"]
                    and verificar_contrasena(cuenta, contrasena_login)
                ):
                    ultimo_inicio_sesion = datetime.now(
                        ZONA_HORARIA_PERU
                    ).isoformat(
                        timespec="seconds"
                    )
                    try:
                        actualizado = actualizar_ultimo_inicio_sesion(
                            nombre_usuario_login,
                            ultimo_inicio_sesion,
                        )
                        if not actualizado:
                            raise ValueError(
                                "La cuenta dejó de existir durante el inicio de sesión."
                            )
                    except (psycopg2.Error, ValueError) as error:
                        st.error(f"No se pudo registrar el inicio de sesión: {error}")
                    else:
                        cuenta["ultimo_inicio_sesion"] = ultimo_inicio_sesion
                        st.session_state["_usuarios_sesion"] = usuarios
                        with st.spinner("Verificando acceso..."):
                            time.sleep(0.6)
                        st.session_state.login_completado = True
                        st.session_state.usuario_actual = nombre_usuario_login
                        st.session_state.rol_usuario = cuenta["rol"]
                        st.session_state.admin_autorizado = (
                            cuenta["rol"] == "Administrador"
                        )
                        st.rerun()
                else:
                    st.error("ACCESO DENEGADO: usuario o contraseña incorrectos.")
    st.stop()

if st.session_state.login_completado:
    cuenta_actual = usuarios.get(st.session_state.get("usuario_actual"))
    if not cuenta_actual or not cuenta_actual["activo"]:
        st.session_state.login_completado = False
        st.session_state.admin_autorizado = False
        st.session_state.pop("_registros_sesion", None)
        st.session_state.pop("_alcance_registros_sesion", None)
        st.session_state.pop("_gestion_personal_prefetch", None)
        st.session_state.pop("_gestion_personal_prefetch_future", None)
        st.session_state.pop("_usuarios_sesion", None)
        st.session_state.pop("_usuarios_eliminados_sesion", None)
        st.session_state.pop("usuario_actual", None)
        st.session_state.pop("rol_usuario", None)
        st.rerun()
    st.session_state.rol_usuario = cuenta_actual["rol"]
    st.session_state.admin_autorizado = (
        cuenta_actual["rol"] == "Administrador"
    )

st.markdown(
    f'<style data-style-revision="{os.stat(__file__).st_mtime_ns}">'
    + """
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    html, body, [data-testid="stAppViewContainer"], .stApp {
        font-family: "Inter", "Segoe UI", sans-serif;
        -webkit-font-smoothing: antialiased;
        -moz-osx-font-smoothing: grayscale;
        text-rendering: optimizeLegibility;
        line-height: 1.5;
    }
    h1, h2, h3 {
        font-weight: 700;
        letter-spacing: -0.03em;
        line-height: 1.15;
    }
    p, div, span, label, button, input, select, textarea {
        letter-spacing: 0.01em;
    }
    [data-testid="stMain"] .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
    }
    [data-testid="stAppViewContainer"] > div {
        display: flex;
        flex-direction: column;
        min-height: 0;
    }
    [data-testid="stMain"] {
        flex: 1 1 0% !important;
        height: auto !important;
        min-height: 0 !important;
        overflow-y: auto !important;
    }
    .stTabs [role="tablist"] button,
    .stButton > button,
    [data-testid="stTextInput"] label,
    [data-testid="stSelectbox"] label,
    [data-testid="stNumberInput"] label,
    [data-testid="stDateInput"] label {
        font-size: 0.96rem;
        line-height: 1.4;
    }
    header,
    [data-testid="stHeader"] {
        display: block !important;
        visibility: visible !important;
        position: relative !important;
        inset: auto !important;
        z-index: 10 !important;
        background: transparent !important;
        box-shadow: none !important;
    }
    body:has(#registros-theme-marker) [data-testid="stHeader"] {
        isolation: isolate;
        border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        background: transparent !important;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.16) !important;
    }
    [data-testid="stAppDeployButton"],
    [data-testid="stMainMenu"] {
        display: none !important;
        visibility: hidden !important;
    }
    [data-testid="stSidebarCollapsedControl"] {
        position: fixed !important;
        top: 0.5rem !important;
        left: 0.5rem !important;
        z-index: 10001 !important;
        display: flex !important;
        visibility: visible !important;
        opacity: 1 !important;
        pointer-events: auto !important;
    }
    [data-testid="stSidebarCollapsedControl"] button {
        min-width: 2.5rem !important;
        min-height: 2.5rem !important;
        border: 1px solid rgba(255, 53, 69, 0.5) !important;
        border-radius: 0.65rem !important;
        background: #101f31 !important;
        color: #ff9da5 !important;
        cursor: pointer !important;
    }
    [data-testid="stSidebarCollapsedControl"] button:hover {
        border-color: #ff3545 !important;
        background: #17283c !important;
    }
    .st-key-menu_usuario {
        position: fixed !important;
        top: 0.5rem;
        right: 1rem;
        z-index: 1000001 !important;
        width: 52px;
        pointer-events: auto !important;
    }
    .st-key-menu_usuario button {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 48px;
        min-width: 48px;
        height: 48px;
        padding: 0;
        border: 1px solid rgba(255, 53, 69, 0.5);
        border-radius: 50%;
        background: #101f31;
        color: #ff9da5;
        cursor: pointer !important;
        pointer-events: auto !important;
    }
    .st-key-menu_usuario button:hover {
        border-color: #ff3545;
        background: #17283c;
        color: #fff;
    }
    body:has(
        .st-key-menu_usuario
        [data-testid="stPopoverButton"][aria-expanded="true"]
    )
    [data-testid="stPopoverBody"] {
        z-index: 1000002 !important;
        width: 220px !important;
        padding: 0.85rem;
        border: 1px solid rgba(255, 53, 69, 0.24);
        border-radius: 8px;
        background: #0d1a2a;
        box-shadow: 0 12px 32px rgba(0, 0, 0, 0.4);
    }
    body:has(
        .st-key-menu_usuario
        [data-testid="stPopoverButton"][aria-expanded="true"]
    )
    [data-testid="stPopoverBody"] [data-testid="stVerticalBlock"] {
        gap: 0.5rem;
    }
    .profile-menu-avatar {
        display: flex;
        justify-content: center;
        margin: 0.25rem 0 0.75rem;
        color: #ff5361;
        font-size: 2rem;
        line-height: 1;
    }
    .profile-menu-avatar span {
        font-family: "Material Symbols Rounded";
        font-feature-settings: "liga";
        font-size: 2rem;
    }
    .profile-menu-label {
        margin: 0.35rem 0 0;
        color: #879188;
        font-size: 0.72rem;
        font-weight: 600;
        text-align: center;
        text-transform: uppercase;
    }
    .profile-menu-role {
        margin: 0;
        color: #f1f5ef;
        font-size: 1rem;
        font-weight: 700;
        text-align: center;
    }
    .profile-menu-user {
        margin: 0;
        color: #f1f5ef;
        font-weight: 600;
        text-align: center;
    }
    body:has(#registros-theme-marker) .block-container {
        padding-top: 10px !important;
        padding-bottom: calc(2rem + 20px) !important;
    }
    .sidebar-logo-container {
        display: flex;
        justify-content: center;
        padding: 0.25rem 0 1rem;
    }
    .sidebar-logo-container img {
        display: block;
        width: 88%;
        max-width: 176px;
        height: auto;
    }
    .main-header-logo {
        display: flex;
        justify-content: flex-end;
        margin-bottom: 0.5rem;
    }
    .main-header-logo img {
        display: block;
        width: 112px;
        max-width: 100%;
        height: auto;
    }
    .st-key-registros_hero {
        position: relative;
        isolation: isolate;
        width: 100%;
        min-height: clamp(14rem, 22vw, 19rem);
        margin-top: -6.75rem;
        margin-bottom: 1rem;
        padding: calc(1.25rem + 3rem) 1.25rem 1.25rem;
        box-sizing: border-box;
    }
    .st-key-registros_hero::before {
        content: "";
        position: absolute;
        top: 60px;
        right: -80px;
        left: -80px;
        height: 271px;
        z-index: 0;
        pointer-events: none;
        background-size: cover;
        background-position: center;
        background-repeat: no-repeat;
    }
    .st-key-registros_hero .main-header-logo {
        transform: translateY(10px);
    }
    .st-key-registros_hero > [data-testid="stLayoutWrapper"] {
        position: relative;
        z-index: 1;
    }
    .reportes-kpi-panel {
        box-sizing: border-box;
        width: 100%;
        margin: 0.75rem 0 1rem;
        padding: 1rem;
        border: 1px solid rgba(173, 190, 174, 0.18);
        border-radius: 12px;
        background: rgba(13, 19, 15, 0.72);
        backdrop-filter: blur(14px);
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.035),
            0 12px 32px rgba(0, 0, 0, 0.22);
    }
    .reportes-kpi-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.75rem;
        margin-bottom: 0.85rem;
    }
    .reportes-kpi-heading {
        color: #e6ece5;
        font-size: 0.95rem;
        font-weight: 700;
    }
    .reportes-kpi-periodo {
        flex: 0 0 auto;
        padding: 0.3rem 0.65rem;
        border: 1px solid rgba(163, 230, 53, 0.28);
        border-radius: 999px;
        background: rgba(163, 230, 53, 0.09);
        color: #d9f99d;
        font-size: 0.72rem;
        font-weight: 700;
    }
    .reportes-kpi-grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 0.7rem;
    }
    .personal-resumen-grid {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 0.7rem;
    }
    .personal-bonificacion-card {
        box-sizing: border-box;
        min-height: 11rem;
        padding: 1.2rem;
        border: 1px solid rgba(230, 162, 60, 0.35);
        border-radius: 10px;
        background: linear-gradient(145deg, rgba(56, 43, 20, 0.78), rgba(19, 29, 39, 0.96));
    }
    .personal-bonificacion-etiqueta {
        color: #e8d6ae;
        font-size: 0.9rem;
        font-weight: 650;
    }
    .personal-bonificacion-valor {
        margin-top: 1.15rem;
        color: #ffd700;
        font-size: clamp(1.8rem, 3vw, 2.5rem);
        font-variant-numeric: tabular-nums;
        font-weight: 800;
        letter-spacing: -0.035em;
        line-height: 1.1;
    }
    .personal-bonificacion-pie {
        margin-top: 0.55rem;
        color: #aeb9c5;
        font-size: 0.78rem;
    }
    .stApp:has(#personal-theme-marker) .reportes-kpi-periodo {
        border-color: rgba(34, 197, 94, 0.42) !important;
        background: rgba(34, 197, 94, 0.12) !important;
        color: #86efac !important;
    }
    .stApp .st-key-personal_bonificacion_panel {
        padding: 0.25rem 0.35rem 0.1rem;
        border-color: rgba(34, 197, 94, 0.2);
        background: rgba(9, 19, 31, 0.42);
    }
    .stApp .st-key-personal_bonificacion_chart {
        border-color: rgba(76, 103, 132, 0.62);
        border-radius: 10px;
        background: linear-gradient(145deg, rgba(19, 35, 54, 0.72), rgba(10, 23, 38, 0.72));
    }
    .reportes-kpi-card {
        position: relative;
        min-width: 0;
        box-sizing: border-box;
        overflow: hidden;
        padding: 1rem 0.9rem 0.85rem;
        border: 1px solid rgba(176, 190, 177, 0.15);
        border-radius: 10px;
        background: rgba(24, 31, 26, 0.7);
    }
    .reportes-kpi-acento {
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        height: 3px;
        background: #a3e635;
    }
    .reportes-kpi-card.supervisor .reportes-kpi-acento {
        background: #93c5fd;
    }
    .reportes-kpi-card.gasto .reportes-kpi-acento {
        background: #f87171;
    }
    .reportes-kpi-card.total .reportes-kpi-acento {
        background: #fbbf64;
    }
    .reportes-kpi-card.acumulado .reportes-kpi-acento {
        background: #a3e635;
    }
    .reportes-kpi-card.finanzas-metodo-card {
        --finanzas-metodo-color: #93c5fd;
    }
    .reportes-kpi-card.finanzas-metodo-card .reportes-kpi-acento {
        background: var(--finanzas-metodo-color);
    }
    .reportes-kpi-card.finanzas-metodo-efectivo {
        --finanzas-metodo-color: #27d98b;
    }
    .reportes-kpi-card.finanzas-metodo-yape {
        --finanzas-metodo-color: #b779f7;
    }
    .reportes-kpi-card.finanzas-metodo-plin {
        --finanzas-metodo-color: #35d6e8;
    }
    .reportes-kpi-card.finanzas-metodo-transferencia {
        --finanzas-metodo-color: #ff9b43;
    }
    .reportes-kpi-card.finanzas-metodo-tarjeta {
        --finanzas-metodo-color: #2388ff;
    }
    .reportes-kpi-card.finanzas-metodo-otros {
        --finanzas-metodo-color: #93c5fd;
    }
    .reportes-kpi-etiqueta {
        overflow: hidden;
        color: #aeb9b0;
        font-size: 0.72rem;
        font-weight: 600;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .personal-ingresos-panel .reportes-kpi-etiqueta {
        white-space: normal;
    }
    .reportes-kpi-icono {
        margin-bottom: 0.55rem;
        color: #a3e635;
        font-family: "Material Symbols Rounded";
        font-feature-settings: "liga";
        font-size: 1.2rem;
        line-height: 1;
    }
    .reportes-kpi-card.supervisor .reportes-kpi-icono {
        color: #93c5fd;
    }
    .reportes-kpi-card.gasto .reportes-kpi-icono {
        color: #f87171;
    }
    .reportes-kpi-card.total .reportes-kpi-icono {
        color: #fbbf64;
    }
    .reportes-kpi-card.acumulado .reportes-kpi-icono {
        color: #a3e635;
    }
    .reportes-kpi-valor {
        margin-top: 0.3rem;
        color: #f4f7f2;
        font-size: 2rem;
        font-weight: 750;
        line-height: 1.1;
    }
    .reportes-kpi-unidad {
        margin-top: 0.2rem;
        color: #879188;
        font-size: 0.68rem;
    }
    .finanzas-kpi-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
        gap: 0.7rem;
    }
    .finanzas-balance-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.85rem;
        margin-bottom: 1rem;
    }
    .finanzas-balance-card {
        position: relative;
        min-width: 0;
        box-sizing: border-box;
        overflow: hidden;
        padding: 1rem 1.1rem;
        border: 1px solid rgba(176, 190, 177, 0.15);
        border-radius: 10px;
        background: rgba(24, 31, 26, 0.7);
        backdrop-filter: blur(14px);
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.035),
            0 8px 20px rgba(0, 0, 0, 0.16);
    }
    .finanzas-balance-card::before {
        position: absolute;
        inset: 0 0 auto;
        height: 3px;
        background: #93c5fd;
        content: "";
    }
    .finanzas-balance-card.gasto::before {
        background: #f87171;
    }
    .finanzas-balance-etiqueta {
        color: #aeb9b0;
        font-size: 0.82rem;
        font-weight: 600;
    }
    .finanzas-balance-valor {
        margin-top: 0.35rem;
        color: #f4f7f2;
        font-size: clamp(1.65rem, 3vw, 2.1rem);
        font-weight: 750;
        letter-spacing: -0.03em;
        line-height: 1.15;
    }
    .st-key-fecha_desglose_finanzas {
        max-width: 15rem;
    }
    .st-key-fecha_referencia_balance_finanzas {
        max-width: 15rem;
    }
    @media (max-width: 640px) {
        .st-key-registros_hero {
            padding: 1rem;
            padding-top: calc(1rem + 3rem);
        }
        .reportes-kpi-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .personal-resumen-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .finanzas-kpi-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .finanzas-balance-grid {
            grid-template-columns: 1fr;
        }
        .reportes-kpi-panel {
            padding: 0.75rem;
        }
    }
    @media (min-width: 641px) and (max-width: 1000px) {
        .reportes-kpi-grid {
            grid-template-columns: repeat(3, minmax(0, 1fr));
        }
        .personal-resumen-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
    }
    [data-testid="stTextInput"] label,
    [data-testid="stSelectbox"] label,
    [data-testid="stNumberInput"] label {
        color: #aaa;
        font-weight: 500;
    }
    [data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(128, 139, 150, 0.07);
        border: 1px solid rgba(128, 139, 150, 0.24);
        border-radius: 12px;
    }
    .st-key-ingresar_admin button {
        background-color: #F8D7DA !important;
        border-color: #E9B9BE !important;
        color: #842029 !important;
        -webkit-text-fill-color: #842029 !important;
    }
    .st-key-ingresar_admin button:hover {
        background-color: #F1C2C7 !important;
        border-color: #DFA4AB !important;
    }
    .st-key-abrir_agregar_personal button,
    .st-key-abrir_agregar_tipo_vehiculo button,
    .st-key-abrir_agregar_servicio button,
    .st-key-guardar_registro_btn button {
        background-color: #BFE8CE !important;
        border-color: #95D3AA !important;
        color: #174B2B !important;
        -webkit-text-fill-color: #174B2B !important;
    }
    .st-key-ingresar_admin button:hover {
        background-color: #EEB2AD !important;
        border-color: #D98E87 !important;
    }
    .st-key-quitar_tipo_button button,
    .st-key-quitar_servicio_button button,
    .st-key-dar_baja_personal_button button,
    .st-key-eliminar_registro_btn button {
        background-color: #F4C1BB !important;
        border-color: #E69A91 !important;
        color: #702B25 !important;
        -webkit-text-fill-color: #702B25 !important;
    }
    .st-key-quitar_tipo_button button:hover,
    .st-key-quitar_servicio_button button:hover,
    .st-key-dar_baja_personal_button button:hover,
    .st-key-eliminar_registro_btn button:hover {
        background-color: #EFA8A0 !important;
        border-color: #D98279 !important;
    }
    .st-key-cancelar_edicion_btn button {
        background-color: #DDF2FF !important;
        border-color: #B9DDF2 !important;
        color: #075985 !important;
        -webkit-text-fill-color: #075985 !important;
    }
    .st-key-cancelar_edicion_btn button:hover {
        background-color: #C6E8FA !important;
        border-color: #A4D5EF !important;
    }
    .st-key-abrir_agregar_personal button:hover,
    .st-key-abrir_agregar_tipo_vehiculo button:hover,
    .st-key-abrir_agregar_servicio button:hover,
    .st-key-guardar_registro_btn button:hover {
        background-color: #A8DDBA !important;
        border-color: #7FC693 !important;
    }
    .st-key-cambiar_fecha_seleccionados button {
        background-color: #BFE8CE !important;
        border-color: #95D3AA !important;
        color: #174B2B !important;
        -webkit-text-fill-color: #174B2B !important;
    }
    .st-key-cambiar_fecha_seleccionados button:hover {
        background-color: #A8DDBA !important;
        border-color: #7FC693 !important;
    }
    .st-key-eliminar_registros_seleccionados button {
        background-color: #F4C1BB !important;
        border-color: #E69A91 !important;
        color: #702B25 !important;
        -webkit-text-fill-color: #702B25 !important;
    }
    .st-key-eliminar_registros_seleccionados button:hover {
        background-color: #EFA8A0 !important;
        border-color: #D98279 !important;
    }
    [data-testid="stAlertContainer"] {
        border-radius: 10px !important;
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08),
            0 8px 24px rgba(0, 0, 0, 0.2);
    }
    [data-testid="stAlertContainer"][role="alert"] {
        background-color: rgba(120, 40, 40, 0.4) !important;
        border: 1px solid rgba(248, 113, 113, 0.3) !important;
        color: #FDE8E7 !important;
        -webkit-text-fill-color: #FDE8E7 !important;
    }
    [data-testid="stAlertContainer"][role="alert"] p,
    [data-testid="stAlertContainer"][role="alert"] svg {
        color: #FDE8E7 !important;
        fill: #FDE8E7 !important;
        stroke: #FDE8E7 !important;
    }
    .st-key-notificacion_exito [data-testid="stAlertContainer"] {
        background-color: rgba(30, 80, 50, 0.4) !important;
        border: 1px solid rgba(74, 222, 128, 0.3) !important;
        color: #E3F8EA !important;
        -webkit-text-fill-color: #E3F8EA !important;
    }
    .st-key-notificacion_exito [data-testid="stAlertContainer"] p,
    .st-key-notificacion_exito [data-testid="stAlertContainer"] svg {
        color: #E3F8EA !important;
        fill: #E3F8EA !important;
        stroke: #E3F8EA !important;
    }
    .st-key-notificacion_eliminacion [data-testid="stAlertContainer"] {
        background-color: rgba(120, 40, 40, 0.4) !important;
        border: 1px solid rgba(248, 113, 113, 0.3) !important;
        color: #FDE8E7 !important;
        -webkit-text-fill-color: #FDE8E7 !important;
    }
    .st-key-notificacion_eliminacion [data-testid="stAlertContainer"] p,
    .st-key-notificacion_eliminacion [data-testid="stAlertContainer"] svg {
        color: #FDE8E7 !important;
        fill: #FDE8E7 !important;
        stroke: #FDE8E7 !important;
    }
    [data-testid="stTextInput"] input,
    [data-testid="stNumberInput"] input,
    [data-testid="stDateInput"] input,
    [data-testid="stTimeInput"] input {
        background-color: #fff !important;
        color: #111 !important;
        -webkit-text-fill-color: #111;
    }
    .st-key-input_placa [data-testid="stTextInput"] input,
    .st-key-input_ruc_cliente [data-testid="stTextInput"] input,
    .st-key-input_razon_social_cliente [data-testid="stTextInput"] input,
    .st-key-input_numero_cliente [data-testid="stTextInput"] input,
    .st-key-input_correo_cliente [data-testid="stTextInput"] input {
        padding-left: 2.65rem !important;
        background-position: 0.9rem center !important;
        background-repeat: no-repeat !important;
        background-size: 1.1rem 1.1rem !important;
    }
    .st-key-input_placa [data-testid="stTextInput"] input {
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23B3262D' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3.4 13.4a2 2 0 0 1 0-2.8l7.2-7.2a2 2 0 0 1 1.4-.6H19a2 2 0 0 1 2 2v7a2 2 0 0 1-.4 1.6Z'/%3E%3Ccircle cx='16.5' cy='7.5' r='1.2'/%3E%3C/svg%3E") !important;
    }
    .st-key-input_ruc_cliente [data-testid="stTextInput"] input {
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23B3262D' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='5' width='18' height='14' rx='2'/%3E%3Cpath d='M7 10h4M7 14h2m4-4h4m-4 4h4'/%3E%3C/svg%3E") !important;
    }
    .st-key-input_razon_social_cliente [data-testid="stTextInput"] input {
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23B3262D' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='8' r='4'/%3E%3Cpath d='M5 21v-2a7 7 0 0 1 14 0v2'/%3E%3C/svg%3E") !important;
    }
    .st-key-input_numero_cliente [data-testid="stTextInput"] input {
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23B3262D' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.4 19.4 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7l.5 3a2 2 0 0 1-.6 1.7L7.1 10.3a16 16 0 0 0 6.6 6.6l1.9-1.9a2 2 0 0 1 1.7-.6l3 .5a2 2 0 0 1 1.7 2Z'/%3E%3C/svg%3E") !important;
    }
    .st-key-input_correo_cliente [data-testid="stTextInput"] input {
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23B3262D' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='5' width='18' height='14' rx='2'/%3E%3Cpath d='m3 7 9 6 9-6'/%3E%3C/svg%3E") !important;
    }
    .st-key-input_motivo [data-testid="stSelectbox"] [role="combobox"] {
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23B3262D' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z'/%3E%3Cpath d='M14 2v6h6M8 13h8m-8 4h8'/%3E%3C/svg%3E") !important;
    }
    .st-key-input_tipo [data-testid="stSelectbox"] [role="combobox"],
    .st-key-input_pago [data-testid="stSelectbox"] [role="combobox"],
    .st-key-input_motivo [data-testid="stSelectbox"] [role="combobox"],
    .st-key-input_metodo_gasto [data-testid="stSelectbox"] [role="combobox"] {
        box-sizing: border-box !important;
        padding-left: 2.65rem !important;
        background-position: 0.9rem center !important;
        background-repeat: no-repeat !important;
        background-size: 1.1rem 1.1rem !important;
    }
    .st-key-input_tipo [data-testid="stSelectbox"] [role="combobox"] {
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23B3262D' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M5 11 7 5h10l2 6'/%3E%3Cpath d='M3 11h18l-1.5 8h-15L3 11Z'/%3E%3Ccircle cx='7.5' cy='16' r='1'/%3E%3Ccircle cx='16.5' cy='16' r='1'/%3E%3Cpath d='M7 5 5 3m12 2 2-2'/%3E%3C/svg%3E") !important;
    }
    .st-key-input_pago [data-testid="stSelectbox"] [role="combobox"],
    .st-key-input_metodo_gasto [data-testid="stSelectbox"] [role="combobox"] {
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23B3262D' stroke-width='1.7' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='3' y='5' width='18' height='14' rx='2'/%3E%3Cpath d='M3 10h18M7 15h3'/%3E%3C/svg%3E") !important;
    }
    [data-testid="stSelectbox"] [role="group"],
    [data-testid="stMultiSelect"] [role="group"] {
        background-color: #fff !important;
        color: #111 !important;
        border-radius: 6px;
    }
    [data-testid="stSelectbox"] [role="combobox"],
    [data-testid="stSelectbox"] input,
    [data-testid="stMultiSelect"] [role="combobox"],
    [data-testid="stMultiSelect"] input {
        background-color: #fff !important;
        color: #111 !important;
        -webkit-text-fill-color: #111;
    }
    [data-testid="stSelectbox"] [role="group"] button,
    [data-testid="stMultiSelect"] [role="group"] button {
        background-color: #B3262D !important;
        border-color: #B3262D !important;
        color: #fff !important;
    }
    [data-testid="stSelectbox"] [role="group"] button svg,
    [data-testid="stMultiSelect"] [role="group"] button svg {
        color: #fff !important;
        stroke: #fff !important;
    }
    [data-testid="stSelectbox"] [role="group"] button:hover,
    [data-testid="stMultiSelect"] [role="group"] button:hover {
        background-color: #92222A !important;
        border-color: #92222A !important;
    }
    [data-testid="stTextInput"] input::placeholder,
    [data-testid="stNumberInput"] input::placeholder,
    [data-testid="stDateInput"] input::placeholder,
    [data-testid="stTimeInput"] input::placeholder {
        color: #555 !important;
        -webkit-text-fill-color: #555;
    }
    [data-testid="stPopoverBody"]:has([class*="_mes_calendario"]) {
        z-index: 1000 !important;
        width: min(340px, calc(100vw - 24px)) !important;
        min-width: 0 !important;
        max-height: calc(100vh - 24px) !important;
        overflow-y: auto !important;
        overscroll-behavior: contain;
        box-sizing: border-box !important;
    }
    @media (max-width: 640px) {
        [data-testid="stPopoverBody"]:has([class*="_mes_calendario"])
        [data-testid="stHorizontalBlock"]:not(:has([data-testid="stSelectbox"])) {
            display: grid !important;
            grid-template-columns: repeat(7, minmax(0, 1fr)) !important;
            align-items: stretch !important;
        }
        [data-testid="stPopoverBody"]:has([class*="_mes_calendario"])
        [data-testid="stHorizontalBlock"]:not(:has([data-testid="stSelectbox"]))
        > [data-testid="column"] {
            width: auto !important;
            min-width: 0 !important;
            max-width: none !important;
        }
    }
    [class*="_popover_calendario"] [data-testid="stPopoverButton"] {
        min-height: 40px !important;
        padding: 0.45rem 0.75rem !important;
        border: 1px solid rgba(173, 190, 174, 0.35) !important;
        border-radius: 8px !important;
        background: #151d16 !important;
        color: #f1f5ef !important;
        visibility: visible !important;
    }
    [class*="_popover_calendario"] [data-testid="stPopoverButton"]:hover {
        border-color: #a3e635 !important;
        background: #202b20 !important;
    }
    [data-testid="stPopoverBody"]:has([class*="_dia_"])
    [class*="_dia_"] [data-testid^="stBaseButton-"] {
        box-sizing: border-box !important;
        width: 100% !important;
        min-width: 0 !important;
        height: 32px !important;
        min-height: 32px !important;
        padding: 0 !important;
        font-size: 0.82rem !important;
        line-height: 1 !important;
    }
    [data-testid="stPopoverBody"]:has([class*="_mes_calendario"])
    [data-testid="stSelectbox"] [role="group"],
    [data-testid="stPopoverBody"]:has([class*="_mes_calendario"])
    [data-testid="stSelectbox"] [role="combobox"] {
        background-color: #202922 !important;
        color: #f4f7f2 !important;
        -webkit-text-fill-color: #f4f7f2 !important;
    }
    [data-testid$="VirtualDropdown"]:not(
        :has([role="listbox"][aria-label="Mes"])
    ):not(:has([role="listbox"][aria-label="Año"])) {
        border: 1px solid #d1d5db !important;
        border-radius: 8px !important;
        background-color: #fff !important;
        color: #111 !important;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2) !important;
    }
    [data-testid$="VirtualDropdown"]:not(
        :has([role="listbox"][aria-label="Mes"])
    ):not(:has([role="listbox"][aria-label="Año"]))
    [role="listbox"] {
        background-color: #fff !important;
        color: #111 !important;
    }
    [data-testid$="VirtualDropdown"]:not(
        :has([role="listbox"][aria-label="Mes"])
    ):not(:has([role="listbox"][aria-label="Año"]))
    [role="option"],
    [data-testid$="VirtualDropdown"]:not(
        :has([role="listbox"][aria-label="Mes"])
    ):not(:has([role="listbox"][aria-label="Año"]))
    [role="option"] [data-item-hl] {
        background-color: transparent !important;
        color: #111 !important;
        -webkit-text-fill-color: #111 !important;
    }
    [data-testid$="VirtualDropdown"]:not(
        :has([role="listbox"][aria-label="Mes"])
    ):not(:has([role="listbox"][aria-label="Año"]))
    [role="option"][aria-selected="true"] [data-item-hl],
    [data-testid$="VirtualDropdown"]:not(
        :has([role="listbox"][aria-label="Mes"])
    ):not(:has([role="listbox"][aria-label="Año"]))
    [role="option"][data-focused="true"] [data-item-hl] {
        border-radius: 6px;
        background-color: #e5e7eb !important;
        color: #111 !important;
    }
    .st-key-descargar_registros_excel button,
    [data-testid="stDownloadButton"] button {
        background-color: #198754 !important;
        border-color: #198754 !important;
        color: #fff !important;
    }
    .st-key-descargar_registros_excel button:hover,
    [data-testid="stDownloadButton"] button:hover {
        background-color: #146c43 !important;
        border-color: #146c43 !important;
    }
    body:has(#registros-theme-marker) [data-testid="stDataFrame"] canvas[style*="height: 36px"] {
        filter: sepia(1) saturate(6) hue-rotate(314deg) brightness(2.8);
    }
    body:has(#registros-theme-marker)
    [class*="st-key-tabla_admin_"] [data-testid="stDataFrame"],
    body:has(#registros-theme-marker)
    [class*="st-key-tabla_registros_supervisor"] [data-testid="stDataFrame"] {
        position: relative !important;
    }
    body:has(#registros-theme-marker)
    [class*="st-key-tabla_admin_"] [data-testid="stDataFrame"]::after,
    body:has(#registros-theme-marker)
    [class*="st-key-tabla_registros_supervisor"] [data-testid="stDataFrame"]::after {
        content: "";
        position: absolute;
        inset: 0 0 auto;
        height: 36px;
        z-index: 10;
        pointer-events: auto;
        background: transparent;
    }
    """ + "</style>",
    unsafe_allow_html=True,
)
st.markdown(
    f"""
    <style>
    body:has(#registros-theme-marker) [data-testid="stHeader"] {{
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        right: 0 !important;
        width: auto !important;
        height: 38px !important;
        min-height: 38px !important;
        z-index: 10000 !important;
        overflow: visible !important;
        background: transparent !important;
    }}
    body:has(#registros-theme-marker) .st-key-menu_usuario {{
        top: 3px !important;
        right: 8px !important;
        width: 32px !important;
        z-index: 10001 !important;
    }}
    body:has(#registros-theme-marker) .st-key-menu_usuario button {{
        width: 32px !important;
        min-width: 32px !important;
        max-width: 32px !important;
        height: 32px !important;
        min-height: 32px !important;
        max-height: 32px !important;
        padding: 0 !important;
    }}
    body:has(#registros-theme-marker) [data-testid="stHeader"]::after {{
        content: "";
        position: absolute;
        inset: 0 0 auto;
        height: 38px;
        z-index: 0;
        pointer-events: none;
        border-bottom: 1px solid rgba(255, 255, 255, 0.14);
        background: rgba(15, 20, 25, 0.08);
        backdrop-filter: blur(5px);
        -webkit-backdrop-filter: blur(5px);
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.2);
    }}
    {estilo_banner_registros}
    body:has(#registros-theme-marker) .st-key-registros_hero > [data-testid="stLayoutWrapper"] {{
        transform: translateY(38px);
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

DIRECTORIO_APP = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_LAVADORES = "lavadores.txt"
ARCHIVO_VEHICULOS = "vehiculos.txt"
ARCHIVO_SERVICIOS = "servicios.txt"
COLUMNAS_CLIENTE = [
    "RUC/DNI",
    "Razón Social/Nombre",
    "Número Cliente",
    "Correo Cliente",
]
NOMBRE_COLUMNA_LOCAL = "Local"
USUARIOS_SUPERVISORES = sorted(
    nombre
    for nombre, cuenta in usuarios.items()
    if cuenta["activo"] and cuenta["rol"] == "Supervisor"
)
COLUMNAS_VALIDAS = [
    "ID", "Tipo", "Placa", "Servicio", "Pago", "Monto", "Fecha",
    "Lavador 1", "Lavador 2", "Lavador 3", "Motivo Gasto",
    "Precio Gasto", "Método Gasto",
    *COLUMNAS_CLIENTE, "Registrado por",
]
COLUMNAS_EXPORTACION = [
    ("Registrado por", "Registrado por"),
    ("Tipo", "TIPO"),
    ("Placa", "PLACA"),
    ("Servicio", "SERVICIO"),
    ("Pago", "PAGO"),
    ("Monto", "MONTO"),
    ("Fecha", "FECHA"),
    ("Hora", "HORA"),
    ("Lavador 1", "LAVADOR 1"),
    ("Lavador 2", "LAVADOR 2"),
    ("Lavador 3", "LAVADOR 3"),
    ("Motivo Gasto", "MOTIVO GASTO"),
    ("Precio Gasto", "PRECIO GASTO"),
    ("Método Gasto", "MÉTODO GASTO"),
    ("RUC/DNI", "RUC/DNI CLIENTE"),
    ("Razón Social/Nombre", "RAZÓN SOCIAL/NOMBRE"),
    ("Número Cliente", "NÚMERO CLIENTE"),
    ("Correo Cliente", "CORREO CLIENTE"),
]

def cargar_catalogo(archivo, valores_iniciales):
    if os.path.exists(archivo):
        with open(archivo, "r", encoding="utf-8") as catalogo:
            valores = [linea.strip() for linea in catalogo if linea.strip()]
        return valores
    return valores_iniciales.copy()

def guardar_catalogo(archivo, valores):
    with open(archivo, "w", encoding="utf-8") as catalogo:
        catalogo.write("\n".join(valores) + "\n")

def consultar_dataframe(fecha=None, supervisor=None, mes=None):
    df = _obtener_registros_sesion(copiar=False)
    if fecha is not None and not df.empty:
        fechas = df["Fecha"].map(parsear_fecha)
        df = df.loc[
            fechas.map(
                lambda valor: pd.notna(valor) and valor.date() == fecha
            )
        ]
    if mes is not None and not df.empty:
        fechas = df["Fecha"].map(parsear_fecha)
        df = df.loc[
            fechas.map(
                lambda valor: pd.notna(valor)
                and valor.year == mes.year
                and valor.month == mes.month
            )
        ]
    if supervisor is not None:
        responsables = (
            df["Registrado por"].fillna("").astype(str).str.strip().str.casefold()
        )
        df = df.loc[responsables == str(supervisor).strip().casefold()]
    return df.reindex(columns=COLUMNAS_VALIDAS, fill_value="").copy()


def _obtener_registros_sesion(forzar_recarga=False, copiar=True):
    nombre_usuario = st.session_state.get("usuario_actual")
    cuenta = usuarios.get(nombre_usuario, {})
    rol = cuenta.get("rol", st.session_state.get("rol_usuario", ""))
    alcance = (nombre_usuario, rol)
    if (
        forzar_recarga
        or st.session_state.get("_alcance_registros_sesion") != alcance
        or "_registros_sesion" not in st.session_state
    ):
        supervisor = nombre_usuario if rol == "Supervisor" else None
        filas = consultar_registros(supervisor=supervisor)
        df = pd.DataFrame.from_records(
            filas,
            columns=COLUMNAS_REGISTRO.values(),
        )
        df = df.rename(
            columns={
                nombre_sql: nombre_app
                for nombre_app, nombre_sql in COLUMNAS_REGISTRO.items()
            }
        )
        df_sesion = df.reindex(
            columns=COLUMNAS_VALIDAS, fill_value=""
        )
        st.session_state["_registros_sesion"] = df_sesion
        st.session_state["_version_registros_sesion"] = (
            st.session_state.get("_version_registros_sesion", 0) + 1
        )
        st.session_state["_huella_datos_bonificacion"] = int(
            pd.util.hash_pandas_object(
                df_sesion[
                    [
                        "Fecha",
                        "Tipo",
                        "Monto",
                        "Lavador 1",
                        "Lavador 2",
                        "Lavador 3",
                    ]
                ],
                index=True,
            ).sum()
        )
        st.session_state["_alcance_registros_sesion"] = alcance
    df_sesion = st.session_state["_registros_sesion"]
    return df_sesion.copy() if copiar else df_sesion


def refrescar_registros_sesion():
    if st.session_state.get("login_completado"):
        database.invalidar_cache_resumenes_personal()
        personal_management.invalidar_cache_gestion_personal()
        _obtener_registros_sesion(forzar_recarga=True)


def guardar_eliminacion_supervisor(
    usuarios_actualizados,
    nombre_supervisor,
    eliminados_actualizados,
):
    with conectar() as conexion:
        eliminar_registros_supervisor_en_conexion(
            conexion, nombre_supervisor
        )
        guardar_usuarios_en_conexion(
            conexion,
            usuarios_actualizados,
            eliminados_actualizados,
        )
    _actualizar_usuarios_en_sesion(
        usuarios_actualizados, eliminados_actualizados
    )
    refrescar_registros_sesion()

def limpiar_monto(valor):
    try:
        val_str = str(valor).replace("S/", "").strip()
        if val_str in ["", "-", "nan", "None"]:
            return 0.0
        return float(val_str)
    except:
        return 0.0

def estilo_metodo_pago(valor):
    estilos = {
        "efectivo": "background-color: #DDF3E4; color: #245B35; font-weight: 600; text-align: center;",
        "tarjeta": "background-color: #DCEBFA; color: #1E4E79; font-weight: 600; text-align: center;",
        "yape": "background-color: #EADFF5; color: #593477; font-weight: 600; text-align: center;",
        "yape/plin": "background-color: #EADFF5; color: #593477; font-weight: 600; text-align: center;",
        "plin": "background-color: #D9F1F8; color: #155E75; font-weight: 600; text-align: center;",
        "transferencia": "background-color: #FFE2C2; color: #9A3412; font-weight: 600; text-align: center;",
    }
    return estilos.get(str(valor).strip().casefold())

def estilo_fila_tabla(fila, filas_seleccionadas):
    fila_seleccionada = fila.name in filas_seleccionadas
    estilo_base = "background-color: #FFFFFF; color: #17212E;"
    if fila_seleccionada:
        estilo_base = "background-color: #8F1D2A; color: #FFFFFF;"
    estilos_celdas = [estilo_base] * len(fila)

    if not fila_seleccionada:
        estilo_pago = estilo_metodo_pago(fila["Pago"])
        if estilo_pago:
            estilos_celdas[fila.index.get_loc("Pago")] = estilo_pago
            if "Monto" in fila.index:
                estilos_celdas[fila.index.get_loc("Monto")] = estilo_pago

    if not fila_seleccionada and "Método Gasto" in fila.index:
        estilo_gasto = estilo_metodo_pago(fila["Método Gasto"])
        if estilo_gasto:
            estilos_celdas[fila.index.get_loc("Método Gasto")] = estilo_gasto

    if not fila_seleccionada and "Registrado por" in fila.index:
        rol = str(fila["Registrado por"]).strip()
        if (
            rol == "Administrador"
            or normalizar_responsable(rol) == "Administrador"
        ):
            estilo_rol = (
                "background-color: #DDF3E4; color: #245B35; "
                "font-weight: 600; text-align: center;"
            )
        elif (
            rol.startswith(("Lavador", "Supervisor"))
            or normalizar_responsable(rol) in USUARIOS_SUPERVISORES
        ):
            estilo_rol = (
                "background-color: #FFE2C2; color: #9A3412; "
                "font-weight: 600; text-align: center;"
            )
        else:
            estilo_rol = "color: #667085; text-align: center;"
        estilos_celdas[fila.index.get_loc("Registrado por")] = estilo_rol

    return estilos_celdas

def crear_tabla_estilizada(
    df,
    filas_seleccionadas=None,
    incluir_usuario=False,
):
    columnas_ocultas = ["ID"]
    if not incluir_usuario:
        columnas_ocultas.append("Registrado por")
    df_visual = (
        df.drop(columns=columnas_ocultas, errors="ignore")
        .fillna("-")
        .replace(r"^\s*$", "-", regex=True)
        .astype(str)
        .reset_index(drop=True)
    )
    if incluir_usuario and "Registrado por" in df_visual.columns:
        columnas = ["Registrado por"] + [
            columna for columna in df_visual.columns if columna != "Registrado por"
        ]
        df_visual = df_visual[columnas]
    if "Fecha" in df_visual.columns:
        fechas_separadas = df_visual["Fecha"].map(separar_fecha_hora_registro)
        posicion_hora = df_visual.columns.get_loc("Fecha") + 1
        df_visual["Fecha"] = fechas_separadas.map(lambda valor: valor[0])
        df_visual.insert(
            posicion_hora,
            "Hora",
            fechas_separadas.map(lambda valor: valor[1]),
        )
    filas_seleccionadas = filas_seleccionadas or set()
    return df_visual.style.apply(
        lambda fila: estilo_fila_tabla(fila, filas_seleccionadas), axis=1
    )


def obtener_columnas_visibles_registros(df, incluir_usuario=False):
    columnas = [
        columna
        for columna in df.columns
        if columna not in {"ID", "Registrado por"}
    ]
    if incluir_usuario and "Registrado por" in df.columns:
        columnas.insert(0, "Registrado por")
    if "Fecha" in columnas:
        columnas.insert(columnas.index("Fecha") + 1, "Hora")
    return columnas


def guardar_seleccion_tabla():
    clave_tabla = st.session_state.get("tabla_admin_key_activa", "tabla_admin")
    estado_tabla = st.session_state.get(clave_tabla)
    seleccion = estado_tabla.get("selection", {}) if estado_tabla else {}
    st.session_state[f"filas_{clave_tabla}"] = set(seleccion.get("rows", []))

def parsear_fecha(valor):
    if pd.isna(valor):
        return pd.NaT

    texto = str(valor).strip()
    formatos = [
        "%Y/%m/%d %I:%M:%S %p", "%Y/%m/%d %I:%M %p",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
        "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M %p",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
        "%Y-%m-%d %I:%M:%S %p", "%Y-%m-%d %I:%M %p",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d-%m-%Y %I:%M:%S %p", "%d-%m-%Y %I:%M %p",
        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
    ]
    for formato in formatos:
        try:
            return datetime.strptime(texto, formato)
        except ValueError:
            continue
    return pd.NaT


def separar_fecha_hora_registro(valor):
    texto = str(valor).strip()
    fecha = parsear_fecha(texto)
    if pd.isna(fecha):
        return texto, "-"
    hora = (
        fecha.strftime("%I:%M:%S %p")
        if re.search(r"\d{1,2}:\d{2}", texto)
        else "-"
    )
    return fecha.strftime("%Y-%m-%d"), hora


def filtrar_registros_por_fecha(df, fecha):
    if df.empty:
        return df.copy()

    fechas_normalizadas = df["Fecha"].map(parsear_fecha)
    coincide_fecha = fechas_normalizadas.map(
        lambda valor: pd.notna(valor) and valor.date() == fecha
    )
    return df.loc[coincide_fecha].copy()

def normalizar_responsable(valor):
    if pd.isna(valor):
        return "No disponible"
    texto = str(valor).strip()
    compacto = re.sub(r"[\s_-]+", "", texto).casefold()
    cuenta = next(
        (
            (nombre, detalle)
            for nombre, detalle in usuarios.items()
            if nombre.casefold() == texto.casefold()
        ),
        None,
    )
    if cuenta:
        nombre, detalle = cuenta
        return nombre if detalle["rol"] == "Supervisor" else "Administrador"
    if compacto in {"admin", "administrador"}:
        return "Administrador"
    coincidencia = re.fullmatch(r"supervisor([1-4])", compacto)
    if coincidencia:
        nombre_legacy = f"supervisor{coincidencia.group(1)}"
        if nombre_legacy in usuarios:
            return nombre_legacy
    if re.fullmatch(r"lavador[1-4]", compacto):
        return "Supervisor sin identificar"
    if compacto == "supervisor":
        return "Supervisor sin identificar"
    return texto or "No disponible"


def obtener_responsables_reporte(df):
    responsables = ["Administrador", *USUARIOS_SUPERVISORES]
    eliminados_normalizados = {
        re.sub(r"[\s_-]+", "", nombre).casefold()
        for nombre in usuarios_eliminados_definitivamente
    }
    if df.empty:
        return responsables

    historicos = df["Registrado por"].map(normalizar_responsable)
    for responsable in historicos:
        responsable_normalizado = re.sub(
            r"[\s_-]+", "", responsable
        ).casefold()
        if (
            responsable not in responsables
            and responsable_normalizado not in eliminados_normalizados
            and responsable not in {"No disponible", "Supervisor sin identificar"}
        ):
            responsables.append(responsable)
    if "Supervisor sin identificar" in historicos.values:
        responsables.append("Supervisor sin identificar")
    return responsables


def filtrar_registros_por_mes(df, fecha):
    if df.empty:
        return df.copy()

    fechas_normalizadas = df["Fecha"].map(parsear_fecha)
    coincide_mes = fechas_normalizadas.map(
        lambda valor: pd.notna(valor)
        and valor.year == fecha.year
        and valor.month == fecha.month
    )
    registros_mes = df.loc[coincide_mes].copy()
    registros_mes["_fecha_orden"] = fechas_normalizadas.loc[coincide_mes]
    return (
        registros_mes.sort_values("_fecha_orden", ascending=True)
        .drop(columns="_fecha_orden")
    )


def filtrar_registros_acumulados_mes(df, fecha):
    registros_mes = filtrar_registros_por_mes(df, fecha)
    if registros_mes.empty:
        return registros_mes
    fechas_registros_mes = registros_mes["Fecha"].map(parsear_fecha)
    return registros_mes.loc[
        fechas_registros_mes.map(
            lambda valor: pd.notna(valor) and valor.date() <= fecha
        )
    ].copy()


def filtrar_registros_del_mes(df, ahora=None):
    ahora = ahora or datetime.now(ZONA_HORARIA_PERU)
    if df.empty:
        return df.copy()

    fechas = df["Fecha"].map(parsear_fecha)
    coincide_mes = fechas.map(
        lambda fecha: pd.notna(fecha)
        and fecha.year == ahora.year
        and fecha.month == ahora.month
        and fecha.date() <= ahora.date()
    )
    registros_mes = df.loc[coincide_mes].copy()
    registros_mes["_fecha_orden"] = fechas.loc[coincide_mes]
    return (
        registros_mes.sort_values("_fecha_orden", ascending=True)
        .drop(columns="_fecha_orden")
    )

def calcular_resumen_financiero(df, ahora=None):
    if df.empty:
        return 0.0, 0.0, 0.0, 0.0

    df_metricas = df.copy()
    df_metricas["Fecha_Normalizada"] = df_metricas["Fecha"].map(parsear_fecha)
    df_metricas["Monto_Num"] = df_metricas["Monto"].map(limpiar_monto)
    df_metricas["Precio_Gasto_Num"] = df_metricas["Precio Gasto"].map(limpiar_monto).abs()

    tipo_gasto = df_metricas["Tipo"].fillna("").astype(str).str.contains(
        r"\b(?:gasto|egreso|expense)\b", case=False, regex=True
    )
    df_metricas["Ingreso_Num"] = df_metricas["Monto_Num"].where(~tipo_gasto, 0.0)
    df_metricas["Gasto_Num"] = df_metricas["Precio_Gasto_Num"].where(
        df_metricas["Precio_Gasto_Num"] > 0,
        df_metricas["Monto_Num"].abs().where(tipo_gasto, 0.0),
    )

    ahora = ahora or datetime.now(ZONA_HORARIA_PERU)
    fechas = df_metricas["Fecha_Normalizada"]
    es_hoy = fechas.dt.date == ahora.date()
    es_mes_actual = (fechas.dt.year == ahora.year) & (fechas.dt.month == ahora.month)

    ganancia_dia = df_metricas.loc[es_hoy, "Ingreso_Num"].sum()
    gasto_dia = df_metricas.loc[es_hoy, "Gasto_Num"].sum()
    ganancia_mes = df_metricas.loc[es_mes_actual, "Ingreso_Num"].sum()
    gasto_mes = df_metricas.loc[es_mes_actual, "Gasto_Num"].sum()
    return ganancia_dia, gasto_dia, ganancia_mes, gasto_mes


def normalizar_metodos_financieros(valores):
    metodos = valores.fillna("").astype(str).str.strip()
    metodos = metodos.mask(
        metodos.str.casefold().isin({"", "-", "nan", "none"}),
        "Sin método especificado",
    )
    nombres_metodo = {
        "efectivo": "Efectivo",
        "yape": "Yape",
        "plin": "Plin",
        "tarjeta": "Tarjeta",
        "transferencia": "Transferencia",
        "yape/plin": "Yape/Plin (histórico)",
    }
    return metodos.map(
        lambda metodo: nombres_metodo.get(metodo.casefold(), metodo)
    )


def desglosar_metodos_financieros(df):
    metodos_base = ["Efectivo", "Yape", "Plin", "Tarjeta", "Transferencia"]
    if df.empty:
        return {metodo: 0.0 for metodo in metodos_base}, {
            metodo: 0.0 for metodo in metodos_base
        }

    es_gasto = df["Tipo"].fillna("").astype(str).str.contains(
        r"\b(?:gasto|egreso|expense)\b", case=False, regex=True
    )
    monto_ingreso = df["Monto"].map(limpiar_monto)
    monto_gasto = df["Precio Gasto"].map(limpiar_monto).abs()
    monto_gasto = monto_gasto.where(
        monto_gasto > 0,
        monto_ingreso.abs().where(es_gasto, 0.0),
    )

    ingresos = (
        monto_ingreso.loc[~es_gasto]
        .groupby(normalizar_metodos_financieros(df.loc[~es_gasto, "Pago"]))
        .sum()
        .to_dict()
    )
    filas_con_gasto = monto_gasto > 0
    gastos = (
        monto_gasto.loc[filas_con_gasto]
        .groupby(
            normalizar_metodos_financieros(
                df.loc[filas_con_gasto, "Método Gasto"]
            )
        )
        .sum()
        .to_dict()
    )
    for metodo in metodos_base:
        ingresos.setdefault(metodo, 0.0)
        gastos.setdefault(metodo, 0.0)
    return ingresos, gastos


def preparar_registros_financieros(df):
    preparados = df.copy()
    fechas = pd.to_datetime(
        preparados["Fecha"].map(parsear_fecha), errors="coerce"
    )
    es_gasto = preparados["Tipo"].fillna("").astype(str).str.contains(
        r"\b(?:gasto|egreso|expense)\b", case=False, regex=True
    )
    monto_ingreso = preparados["Monto"].map(limpiar_monto)
    monto_gasto = preparados["Precio Gasto"].map(limpiar_monto).abs()
    monto_gasto = monto_gasto.where(
        monto_gasto > 0,
        monto_ingreso.abs().where(es_gasto, 0.0),
    )
    preparados["_fecha_finanzas"] = fechas
    preparados["_periodo_finanzas"] = fechas.dt.to_period("M")
    preparados["_dia_finanzas"] = fechas.dt.date
    preparados["_es_gasto_finanzas"] = es_gasto
    preparados["_monto_ingreso_finanzas"] = monto_ingreso.where(
        ~es_gasto, 0.0
    )
    preparados["_monto_gasto_finanzas"] = monto_gasto
    preparados["_metodo_ingreso_finanzas"] = normalizar_metodos_financieros(
        preparados["Pago"]
    )
    preparados["_metodo_gasto_finanzas"] = normalizar_metodos_financieros(
        preparados["Método Gasto"]
    )
    preparados["_responsable_finanzas"] = preparados["Registrado por"].map(
        normalizar_responsable
    )
    preparados["_responsable_busqueda_finanzas"] = (
        preparados["Registrado por"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.casefold()
    )
    return preparados


def obtener_registros_financieros_sesion(df):
    version = st.session_state.get("_version_registros_sesion", 0)
    if st.session_state.get("_version_registros_financieros") != version:
        st.session_state["_registros_financieros_sesion"] = (
            preparar_registros_financieros(df)
        )
        st.session_state["_responsables_financieros_sesion"] = (
            obtener_responsables_reporte(df)[1:]
        )
        st.session_state["_version_registros_financieros"] = version
    return (
        st.session_state["_registros_financieros_sesion"],
        st.session_state["_responsables_financieros_sesion"],
    )


def filtrar_registros_financieros(df, fecha, periodo):
    if df.empty:
        return df
    if periodo == "Día":
        coincide_periodo = df["_dia_finanzas"] == fecha
    else:
        coincide_periodo = df["_periodo_finanzas"] == pd.Period(
            year=fecha.year, month=fecha.month, freq="M"
        )
    return df.loc[coincide_periodo]


def desglosar_registros_financieros(df):
    metodos_base = ["Efectivo", "Yape", "Plin", "Tarjeta", "Transferencia"]
    if df.empty:
        return {metodo: 0.0 for metodo in metodos_base}, {
            metodo: 0.0 for metodo in metodos_base
        }

    ingresos = (
        df.loc[~df["_es_gasto_finanzas"]]
        .groupby("_metodo_ingreso_finanzas")["_monto_ingreso_finanzas"]
        .sum()
        .to_dict()
    )
    filas_con_gasto = df["_monto_gasto_finanzas"] > 0
    gastos = (
        df.loc[filas_con_gasto]
        .groupby("_metodo_gasto_finanzas")["_monto_gasto_finanzas"]
        .sum()
        .to_dict()
    )
    for metodo in metodos_base:
        ingresos.setdefault(metodo, 0.0)
        gastos.setdefault(metodo, 0.0)
    return ingresos, gastos


def calcular_ingresos_financieros_por_responsable(
    df, fecha, periodo, responsables
):
    filtrados = filtrar_registros_financieros(df, fecha, periodo)
    ingresos = (
        filtrados.groupby("_responsable_finanzas")[
            "_monto_ingreso_finanzas"
        ]
        .sum()
    )
    return ingresos.reindex(responsables, fill_value=0.0)


def calcular_ingresos_por_responsable(df, fecha, periodo, hasta_fecha=None):
    orden_responsables = obtener_responsables_reporte(df)[1:]
    if df.empty:
        return pd.Series(0.0, index=orden_responsables, dtype=float)

    df_metricas = df.copy()
    df_metricas["Fecha_Normalizada"] = df_metricas["Fecha"].map(parsear_fecha)
    df_metricas["Monto_Num"] = df_metricas["Monto"].map(limpiar_monto)
    tipo_gasto = df_metricas["Tipo"].fillna("").astype(str).str.contains(
        r"\b(?:gasto|egreso|expense)\b", case=False, regex=True
    )
    df_metricas["Ingreso_Num"] = df_metricas["Monto_Num"].where(~tipo_gasto, 0.0)
    df_metricas["Responsable"] = df_metricas["Registrado por"].map(
        normalizar_responsable
    )

    fechas = df_metricas["Fecha_Normalizada"]
    if periodo == "Día":
        coincide_periodo = fechas.dt.date == fecha
    else:
        coincide_periodo = (fechas.dt.year == fecha.year) & (
            fechas.dt.month == fecha.month
        )
        if hasta_fecha is not None:
            coincide_periodo &= fechas.dt.date <= hasta_fecha

    ingresos = df_metricas.loc[coincide_periodo].groupby("Responsable")[
        "Ingreso_Num"
    ].sum()
    return ingresos.reindex(orden_responsables, fill_value=0.0)


def calcular_metricas_lavadores(df, lavadores, lavador_detalle=None):
    claves_lavadores = {
        str(lavador).strip().casefold()
        for lavador in lavadores
        if str(lavador).strip()
    }
    metricas_por_lavador = {
        lavador: {
            "vehiculos": 0,
            "solitarios": 0,
            "compartidos": 0,
            "grupales": 0,
            "monto_solitario": 0.0,
            "monto_compartido": 0.0,
            "monto_grupal": 0.0,
            "monto": 0.0,
        }
        for lavador in claves_lavadores
    }
    detalles = []
    lavador_detalle_normalizado = (
        str(lavador_detalle).strip().casefold()
        if lavador_detalle
        else None
    )
    if df.empty or not claves_lavadores:
        return metricas_por_lavador, pd.DataFrame()

    claves_monto_modalidad = {
        1: ("Solitario", "solitarios", "monto_solitario"),
        2: ("Compartido (2 lavadores)", "compartidos", "monto_compartido"),
        3: ("Grupal (3 lavadores)", "grupales", "monto_grupal"),
    }
    for registro in df.to_dict("records"):
        tipo_registro = registro.get("Tipo", "")
        if pd.notna(tipo_registro) and re.search(
            r"\b(?:gasto|egreso|expense)\b",
            str(tipo_registro).strip(),
            re.IGNORECASE,
        ):
            continue

        participantes = []
        participantes_vistos = set()
        for columna in ("Lavador 1", "Lavador 2", "Lavador 3"):
            valor_nombre = registro.get(columna, "")
            if pd.isna(valor_nombre):
                continue
            nombre = str(valor_nombre).strip()
            nombre_normalizado = nombre.casefold()
            if nombre_normalizado in {"", "-", "nan", "none"}:
                continue
            if nombre_normalizado not in participantes_vistos:
                participantes.append(nombre)
                participantes_vistos.add(nombre_normalizado)

        cantidad_participantes = len(participantes)
        if cantidad_participantes not in claves_monto_modalidad:
            continue

        modalidad, clave_conteo, clave_monto_modalidad = (
            claves_monto_modalidad[cantidad_participantes]
        )
        monto_lavado = limpiar_monto(registro.get("Monto", ""))
        monto_producido = monto_lavado / cantidad_participantes
        for lavador in participantes_vistos & claves_lavadores:
            metricas = metricas_por_lavador[lavador]
            metricas["vehiculos"] += 1
            metricas[clave_conteo] += 1
            metricas["monto"] += monto_producido
            metricas[clave_monto_modalidad] += monto_producido

        if lavador_detalle_normalizado not in participantes_vistos:
            continue

        def obtener_texto_registro(columna):
            valor = registro.get(columna, "")
            return "" if pd.isna(valor) else str(valor)

        fecha_registro = parsear_fecha(registro.get("Fecha", ""))
        detalles.append(
            {
                "Fecha": (
                    fecha_registro.strftime("%d/%m/%Y %I:%M %p")
                    if pd.notna(fecha_registro)
                    else str(registro.get("Fecha", ""))
                ),
                "Placa": obtener_texto_registro("Placa"),
                "Servicio": obtener_texto_registro("Servicio"),
                "Modalidad": modalidad,
                "Participantes": ", ".join(participantes),
                "Monto del lavado": monto_lavado,
                "Monto producido": monto_producido,
            }
        )

    return metricas_por_lavador, pd.DataFrame.from_records(detalles)


def calcular_metricas_lavador(df, lavador):
    lavador_normalizado = str(lavador or "").strip().casefold()
    metricas, detalles = calcular_metricas_lavadores(
        df,
        [lavador],
        lavador_detalle=lavador,
    )
    metricas_lavador = metricas.get(
        lavador_normalizado,
        {
            "vehiculos": 0,
            "solitarios": 0,
            "compartidos": 0,
            "grupales": 0,
            "monto_solitario": 0.0,
            "monto_compartido": 0.0,
            "monto_grupal": 0.0,
            "monto": 0.0,
        },
    )
    return metricas_lavador, detalles


def generar_excel_registros(df):
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    df_exportacion = df.reindex(
        columns=[columna for columna, _ in COLUMNAS_EXPORTACION],
        fill_value="-",
    ).copy()
    df_exportacion["Registrado por"] = df_exportacion[
        "Registrado por"
    ].map(
        lambda valor: (
            "No disponible"
            if pd.isna(valor) or not str(valor).strip() or str(valor).strip() == "-"
            else str(valor).strip()
        )
    )
    fechas_separadas = df_exportacion["Fecha"].map(separar_fecha_hora_registro)
    df_exportacion["Fecha"] = fechas_separadas.map(lambda valor: valor[0])
    df_exportacion["Hora"] = fechas_separadas.map(lambda valor: valor[1])
    df_exportacion.columns = [titulo for _, titulo in COLUMNAS_EXPORTACION]

    for columna in ("MONTO", "PRECIO GASTO"):
        df_exportacion[columna] = df_exportacion[columna].map(
            lambda valor: "-"
            if str(valor).strip().casefold() in {"", "-", "nan", "none"}
            else limpiar_monto(valor)
        )

    archivo = BytesIO()
    with pd.ExcelWriter(archivo, engine="openpyxl") as writer:
        df_exportacion.to_excel(writer, index=False, sheet_name="Registros")
        hoja = writer.sheets["Registros"]
        formato_moneda = '"S/ " #,##0.00;"S/ " -#,##0.00;"S/ " 0.00'
        borde_fino = Side(style="thin", color="FFB7C9D6")
        borde_celda = Border(
            left=borde_fino,
            right=borde_fino,
            top=borde_fino,
            bottom=borde_fino,
        )
        relleno_cabecera = PatternFill(fill_type="solid", fgColor="FF000000")
        fuente_cabecera = Font(color="FFFFFFFF", bold=True)

        for celda in hoja[1]:
            celda.fill = relleno_cabecera
            celda.font = fuente_cabecera
            celda.alignment = Alignment(horizontal="center", vertical="center")

        for fila in hoja.iter_rows():
            for celda in fila:
                celda.border = borde_celda
                if celda.row > 1:
                    celda.alignment = Alignment(vertical="center")

        columnas_moneda = {
            hoja.cell(row=1, column=numero_columna).value: numero_columna
            for numero_columna in range(1, hoja.max_column + 1)
            if hoja.cell(row=1, column=numero_columna).value
            in {"MONTO", "PRECIO GASTO"}
        }
        for numero_columna in columnas_moneda.values():
            for fila in range(2, hoja.max_row + 1):
                celda = hoja.cell(row=fila, column=numero_columna)
                if isinstance(celda.value, (int, float)):
                    celda.number_format = formato_moneda

        for celdas_columna in hoja.columns:
            letra_columna = celdas_columna[0].column_letter
            numero_columna = celdas_columna[0].column
            longitud_maxima = 0
            for celda in celdas_columna:
                if celda.value is None:
                    continue
                if (
                    numero_columna in columnas_moneda.values()
                    and isinstance(celda.value, (int, float))
                ):
                    texto = f"S/ {celda.value:,.2f}"
                else:
                    texto = str(celda.value)
                longitud_maxima = max(longitud_maxima, len(texto))
            hoja.column_dimensions[letra_columna].width = max(longitud_maxima + 2, 12)

        hoja.row_dimensions[1].height = 24
        hoja.freeze_panes = "A2"
        hoja.auto_filter.ref = hoja.dimensions

    return archivo.getvalue()


def validar_campos_gasto(precio_gasto, metodo_gasto):
    precio_ingresado = str(precio_gasto).strip() not in {"", "-"}
    metodo_seleccionado = str(metodo_gasto).strip() not in {"", "-"}
    errores = []
    if precio_ingresado and not metodo_seleccionado:
        errores.append(
            "No se puede registrar un precio de gasto sin seleccionar el método de gasto."
        )
    if metodo_seleccionado and not precio_ingresado:
        errores.append(
            "No se puede registrar un método de gasto sin ingresar el precio del gasto."
        )
    return errores


# --- INICIALIZAR ESQUEMA DE POSTGRESQL UNA VEZ POR SESIÓN ---
if st.session_state.get("_version_esquema_postgresql") != 3:
    try:
        inicializar_base_datos()
    except (OSError, ValueError, psycopg2.Error) as error:
        st.error(f"No se pudieron cargar los registros: {error}")
        st.stop()
    st.session_state["_esquema_postgresql_inicializado"] = True
    st.session_state["_version_esquema_postgresql"] = 3

def notificar_exito(mensaje, tipo="exito", destino=None):
    st.session_state.notificacion_exito = {
        "mensaje": mensaje,
        "tipo": tipo,
        "destino": destino,
        "creado_en": time.monotonic(),
    }

def mostrar_notificacion_exito(destino=None):
    notificacion = st.session_state.get("notificacion_exito")
    if notificacion:
        if isinstance(notificacion, str):
            notificacion = {"mensaje": notificacion, "tipo": "exito"}
        if notificacion.get("destino") != destino:
            return
        if destino is not None:
            if time.monotonic() - notificacion.get("creado_en", 0) >= 10:
                st.session_state.pop("notificacion_exito", None)
                return
        else:
            st.session_state.pop("notificacion_exito", None)
        tipo = notificacion.get("tipo", "exito")
        if tipo not in {"exito", "eliminacion"}:
            tipo = "exito"
        with st.container(key=f"notificacion_{tipo}"):
            st.success(notificacion["mensaje"])

# Catálogos persistentes compartidos por el panel y el formulario
lista_lavadores = cargar_catalogo(
    ARCHIVO_LAVADORES,
    [
        "Ismael",
        "Eymar",
        "Antony",
        "Anderson",
        "Alvaro",
        "Jeferson",
        "Zara",
        "Dylan",
        "Wender",
        "Eduardo",
        "Javier",
        "Eglimar",
        "Gabriel",
        "Jose",
        "Jose elis",
        "Oscar",
        "Renzo",
        "Renzo V.",
        "Sergio",
        "Kelvin",
        "Merwin",
        "William",
        "Pedro",
        "Felix",
        "Marco",
    ],
)
if (
    st.session_state.get("login_completado")
    and st.session_state.get("admin_autorizado")
    and lista_lavadores
):
    try:
        personal_management.precargar_datos_gestion_personal(
            lista_lavadores,
            datetime.now(ZONA_HORARIA_PERU).date(),
        )
    except (psycopg2.Error, ValueError) as error:
        st.warning(
            "No se pudieron precargar los datos de Personal. "
            f"Se consultarán al abrir el módulo: {error}"
        )
lista_tipos = cargar_catalogo(
    ARCHIVO_VEHICULOS, ["Auto", "Moto", "Motaxi", "Camioneta", "Bicicleta"]
)
lista_servicios = cargar_catalogo(
    ARCHIVO_SERVICIOS,
    [
        "Lavado Simple",
        "Lavado Completo",
        "Encerado",
        "Lavado De Salon",
        "L. Basico+Pulido",
        "L. Basico+Motor",
        "L. Basico+Chasis,Motor,Cera",
        "Tratamiento de cerámico",
    ],
)
if not lista_tipos:
    lista_tipos = ["Auto"]
if not lista_servicios:
    lista_servicios = ["Lavado Simple"]


@st.dialog("Agregar elemento")
def mostrar_formulario_catalogo(tipo_catalogo):
    catalogos = {
        "personal": (
            "Nuevo nombre",
            "Registrar Lavador",
            ARCHIVO_LAVADORES,
            lista_lavadores,
            "Lavador",
        ),
        "vehiculo": (
            "Nuevo tipo de vehículo",
            "Añadir tipo de vehículo",
            ARCHIVO_VEHICULOS,
            lista_tipos,
            "Tipo de vehículo",
        ),
        "servicio": (
            "Nuevo servicio",
            "Añadir servicio",
            ARCHIVO_SERVICIOS,
            lista_servicios,
            "Servicio",
        ),
    }
    etiqueta_campo, etiqueta_boton, archivo, elementos, nombre_elemento = (
        catalogos[tipo_catalogo]
    )
    with st.form(f"form_agregar_{tipo_catalogo}", clear_on_submit=True):
        nuevo_elemento = st.text_input(etiqueta_campo)
        guardar_elemento = st.form_submit_button(
            etiqueta_boton,
            type="primary",
            use_container_width=True,
        )
    if not guardar_elemento:
        return

    nombre_limpio = nuevo_elemento.strip()
    if not nombre_limpio:
        st.warning(f"Escribe {nombre_elemento.lower()} para añadir.")
    elif any(
        nombre_limpio.casefold() == existente.casefold()
        for existente in elementos
    ):
        st.warning(f"Este {nombre_elemento.lower()} ya está registrado.")
    else:
        guardar_catalogo(archivo, [*elementos, nombre_limpio])
        notificar_exito(
            f"{nombre_elemento} {nombre_limpio} añadido correctamente.",
            destino=tipo_catalogo,
        )
        st.rerun()


@st.dialog("Confirmar baja de personal")
def mostrar_confirmacion_baja_personal(nombre):
    st.warning(f"¿Confirmas que deseas dar de baja a {nombre}?")
    col_confirmar, col_cancelar = st.columns(2)
    with col_confirmar:
        confirmar = st.button(
            "Sí, dar de baja",
            key="confirmar_baja_personal",
            type="primary",
            use_container_width=True,
        )
    with col_cancelar:
        cancelar = st.button(
            "Cancelar",
            key="cancelar_baja_personal",
            type="secondary",
            use_container_width=True,
        )

    if cancelar:
        st.session_state.pop("gestion_seleccion_activa", None)
        st.rerun()
    if not confirmar:
        return
    if nombre not in lista_lavadores:
        st.error("Este personal ya no está registrado.")
        return

    lista_actualizada = [
        nombre_personal
        for nombre_personal in lista_lavadores
        if nombre_personal != nombre
    ]
    guardar_catalogo(ARCHIVO_LAVADORES, lista_actualizada)
    for clave in ("input_l1", "input_l2", "input_l3"):
        if st.session_state.get(clave) == nombre:
            st.session_state[clave] = "-"
    st.session_state.pop("gestion_seleccion_activa", None)
    notificar_exito(
        f"Lavador {nombre} dado de baja correctamente.",
        "eliminacion",
        destino="personal",
    )
    st.rerun()


lista_pagos = ["Efectivo", "Tarjeta", "Yape", "Plin", "Transferencia"]
OPCIONES_MOTIVO_GASTO = [
    "-",
    "AD. (Adelanto)",
    "Almuerzo",
    "Limpieza",
    "Otros",
]


def es_motivo_adelanto(motivo):
    return str(motivo or "").strip().casefold() in {
        "ad.",
        "ad. (adelanto)",
    }


def formatear_motivo_gasto(motivo, lavador):
    motivo = str(motivo or "").strip()
    if es_motivo_adelanto(motivo):
        lavador = str(lavador or "").strip()
        return f"AD. {lavador.upper()}" if lavador and lavador != "-" else "AD."
    return motivo or "-"


def seleccionar_motivo_sugerencia():
    motivo_seleccionado = str(
        st.session_state.input_motivo_sugerencia or "-"
    ).strip()
    st.session_state.input_motivo = (
        "" if motivo_seleccionado == "-" else motivo_seleccionado
    )


l1_opts = ["-", *lista_lavadores]
l2_opts = ["-", *lista_lavadores]
l3_opts = ["-", *lista_lavadores]

if "edit_id" not in st.session_state: st.session_state.edit_id = None
if "registro_cargado_id" not in st.session_state: st.session_state.registro_cargado_id = None
if "revision_tabla_fecha" not in st.session_state: st.session_state.revision_tabla_fecha = 0
if "input_tipo" not in st.session_state: st.session_state.input_tipo = "Auto"
if "input_placa" not in st.session_state: st.session_state.input_placa = ""
if "input_servicio" not in st.session_state: st.session_state.input_servicio = "Lavado Simple"
if "input_monto" not in st.session_state: st.session_state.input_monto = "16.00"
if "input_pago" not in st.session_state: st.session_state.input_pago = "Efectivo"
if "input_l1" not in st.session_state: st.session_state.input_l1 = "Carlos"
if "input_l2" not in st.session_state: st.session_state.input_l2 = "-"
if "input_l3" not in st.session_state: st.session_state.input_l3 = "-"
if "input_motivo" not in st.session_state: st.session_state.input_motivo = ""
if "motivos_gasto_personalizados" not in st.session_state:
    st.session_state.motivos_gasto_personalizados = []
if "input_lavador_adelanto" not in st.session_state: st.session_state.input_lavador_adelanto = "-"
if "input_precio_gasto" not in st.session_state: st.session_state.input_precio_gasto = "-"
if "input_metodo_gasto" not in st.session_state: st.session_state.input_metodo_gasto = "-"
if "input_ruc_cliente" not in st.session_state: st.session_state.input_ruc_cliente = ""
if "input_razon_social_cliente" not in st.session_state: st.session_state.input_razon_social_cliente = ""
if "input_numero_cliente" not in st.session_state: st.session_state.input_numero_cliente = ""
if "input_correo_cliente" not in st.session_state: st.session_state.input_correo_cliente = ""
if (
    st.session_state.input_pago not in lista_pagos
    and not (
        st.session_state.edit_id
        and st.session_state.input_pago == "Yape/Plin"
    )
):
    st.session_state.input_pago = "Efectivo"

if st.session_state.input_tipo not in lista_tipos:
    st.session_state.input_tipo = lista_tipos[0] if lista_tipos else ""
if st.session_state.input_servicio not in lista_servicios:
    st.session_state.input_servicio = lista_servicios[0] if lista_servicios else ""
if st.session_state.input_l1 not in l1_opts:
    st.session_state.input_l1 = "Carlos" if "Carlos" in lista_lavadores else (lista_lavadores[0] if lista_lavadores else "-")
if st.session_state.input_l2 not in l2_opts:
    st.session_state.input_l2 = "-"
if st.session_state.input_l3 not in l3_opts:
    st.session_state.input_l3 = "-"

def restablecer_campos_formulario():
    st.session_state.input_tipo = lista_tipos[0] if lista_tipos else ""
    st.session_state.input_placa = ""
    st.session_state.input_servicio = lista_servicios[0] if lista_servicios else ""
    st.session_state.input_monto = "16.00"
    st.session_state.input_pago = "Efectivo"
    st.session_state.input_l1 = "Carlos" if "Carlos" in lista_lavadores else (lista_lavadores[0] if lista_lavadores else "-")
    st.session_state.input_l2 = "-"
    st.session_state.input_l3 = "-"
    st.session_state.input_motivo = ""
    st.session_state.input_lavador_adelanto = "-"
    st.session_state.input_precio_gasto = "-"
    st.session_state.input_metodo_gasto = "-"
    st.session_state.input_ruc_cliente = ""
    st.session_state.input_razon_social_cliente = ""
    st.session_state.input_numero_cliente = ""
    st.session_state.input_correo_cliente = ""


if st.session_state.pop("resetear_formulario", False):
    restablecer_campos_formulario()


def limpiar_formulario():
    st.session_state.edit_id = None
    st.session_state.resetear_formulario = True

def cambiar_fecha_tabla():
    reiniciar_seleccion_tabla()


def reiniciar_seleccion_tabla():
    st.session_state.revision_tabla_fecha += 1
    if st.session_state.edit_id:
        st.session_state.edit_id = None
        st.session_state.registro_cargado_id = None
        st.session_state.resetear_formulario = True

def cargar_fila_en_formulario(fila_data):
    registro_id = str(fila_data['ID'])
    if st.session_state.registro_cargado_id == registro_id:
        return

    st.session_state.registro_cargado_id = registro_id
    st.session_state.edit_id = registro_id
    fecha_original = parsear_fecha(fila_data["Fecha"])
    if pd.isna(fecha_original):
        st.session_state.fecha_edicion_registro = datetime.now(
            ZONA_HORARIA_PERU
        ).date()
        st.session_state.fecha_edicion_hora_original = None
    else:
        st.session_state.fecha_edicion_registro = fecha_original.date()
        st.session_state.fecha_edicion_hora_original = fecha_original
    st.session_state.input_tipo = fila_data['Tipo'] if fila_data['Tipo'] in lista_tipos else lista_tipos[0]
    st.session_state.input_placa = str(fila_data['Placa'])
    st.session_state.input_servicio = fila_data['Servicio'] if fila_data['Servicio'] in lista_servicios else lista_servicios[0]
    st.session_state.input_monto = str(fila_data['Monto']).replace("S/", "").strip()
    metodo_pago = str(fila_data['Pago']).strip()
    st.session_state.input_pago = (
        metodo_pago
        if metodo_pago in {*lista_pagos, "Yape/Plin"}
        else "Efectivo"
    )
    st.session_state.input_l1 = fila_data['Lavador 1'] if fila_data['Lavador 1'] in l1_opts else (
        "Carlos" if "Carlos" in lista_lavadores else (lista_lavadores[0] if lista_lavadores else "-")
    )
    st.session_state.input_l2 = fila_data['Lavador 2'] if fila_data['Lavador 2'] in l2_opts else "-"
    st.session_state.input_l3 = fila_data['Lavador 3'] if fila_data['Lavador 3'] in l3_opts else "-"
    motivo_guardado = (
        ""
        if pd.isna(fila_data["Motivo Gasto"])
        or str(fila_data["Motivo Gasto"]).strip() == "-"
        else str(fila_data["Motivo Gasto"]).strip()
    )
    st.session_state.input_lavador_adelanto = "-"
    for lavador in lista_lavadores:
        if motivo_guardado.casefold() == f"ad. {lavador}".casefold():
            motivo_guardado = "AD."
            st.session_state.input_lavador_adelanto = lavador
            break
    st.session_state.input_motivo = motivo_guardado or ""
    precio_gasto = str(fila_data['Precio Gasto']).replace("S/", "").strip()
    st.session_state.input_precio_gasto = "" if precio_gasto.casefold() in {"", "-", "nan", "none"} else precio_gasto
    metodo_gasto = str(fila_data['Método Gasto']).strip()
    st.session_state.input_metodo_gasto = metodo_gasto if metodo_gasto in lista_pagos else "-"
    valor_ruc = str(fila_data['RUC/DNI'])
    st.session_state.input_ruc_cliente = "" if valor_ruc.casefold() in {"", "-", "nan", "none"} else valor_ruc
    razon_social = str(fila_data['Razón Social/Nombre'])
    st.session_state.input_razon_social_cliente = "" if razon_social.casefold() in {"", "-", "nan", "none"} else razon_social
    numero_cliente = str(fila_data['Número Cliente'])
    st.session_state.input_numero_cliente = "" if numero_cliente.casefold() in {"", "-", "nan", "none"} else numero_cliente
    correo_cliente = str(fila_data['Correo Cliente'])
    st.session_state.input_correo_cliente = "" if correo_cliente.casefold() in {"", "-", "nan", "none"} else correo_cliente

# --- NAVEGACIÓN Y VISTAS ---
opciones_navegacion = [
    "Registros",
    "Gestión",
    "Control",
    "Finanzas",
    "Personal",
]
opciones_navegacion_validas = [*opciones_navegacion, "Gestión de Personal"]
claves_navegacion = {
    "Registros": "registros",
    "Gestión": "gestion",
    "Control": "control",
    "Finanzas": "finanzas",
    "Personal": "personal",
}
iconos_navegacion = {
    "Registros": ":material/receipt_long:",
    "Gestión": ":material/settings:",
    "Control": ":material/verified_user:",
    "Finanzas": ":material/account_balance_wallet:",
    "Personal": ":material/groups:",
}
if st.session_state.get("menu_activo") not in opciones_navegacion_validas:
    menu_anterior = st.session_state.get(
        "menu_activo", st.session_state.get("seccion_navegacion")
    )
    menu_anterior = {"Ajustes": "Control", "Caja": "Finanzas"}.get(
        menu_anterior, menu_anterior
    )
    st.session_state.menu_activo = (
        menu_anterior
        if menu_anterior in opciones_navegacion_validas
        else "Registros"
    )


def actualizar_menu_activo(opcion):
    st.session_state.menu_activo = opcion


st.sidebar.markdown(
    f'<div class="sidebar-logo-container"><img src="data:image/png;base64,{logo_base64}" alt="The Bunker Car Wash"></div>',
    unsafe_allow_html=True,
)
st.markdown(
    f"""
    <style>
    [data-testid="stSidebar"]::before {{
        background-image:
            linear-gradient(
                180deg,
                #081421 0%,
                rgba(8, 20, 33, 0.46) 28%,
                rgba(5, 13, 23, 0.42) 100%
            ),
            url("data:image/png;base64,{imagen_auto_sidebar_base64}");
    }}
    </style>
    """,
    unsafe_allow_html=True,
)
st.markdown(
    """
    <style>
    :root {
        color-scheme: dark;
        --bunker-bg: #07111e;
        --bunker-panel: #0c1929;
        --bunker-panel-raised: #101f31;
        --bunker-border: #26394f;
        --bunker-text: #f4f7fb;
        --bunker-muted: #a7b8cd;
        --bunker-red: #ff3545;
        --bunker-red-deep: #c91f31;
    }
    html, body, .stApp, [data-testid="stAppViewContainer"] {
        background-color: var(--bunker-bg) !important;
        color: var(--bunker-text) !important;
    }
    .stApp {
        background-image:
            radial-gradient(ellipse at 80% 0%, rgba(34, 63, 94, 0.17), transparent 42%),
            linear-gradient(145deg, #07111e, #081321 58%, #060e19) !important;
    }
    body:not(:has(#registros-theme-marker)) .main-header-logo {
        display: none !important;
    }
    html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
        max-width: 100%;
        overflow-x: hidden !important;
    }
    .stApp [data-testid="stMain"] {
        color: var(--bunker-text);
    }
    .stApp [data-testid="stMainBlockContainer"] {
        max-width: 1560px;
        padding-right: clamp(1rem, 2.6vw, 2.8rem);
        padding-left: clamp(1rem, 2.6vw, 2.8rem);
    }
    [data-testid="stSidebar"] {
        position: relative;
        isolation: isolate;
        width: 268px !important;
        min-width: 268px !important;
        border-right: 1px solid rgba(96, 122, 151, 0.2);
        background-color: #081421 !important;
        background-attachment: fixed !important;
    }
    [data-testid="stSidebar"][aria-expanded="false"] {
        width: 0 !important;
        min-width: 0 !important;
        flex: 0 0 0 !important;
    }
    [data-testid="stSidebar"]::before {
        position: absolute;
        z-index: 0;
        right: 0;
        bottom: 0;
        left: 0;
        height: 38%;
        min-height: 230px;
        content: "";
        pointer-events: none;
        filter: grayscale(1) brightness(0.62) sepia(0.3) hue-rotate(175deg)
            saturate(0.85);
        background-size: cover;
        background-position: center 54%;
        background-repeat: no-repeat;
    }
    [data-testid="stSidebar"]::after {
        position: absolute;
        z-index: 1;
        right: 0;
        bottom: 0;
        left: 0;
        height: 72px;
        pointer-events: none;
        background: linear-gradient(
            130deg,
            transparent 0 8%,
            #52657a 8% 12%,
            transparent 12% 17%,
            var(--bunker-red) 17% 23%,
            transparent 23% 28%,
            #52657a 28% 32%,
            transparent 32% 100%
        );
        clip-path: polygon(0 100%, 24% 0, 37% 0, 13% 100%, 100% 100%);
        content: "";
        opacity: 0.9;
    }
    [data-testid="stSidebar"] > div:first-child {
        position: relative;
        z-index: 2;
        min-height: 100%;
        background: transparent;
    }
    [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
        gap: 0.22rem;
    }
    .sidebar-logo-container {
        position: relative;
        z-index: 1;
        min-height: 174px;
        align-items: center;
        padding: 0.8rem 0.5rem 1rem;
        border-bottom: 1px solid rgba(96, 122, 151, 0.16);
    }
    .sidebar-logo-container::before {
        position: absolute;
        top: 50%;
        left: 50%;
        width: min(86%, 212px);
        height: 126px;
        border: 1px solid rgba(255, 53, 69, 0.32);
        border-radius: 50%;
        content: "";
        transform: translate(-50%, -50%);
        pointer-events: none;
    }
    .sidebar-logo-container img {
        position: relative;
        z-index: 1;
        width: 94%;
        max-width: 216px;
        filter: drop-shadow(0 5px 12px rgba(0, 0, 0, 0.45));
    }
    [data-testid="stSidebar"] .st-key-navegacion_registros,
    [data-testid="stSidebar"] .st-key-navegacion_gestion,
    [data-testid="stSidebar"] .st-key-navegacion_control,
    [data-testid="stSidebar"] .st-key-navegacion_finanzas,
    [data-testid="stSidebar"] .st-key-navegacion_personal,
    [data-testid="stSidebar"] .st-key-navegacion_gestion_personal {
        position: relative;
        z-index: 2;
        margin: 0;
    }
    [data-testid="stSidebar"] .st-key-navegacion_registros button,
    [data-testid="stSidebar"] .st-key-navegacion_gestion button,
    [data-testid="stSidebar"] .st-key-navegacion_control button,
    [data-testid="stSidebar"] .st-key-navegacion_finanzas button,
    [data-testid="stSidebar"] .st-key-navegacion_personal button,
    [data-testid="stSidebar"] .st-key-navegacion_gestion_personal button {
        min-height: 54px !important;
        border: 1px solid rgba(111, 137, 166, 0.24) !important;
        border-radius: 0 9px 0 9px !important;
        background: rgba(9, 22, 36, 0.9) !important;
        color: #c6d2e3 !important;
        -webkit-text-fill-color: #c6d2e3 !important;
        font-size: 0.96rem !important;
        font-weight: 650 !important;
        justify-content: flex-start !important;
        padding-left: 1.05rem !important;
        clip-path: polygon(0 0, calc(100% - 13px) 0, 100% 13px, 100% 100%, 13px 100%, 0 calc(100% - 13px));
        transition: background-color 160ms ease, border-color 160ms ease,
            color 160ms ease, transform 160ms ease, box-shadow 160ms ease !important;
    }
    [data-testid="stSidebar"] .st-key-navegacion_registros button:hover,
    [data-testid="stSidebar"] .st-key-navegacion_gestion button:hover,
    [data-testid="stSidebar"] .st-key-navegacion_control button:hover,
    [data-testid="stSidebar"] .st-key-navegacion_finanzas button:hover,
    [data-testid="stSidebar"] .st-key-navegacion_personal button:hover,
    [data-testid="stSidebar"] .st-key-navegacion_gestion_personal button:hover {
        transform: translateX(3px);
        border-color: rgba(255, 53, 69, 0.62) !important;
        background: rgba(23, 43, 65, 0.98) !important;
        color: #fff !important;
        -webkit-text-fill-color: #fff !important;
    }
    [data-testid="stSidebar"] .st-key-navegacion_registros [data-testid="stBaseButton-primary"],
    [data-testid="stSidebar"] .st-key-navegacion_gestion [data-testid="stBaseButton-primary"],
    [data-testid="stSidebar"] .st-key-navegacion_control [data-testid="stBaseButton-primary"],
    [data-testid="stSidebar"] .st-key-navegacion_finanzas [data-testid="stBaseButton-primary"],
    [data-testid="stSidebar"] .st-key-navegacion_personal [data-testid="stBaseButton-primary"],
    [data-testid="stSidebar"] .st-key-navegacion_gestion_personal [data-testid="stBaseButton-primary"] {
        border-color: #ff3545 !important;
        background: linear-gradient(110deg, #ef293e, #ff3545) !important;
        color: #fff !important;
        -webkit-text-fill-color: #fff !important;
        box-shadow: 0 6px 20px rgba(255, 53, 69, 0.22);
    }
    [data-testid="stSidebar"] .st-key-navegacion_gestion_personal {
        margin: 0;
    }
    [data-testid="stSidebar"] [data-testid^="stBaseButton-"] [data-testid="stIconMaterial"],
    [data-testid="stSidebar"] [data-testid^="stBaseButton-"] svg {
        color: currentColor !important;
        fill: currentColor !important;
    }
    .bunker-page-heading {
        position: relative;
        isolation: isolate;
        margin: 0.35rem 0 1.35rem;
        padding: 0.15rem 5rem 0.25rem 0;
    }
    .bunker-page-heading::before {
        display: block;
        width: 2.4rem;
        height: 4px;
        margin-bottom: 0.6rem;
        border-radius: 4px;
        background: var(--bunker-red);
        content: "";
        box-shadow: 0 0 14px rgba(255, 53, 69, 0.26);
    }
    .bunker-page-heading::after {
        position: absolute;
        top: -0.6rem;
        right: 0;
        width: 4.25rem;
        height: 2.7rem;
        pointer-events: none;
        background: linear-gradient(
            130deg,
            transparent 0 24%,
            #334a62 24% 40%,
            transparent 40% 52%,
            var(--bunker-red) 52% 62%,
            transparent 62% 100%
        );
        clip-path: polygon(32% 0, 100% 0, 67% 100%, 0 100%);
        content: "";
        opacity: 0.9;
    }
    .bunker-page-heading h1 {
        margin: 0 !important;
        color: var(--bunker-text) !important;
        font-size: clamp(1.8rem, 3vw, 2.65rem) !important;
        font-weight: 800 !important;
        letter-spacing: -0.04em !important;
        line-height: 1.08 !important;
        text-transform: uppercase;
    }
    .bunker-page-heading h1 .bunker-title-accent {
        color: var(--bunker-red);
    }
    .bunker-page-heading p {
        margin: 0.45rem 0 0;
        color: var(--bunker-muted);
        font-size: 0.94rem;
    }
    .stApp h2, .stApp h3, .stApp h4 {
        color: #f0f4fa !important;
        letter-spacing: -0.025em;
    }
    .stApp [data-testid="stCaptionContainer"],
    .stApp [data-testid="stMarkdownContainer"] p,
    .stApp [data-testid="stText"] {
        color: var(--bunker-muted);
    }
    .stApp [data-testid="stVerticalBlockBorderWrapper"] {
        border: 1px solid rgba(77, 102, 131, 0.42) !important;
        border-radius: 13px !important;
        background: linear-gradient(145deg, rgba(15, 30, 48, 0.94), rgba(9, 22, 37, 0.96)) !important;
        box-shadow: inset 0 1px 0 rgba(185, 207, 232, 0.025),
            0 8px 22px rgba(0, 0, 0, 0.12);
    }
    .stApp [data-testid="stExpander"] {
        border: 1px solid var(--bunker-border) !important;
        border-radius: 11px !important;
        background: rgba(12, 25, 41, 0.82) !important;
    }
    .stApp [data-testid="stExpander"] summary,
    .stApp [data-testid="stExpander"] summary span {
        color: #dbe5f2 !important;
    }
    .stApp .reportes-kpi-panel,
    .stApp .finanzas-balance-card {
        position: relative;
        isolation: isolate;
        overflow: hidden;
        border: 1px solid rgba(50, 76, 104, 0.72) !important;
        border-radius: 14px !important;
        background: linear-gradient(145deg, #0c1929, #091522 78%) !important;
        box-shadow: 0 10px 26px rgba(0, 0, 0, 0.16) !important;
    }
    .stApp .reportes-kpi-panel::after {
        position: absolute;
        top: auto;
        right: 0;
        bottom: 0;
        z-index: 0;
        width: 58px;
        height: 30px;
        pointer-events: none;
        background: linear-gradient(
            130deg,
            transparent 0 30%,
            #30445a 30% 43%,
            transparent 43% 54%,
            var(--bunker-red) 54% 64%,
            transparent 64% 100%
        );
        clip-path: polygon(35% 0, 100% 0, 65% 100%, 0 100%);
        content: "";
        opacity: 0.72;
    }
    .stApp .reportes-kpi-header,
    .stApp .reportes-kpi-grid,
    .stApp .personal-resumen-grid,
    .stApp .finanzas-kpi-grid {
        position: relative;
        z-index: 1;
    }
    .stApp .reportes-kpi-heading {
        color: #f0f4fa !important;
        font-size: 1rem;
    }
    .stApp .reportes-kpi-periodo {
        border-color: rgba(255, 53, 69, 0.32) !important;
        background: rgba(255, 53, 69, 0.1) !important;
        color: #ff9da5 !important;
    }
    .stApp .reportes-kpi-card {
        border: 1px solid rgba(76, 103, 132, 0.62) !important;
        border-radius: 10px !important;
        background: linear-gradient(145deg, rgba(19, 35, 54, 0.98), rgba(10, 23, 38, 0.98)) !important;
    }
    .stApp .reportes-kpi-icono {
        display: grid;
        width: 2.4rem;
        height: 2.4rem;
        place-items: center;
        border: 1px solid currentColor;
        border-radius: 50%;
        background: color-mix(in srgb, currentColor 14%, transparent);
        margin-bottom: 0.45rem;
    }
    .stApp .personal-resumen-grid .reportes-kpi-card.total {
        --card-accent: #ffb52e;
        background: linear-gradient(145deg, rgba(42, 37, 22, 0.98), rgba(18, 24, 32, 0.98)) !important;
    }
    .stApp .personal-resumen-grid .reportes-kpi-card.acumulado,
    .stApp .personal-ingresos-panel .finanzas-metodo-efectivo {
        --card-accent: #20d38a;
        background: linear-gradient(145deg, rgba(10, 44, 39, 0.96), rgba(10, 26, 37, 0.98)) !important;
    }
    .stApp .personal-resumen-grid .reportes-kpi-card.compartido,
    .stApp .personal-ingresos-panel .finanzas-metodo-yape {
        --card-accent: #36b8ff;
        background: linear-gradient(145deg, rgba(10, 38, 60, 0.96), rgba(10, 25, 42, 0.98)) !important;
    }
    .stApp .personal-resumen-grid .reportes-kpi-card.grupal,
    .stApp .personal-ingresos-panel .finanzas-metodo-plin {
        --card-accent: #a46bff;
        background: linear-gradient(145deg, rgba(36, 25, 63, 0.96), rgba(15, 24, 44, 0.98)) !important;
    }
    .stApp .personal-ingresos-panel .finanzas-metodo-transferencia {
        --card-accent: #ff3545;
        --finanzas-metodo-color: #ff3545;
        border-color: rgba(255, 53, 69, 0.76) !important;
        background: linear-gradient(145deg, rgba(75, 25, 35, 0.96), rgba(34, 20, 33, 0.98)) !important;
        box-shadow: 0 0 18px rgba(255, 53, 69, 0.1);
    }
    .stApp .personal-resumen-grid .reportes-kpi-card.total .reportes-kpi-acento,
    .stApp .personal-resumen-grid .reportes-kpi-card.acumulado .reportes-kpi-acento,
    .stApp .personal-resumen-grid .reportes-kpi-card.compartido .reportes-kpi-acento,
    .stApp .personal-resumen-grid .reportes-kpi-card.grupal .reportes-kpi-acento,
    .stApp .personal-ingresos-panel .reportes-kpi-card .reportes-kpi-acento {
        background: var(--card-accent) !important;
    }
    .stApp .personal-resumen-grid .reportes-kpi-card.total .reportes-kpi-icono,
    .stApp .personal-resumen-grid .reportes-kpi-card.total .reportes-kpi-acento { color: #ffb52e; }
    .stApp .personal-resumen-grid .reportes-kpi-card.acumulado .reportes-kpi-icono,
    .stApp .personal-ingresos-panel .finanzas-metodo-efectivo .reportes-kpi-icono { color: #20d38a; }
    .stApp .personal-resumen-grid .reportes-kpi-card.compartido .reportes-kpi-icono,
    .stApp .personal-ingresos-panel .finanzas-metodo-yape .reportes-kpi-icono { color: #36b8ff; }
    .stApp .personal-resumen-grid .reportes-kpi-card.grupal .reportes-kpi-icono,
    .stApp .personal-ingresos-panel .finanzas-metodo-plin .reportes-kpi-icono { color: #a46bff; }
    .stApp .personal-ingresos-panel .finanzas-metodo-transferencia .reportes-kpi-icono { color: #ff705f; }
    .stApp .reportes-kpi-etiqueta,
    .stApp .reportes-kpi-unidad,
    .stApp .finanzas-balance-etiqueta { color: #a7b8cd !important; }
    .stApp .reportes-kpi-valor,
    .stApp .finanzas-balance-valor { color: #f7f9fc !important; }
    .stApp:has(#finanzas-theme-marker) .finanzas-balance-grid {
        gap: 1rem;
        margin: 0.85rem 0 1.15rem;
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-balance-card {
        display: flex;
        align-items: center;
        gap: 1rem;
        min-height: 7rem;
        padding: 1.15rem 1.25rem;
        border-color: #28506b !important;
        border-radius: 15px !important;
        background: linear-gradient(120deg, #0d2033, #0a1727 75%) !important;
        box-shadow: inset 0 1px 0 rgba(190, 215, 240, 0.04),
            0 8px 24px rgba(0, 0, 0, 0.14) !important;
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-balance-card::before {
        height: 3px;
        background: #24b9ed;
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-balance-card.gasto {
        border-color: rgba(255, 83, 97, 0.52) !important;
        background: linear-gradient(120deg, #281b2a, #111a2a 78%) !important;
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-balance-card.gasto::before {
        background: #ff5361;
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-balance-card::after {
        position: absolute;
        right: 0;
        bottom: 0;
        width: 62px;
        height: 37px;
        pointer-events: none;
        background: linear-gradient(
            130deg,
            transparent 0 28%,
            #30475e 28% 44%,
            transparent 44% 55%,
            #ff3545 55% 67%,
            transparent 67% 100%
        );
        clip-path: polygon(35% 0, 100% 0, 65% 100%, 0 100%);
        content: "";
        opacity: 0.86;
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-balance-icon {
        display: grid;
        flex: 0 0 3.4rem;
        width: 3.4rem;
        height: 3.4rem;
        place-items: center;
        border: 1px solid rgba(54, 190, 235, 0.68);
        border-radius: 50%;
        background: rgba(21, 155, 199, 0.1);
        color: #73d7f5;
        font-family: "Material Symbols Rounded";
        font-feature-settings: "liga";
        font-size: 1.65rem;
        line-height: 1;
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-balance-card.gasto .finanzas-balance-icon {
        border-color: rgba(255, 83, 97, 0.62);
        background: rgba(255, 53, 69, 0.1);
        color: #ff7882;
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-balance-content {
        position: relative;
        z-index: 1;
        min-width: 0;
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-balance-etiqueta {
        color: #b8cbe0 !important;
        font-size: 0.9rem;
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-balance-valor {
        margin-top: 0.12rem;
        font-size: clamp(1.8rem, 3vw, 2.3rem);
    }
    .stApp:has(#finanzas-theme-marker) [data-testid="stExpander"] {
        margin: 0.8rem 0 1rem;
        overflow: hidden;
        border: 1px solid #254865 !important;
        border-radius: 13px !important;
        background: linear-gradient(145deg, #0b1b2d, #081522) !important;
    }
    .stApp:has(#finanzas-theme-marker) [data-testid="stExpander"] details {
        border: 0 !important;
        background: transparent !important;
    }
    .stApp:has(#finanzas-theme-marker) [data-testid="stExpander"] summary {
        min-height: 3.1rem;
        padding: 0.15rem 1rem !important;
        border-bottom: 1px solid rgba(57, 91, 122, 0.42);
        background: linear-gradient(100deg, #10263c, #0c1d30) !important;
        color: #e7f0fa !important;
        transition: background-color 150ms ease, border-color 150ms ease;
    }
    .stApp:has(#finanzas-theme-marker) [data-testid="stExpander"] summary:hover {
        border-bottom-color: rgba(42, 183, 231, 0.55);
        background: linear-gradient(100deg, #15304a, #10243a) !important;
    }
    .stApp:has(#finanzas-theme-marker) [data-testid="stExpander"] summary [data-testid="stIconMaterial"] {
        color: #6bcdf0 !important;
    }
    .stApp:has(#finanzas-theme-marker) [data-testid="stExpanderDetails"] {
        padding: 0.6rem 0.8rem 0.8rem;
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel .finanzas-kpi-grid,
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel .finanzas-kpi-grid {
        grid-template-columns: repeat(5, minmax(0, 1fr));
        align-items: stretch;
        gap: 0.8rem;
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel,
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel {
        padding: 0.7rem;
        border: 1px solid rgba(80, 105, 132, 0.62) !important;
        border-radius: 16px !important;
        background: #07111e !important;
        box-shadow: inset 0 1px 0 rgba(180, 205, 230, 0.025),
            0 8px 20px rgba(0, 0, 0, 0.16) !important;
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel {
        border-color: rgba(53, 136, 171, 0.55) !important;
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel {
        border-color: rgba(105, 69, 83, 0.62) !important;
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel .reportes-kpi-header,
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel .reportes-kpi-header {
        margin: 0 0 0.75rem;
        padding: 0.55rem 0.7rem;
        border: 1px solid rgba(74, 101, 130, 0.38);
        border-radius: 8px;
        background: linear-gradient(100deg, #102238, #0a1929);
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel .finanzas-kpi-grid .finanzas-metodo-card,
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel .finanzas-kpi-grid .finanzas-metodo-card {
        display: flex;
        flex-direction: column;
        align-items: flex-start;
        min-width: 0;
        min-height: 10rem;
        padding: 0.9rem 1rem 0.8rem;
        border: 1px solid color-mix(
            in srgb,
            var(--finanzas-metodo-color) 58%,
            #0d1b2b
        ) !important;
        border-radius: 11px !important;
        background: linear-gradient(145deg, #102238, #0d1b2b 78%) !important;
        box-shadow: inset 0 1px 0 rgba(196, 217, 239, 0.035);
        transition: transform 200ms ease, border-color 200ms ease,
            box-shadow 200ms ease, background-color 200ms ease;
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel .finanzas-kpi-grid .finanzas-metodo-card .reportes-kpi-icono,
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel .finanzas-kpi-grid .finanzas-metodo-card .reportes-kpi-icono {
        display: grid;
        flex: 0 0 2.65rem;
        width: 2.65rem;
        height: 2.65rem;
        place-items: center;
        border: 1px solid var(--finanzas-metodo-color);
        border-radius: 50%;
        background: color-mix(in srgb, var(--finanzas-metodo-color) 11%, #0d1b2b);
        margin: 0 0 0.55rem;
        color: var(--finanzas-metodo-color) !important;
        font-size: 1.2rem;
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel .finanzas-kpi-grid .finanzas-metodo-card .reportes-kpi-acento,
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel .finanzas-kpi-grid .finanzas-metodo-card .reportes-kpi-acento {
        z-index: 1;
        height: 4px;
        background: var(--finanzas-metodo-color) !important;
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel .finanzas-kpi-grid .finanzas-metodo-card .reportes-kpi-etiqueta,
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel .finanzas-kpi-grid .finanzas-metodo-card .reportes-kpi-etiqueta {
        max-width: 100%;
        margin-bottom: 0.2rem;
        color: #c5d2e1 !important;
        font-size: 0.84rem;
        font-weight: 600;
        white-space: normal;
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel .finanzas-kpi-grid .finanzas-metodo-card .reportes-kpi-valor,
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel .finanzas-kpi-grid .finanzas-metodo-card .reportes-kpi-valor {
        max-width: 100%;
        margin: 0.1rem 0 0.25rem;
        color: #f7f9fc !important;
        font-size: clamp(1.35rem, 2.15vw, 1.9rem);
        font-variant-numeric: tabular-nums;
        font-weight: 800;
        letter-spacing: -0.035em;
        line-height: 1.16;
        overflow-wrap: anywhere;
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel .finanzas-kpi-grid .finanzas-metodo-card .reportes-kpi-unidad,
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel .finanzas-kpi-grid .finanzas-metodo-card .reportes-kpi-unidad {
        margin-top: auto;
        color: #9aadc3 !important;
        font-size: 0.72rem;
        line-height: 1.25;
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel .finanzas-kpi-grid .finanzas-metodo-card:hover,
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel .finanzas-kpi-grid .finanzas-metodo-card:hover,
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel .finanzas-kpi-grid .finanzas-metodo-card:focus-visible,
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel .finanzas-kpi-grid .finanzas-metodo-card:focus-visible {
        border-color: var(--finanzas-metodo-color) !important;
        box-shadow: 0 5px 14px color-mix(
            in srgb,
            var(--finanzas-metodo-color) 16%,
            transparent
        ), inset 0 1px 0 rgba(196, 217, 239, 0.05);
        outline: none;
        transform: translateY(-2px);
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-income-panel .reportes-kpi-periodo {
        border-color: rgba(54, 190, 235, 0.42) !important;
        background: rgba(21, 155, 199, 0.12) !important;
        color: #73d7f5 !important;
    }
    .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel::after {
        opacity: 0.94;
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-expense-panel .reportes-kpi-heading {
        color: #ffe6e8 !important;
    }
    .stApp:has(#finanzas-theme-marker) .finanzas-expense-panel .reportes-kpi-periodo {
        border-color: rgba(255, 83, 97, 0.38) !important;
        background: rgba(255, 53, 69, 0.12) !important;
        color: #ff9da5 !important;
    }
    .stApp:has(#finanzas-theme-marker) [data-testid="stMain"] h3 {
        position: relative;
        margin-top: 1.35rem;
        padding-top: 0.4rem;
        color: #f3f7fc !important;
    }
    .stApp:has(#finanzas-theme-marker) [data-testid="stMain"] h3::before {
        display: block;
        width: 2rem;
        height: 3px;
        margin-bottom: 0.45rem;
        border-radius: 3px;
        background: #25b9eb;
        content: "";
    }
    .stApp:has(#finanzas-theme-marker) [data-testid="stMain"] h3:has(+ [data-testid="stElementContainer"] .st-key-fecha_referencia_gastos_finanzas)::before {
        background: #ff5361;
    }
    .stApp:has(#finanzas-theme-marker) [data-testid="stMain"] #balance-de-gastos::before {
        background: #ff5361;
    }
    .stApp [data-testid="stTextInput"] label,
    .stApp [data-testid="stSelectbox"] label,
    .stApp [data-testid="stNumberInput"] label,
    .stApp [data-testid="stDateInput"] label,
    .stApp [data-testid="stTimeInput"] label,
    .stApp [data-testid="stMultiSelect"] label {
        color: #b5c4d7 !important;
        font-weight: 600 !important;
    }
    .stApp [data-testid="stTextInput"] input,
    .stApp [data-testid="stNumberInput"] input,
    .stApp [data-testid="stDateInput"] input,
    .stApp [data-testid="stTimeInput"] input,
    .stApp [data-testid="stSelectbox"] [role="group"],
    .stApp [data-testid="stSelectbox"] [role="combobox"],
    .stApp [data-testid="stMultiSelect"] [role="group"],
    .stApp [data-testid="stMultiSelect"] [role="combobox"] {
        border: 1px solid #30465f !important;
        border-radius: 7px !important;
        background-color: #101f31 !important;
        color: #f1f5fb !important;
        -webkit-text-fill-color: #f1f5fb !important;
        box-shadow: none !important;
    }
    .stApp [data-testid="stTextInput"] input::placeholder,
    .stApp [data-testid="stNumberInput"] input::placeholder {
        color: #8194ab !important;
        -webkit-text-fill-color: #8194ab !important;
    }
    .stApp [data-testid="stTextInput"] input:focus,
    .stApp [data-testid="stNumberInput"] input:focus,
    .stApp [data-testid="stDateInput"] input:focus,
    .stApp [data-testid="stSelectbox"] [role="combobox"]:focus,
    .stApp [data-testid="stMultiSelect"] [role="combobox"]:focus {
        border-color: #ff5361 !important;
        box-shadow: 0 0 0 2px rgba(255, 53, 69, 0.16) !important;
    }
    .stApp .st-key-input_placa [data-testid="stTextInput"] input,
    .stApp .st-key-input_ruc_cliente [data-testid="stTextInput"] input,
    .stApp .st-key-input_razon_social_cliente [data-testid="stTextInput"] input,
    .stApp .st-key-input_numero_cliente [data-testid="stTextInput"] input,
    .stApp .st-key-input_correo_cliente [data-testid="stTextInput"] input {
        background-color: #101f31 !important;
        color: #f1f5fb !important;
        -webkit-text-fill-color: #f1f5fb !important;
    }
    .stApp .st-key-input_tipo [data-testid="stSelectbox"] [role="combobox"],
    .stApp .st-key-input_pago [data-testid="stSelectbox"] [role="combobox"],
    .stApp .st-key-input_motivo [data-testid="stSelectbox"] [role="combobox"],
    .stApp .st-key-input_metodo_gasto [data-testid="stSelectbox"] [role="combobox"] {
        background-color: #101f31 !important;
        color: #f1f5fb !important;
        -webkit-text-fill-color: #f1f5fb !important;
    }
    .stApp [data-testid="stSelectbox"] [role="group"] button,
    .stApp [data-testid="stDateInput"] button {
        border-color: #a92132 !important;
        background: #c92337 !important;
        color: #fff !important;
    }
    .stApp [data-testid$="VirtualDropdown"] {
        border: 1px solid #30465f !important;
        border-radius: 8px !important;
        background: #101f31 !important;
        color: #f1f5fb !important;
    }
    .stApp [data-testid$="VirtualDropdown"] [role="option"],
    .stApp [data-testid$="VirtualDropdown"] [role="option"] [data-item-hl] {
        background: transparent !important;
        color: #e4edf8 !important;
        -webkit-text-fill-color: #e4edf8 !important;
    }
    .stApp [data-testid$="VirtualDropdown"] [role="option"][aria-selected="true"],
    .stApp [data-testid$="VirtualDropdown"] [role="option"][data-focused="true"] {
        background: rgba(255, 53, 69, 0.18) !important;
    }
    .stApp [data-testid$="VirtualDropdown"]:not(
        :has([role="listbox"][aria-label="Mes"])
    ):not(:has([role="listbox"][aria-label="Año"])),
    .stApp [data-testid$="VirtualDropdown"]:not(
        :has([role="listbox"][aria-label="Mes"])
    ):not(:has([role="listbox"][aria-label="Año"])) [role="listbox"] {
        border-color: #30465f !important;
        background: #101f31 !important;
        color: #f1f5fb !important;
    }
    .stApp [data-testid$="VirtualDropdown"]:not(
        :has([role="listbox"][aria-label="Mes"])
    ):not(:has([role="listbox"][aria-label="Año"]))
    [role="option"],
    .stApp [data-testid$="VirtualDropdown"]:not(
        :has([role="listbox"][aria-label="Mes"])
    ):not(:has([role="listbox"][aria-label="Año"]))
    [role="option"] [data-item-hl] {
        background: transparent !important;
        color: #e4edf8 !important;
        -webkit-text-fill-color: #e4edf8 !important;
    }
    .stApp [class*="_popover_calendario"] [data-testid="stPopoverButton"] {
        border-color: #344a62 !important;
        background: #101f31 !important;
        color: #e4edf8 !important;
    }
    .stApp [class*="_popover_calendario"] [data-testid="stPopoverButton"]:hover {
        border-color: #ff5361 !important;
        background: #17283c !important;
    }
    .stApp [data-testid="stPopoverBody"]:has([class*="_mes_calendario"])
    [data-testid="stSelectbox"] [role="group"],
    .stApp [data-testid="stPopoverBody"]:has([class*="_mes_calendario"])
    [data-testid="stSelectbox"] [role="combobox"] {
        background-color: #101f31 !important;
        color: #f1f5fb !important;
        -webkit-text-fill-color: #f1f5fb !important;
    }
    .stApp [data-testid="stBaseButton-primary"],
    .stApp [data-testid="stFormSubmitButton"] button {
        border: 1px solid #ff3545 !important;
        border-radius: 8px !important;
        background: linear-gradient(110deg, #e8273b, #ff3545) !important;
        color: #fff !important;
        -webkit-text-fill-color: #fff !important;
        font-weight: 700 !important;
        transition: transform 150ms ease, box-shadow 150ms ease,
            filter 150ms ease !important;
    }
    .stApp [data-testid="stBaseButton-primary"]:hover,
    .stApp [data-testid="stFormSubmitButton"] button:hover {
        transform: translateY(-1px);
        filter: brightness(1.08);
        box-shadow: 0 6px 18px rgba(255, 53, 69, 0.18);
    }
    .stApp [data-testid="stBaseButton-secondary"] {
        border: 1px solid #344a62 !important;
        border-radius: 8px !important;
        background: #112137 !important;
        color: #d7e2f0 !important;
        -webkit-text-fill-color: #d7e2f0 !important;
        transition: background-color 150ms ease, border-color 150ms ease !important;
    }
    .stApp [data-testid="stBaseButton-secondary"]:hover {
        border-color: #637c98 !important;
        background: #182b43 !important;
    }
    .stApp .st-key-cambiar_fecha_seleccionados [data-testid="stBaseButton-primary"] {
        border-color: #27b978 !important;
        background: linear-gradient(110deg, #128653, #20b875) !important;
    }
    .stApp .st-key-eliminar_registros_seleccionados [data-testid="stBaseButton-primary"] {
        border-color: #ff3545 !important;
        background: linear-gradient(110deg, #c91f31, #ff3545) !important;
    }
    .stApp [data-testid="stAlertContainer"] {
        border: 1px solid #314961 !important;
        border-radius: 9px !important;
        background: #102137 !important;
        color: #d9e6f4 !important;
        box-shadow: none !important;
    }
    .stApp [data-testid="stAlertContainer"] p,
    .stApp [data-testid="stAlertContainer"] svg {
        color: inherit !important;
        fill: currentColor !important;
        stroke: currentColor !important;
    }
    .stApp [data-testid="stTabs"] [role="tablist"] {
        border-bottom: 1px solid #26394f;
    }
    .stApp [data-testid="stTabs"] [role="tab"] {
        color: #a7b8cd !important;
    }
    .stApp [data-testid="stTabs"] [role="tab"][aria-selected="true"] {
        color: #fff !important;
        border-bottom-color: var(--bunker-red) !important;
    }
    .stApp hr {
        border-color: rgba(92, 117, 145, 0.32) !important;
    }
    @media (max-width: 900px) {
        [data-testid="stSidebar"] {
            width: min(268px, 82vw) !important;
            min-width: min(268px, 82vw) !important;
        }
        .stApp [data-testid="stMainBlockContainer"] {
            padding-right: 1rem;
            padding-left: 1rem;
        }
        .stApp .personal-resumen-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .stApp .finanzas-balance-grid {
            grid-template-columns: 1fr;
        }
        .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel .finanzas-kpi-grid,
        .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel .finanzas-kpi-grid {
            grid-template-columns: repeat(3, minmax(0, 1fr));
        }
    }
    @media (max-width: 560px) {
        .bunker-page-heading {
            padding-right: 3rem;
        }
        .bunker-page-heading::after {
            width: 2.8rem;
            height: 1.8rem;
        }
        .stApp .reportes-kpi-grid,
        .stApp .personal-resumen-grid,
        .stApp .finanzas-kpi-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-income-panel .finanzas-kpi-grid,
        .stApp:has(#finanzas-theme-marker) .reportes-kpi-panel.finanzas-expense-panel .finanzas-kpi-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .stApp .reportes-kpi-panel {
            padding: 0.75rem;
        }
        .stApp .reportes-kpi-header {
            align-items: flex-start;
            flex-direction: column;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

for opcion in opciones_navegacion:
    st.sidebar.button(
        opcion,
        key=f"navegacion_{claves_navegacion[opcion]}",
        icon=iconos_navegacion[opcion],
        type="primary" if st.session_state.menu_activo == opcion else "secondary",
        use_container_width=True,
        on_click=actualizar_menu_activo,
        args=(opcion,),
    )

st.sidebar.button(
    "Gestión de Personal",
    key="navegacion_gestion_personal",
    icon=":material/calendar_month:",
    type=(
        "primary"
        if st.session_state.menu_activo == "Gestión de Personal"
        else "secondary"
    ),
    use_container_width=True,
    on_click=actualizar_menu_activo,
    args=("Gestión de Personal",),
)

seccion = st.session_state.menu_activo
if "admin_autorizado" not in st.session_state:
    st.session_state.admin_autorizado = False

is_admin = st.session_state.admin_autorizado
is_supervisor = str(st.session_state.get("rol_usuario", "")).startswith(
    "Supervisor"
)
USUARIOS_SUPERVISORES = sorted(
    nombre
    for nombre, cuenta in usuarios.items()
    if cuenta["activo"]
    and cuenta["rol"] == "Supervisor"
)

def mostrar_menu_usuario():
    rol_perfil = (
        "Administrador"
        if st.session_state.rol_usuario == "Administrador"
        else "Supervisor"
        if st.session_state.rol_usuario.startswith("Supervisor")
        else "Lavador"
    )
    with st.popover(
        ":material/account_circle:",
        key="menu_usuario",
        help="Cuenta de usuario",
        width=240,
    ):
        st.markdown(
            '<div class="profile-menu-avatar"><span aria-hidden="true">account_circle</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<p class="profile-menu-label">Usuario</p>'
            f'<p class="profile-menu-user">'
            f'{escape(st.session_state.usuario_actual)}</p>'
            f'<p class="profile-menu-label">Rol</p>'
            f'<p class="profile-menu-role">{escape(rol_perfil)}</p>',
            unsafe_allow_html=True,
        )
        st.divider()
        if st.button(
            "Cerrar sesión",
            key="cerrar_sesion",
            type="secondary",
            use_container_width=True,
        ):
            st.session_state.login_completado = False
            st.session_state.admin_autorizado = False
            st.session_state.menu_activo = "Registros"
            st.session_state.pop("_registros_sesion", None)
            st.session_state.pop("_alcance_registros_sesion", None)
            st.session_state.pop("_gestion_personal_prefetch", None)
            st.session_state.pop("_gestion_personal_prefetch_future", None)
            st.session_state.pop("_usuarios_sesion", None)
            st.session_state.pop("_usuarios_eliminados_sesion", None)
            for key in (
                "usuario_actual",
                "rol_usuario",
                "usuario_login",
                "contrasena_login",
                "password_admin",
            ):
                st.session_state.pop(key, None)
            st.rerun()

mostrar_menu_usuario()

if seccion != "Registros":
    _, logo_columna = st.columns([8, 2])
    with logo_columna:
        st.markdown(
            f'<div class="main-header-logo"><img src="data:image/png;base64,{logo_base64}" alt="The Bunker Car Wash"></div>',
            unsafe_allow_html=True,
        )

if (
    seccion != "Registros"
    and not is_admin
    and not (seccion in {"Gestión", "Control"} and is_supervisor)
):
    mostrar_titulo_seccion(seccion)
    st.error("Acceso restringido. Se requiere una cuenta de Administrador.")
    st.stop()

df_registros = None
if seccion in {
    "Registros",
    "Control",
    "Finanzas",
    "Personal",
    "Gestión de Personal",
}:
    df_registros = _obtener_registros_sesion(copiar=False)

if seccion == "Gestión de Personal":
    cache_resumenes_adelantos = {}
    cache_metricas_bonificacion = {}

    def _metricas_bonificacion_personal(fecha):
        if fecha not in cache_metricas_bonificacion:
            registros_acumulados = filtrar_registros_acumulados_mes(
                df_registros,
                fecha,
            )
            metricas_por_lavador, _ = calcular_metricas_lavadores(
                registros_acumulados,
                lista_lavadores,
            )
            cache_metricas_bonificacion[fecha] = metricas_por_lavador
        return cache_metricas_bonificacion[fecha]

    def _resumen_adelantos_personal(fecha_inicio, fecha_fin):
        clave = (fecha_inicio, fecha_fin)
        if clave not in cache_resumenes_adelantos:
            cache_resumenes_adelantos[clave] = (
                database.consultar_resumen_adelantos_registros_personal(
                    fecha_inicio,
                    fecha_fin,
                )
            )
        return cache_resumenes_adelantos[clave]

    def obtener_bonificacion_mensual_personal(nombre, fecha):
        metricas = _metricas_bonificacion_personal(fecha).get(
            nombre.strip().casefold()
        )
        return (
            Decimal(str(metricas["monto"] / 30))
            if metricas
            else Decimal("0")
        )

    def obtener_desglose_bonificacion_mensual_personal(nombre, fecha):
        metricas = _metricas_bonificacion_personal(fecha).get(
            nombre.strip().casefold(),
        )
        if metricas is None:
            metricas = {}
        return {
            "solitarios": metricas.get("solitarios", 0),
            "compartidos": metricas.get("compartidos", 0),
            "grupales": metricas.get("grupales", 0),
            "monto_solitario": metricas.get("monto_solitario", 0),
            "monto_compartido": metricas.get("monto_compartido", 0),
            "monto_grupal": metricas.get("monto_grupal", 0),
        }

    def obtener_adelantos_registrados_personal(
        nombre, periodo_inicio, periodo_fin
    ):
        trabajador = " ".join(nombre.casefold().split())
        return sum(
            (
                Decimal(str(registro["importe"]))
                for registro
                in _resumen_adelantos_personal(
                    periodo_inicio,
                    periodo_fin,
                )
                if registro["trabajador"] == trabajador
            ),
            Decimal("0.00"),
        )

    def obtener_detalle_adelantos_mensuales_personal(nombre, fecha):
        inicio = fecha.replace(day=1)
        fin = fecha.replace(
            day=calendar.monthrange(fecha.year, fecha.month)[1]
        )
        trabajador = " ".join(nombre.casefold().split())
        return [
            {
                "fecha": registro["fecha"],
                "importe": Decimal(str(registro["importe"])),
                "solicitudes": registro["solicitudes"],
            }
            for registro
            in _resumen_adelantos_personal(
                inicio,
                fin,
            )
            if registro["trabajador"] == trabajador
        ]

    st.session_state[
        "_gestion_personal_obtener_adelantos_registrados"
    ] = obtener_adelantos_registrados_personal
    st.session_state[
        "_gestion_personal_obtener_detalle_adelantos_mensuales"
    ] = obtener_detalle_adelantos_mensuales_personal
    st.session_state[
        "_gestion_personal_obtener_desglose_bonificacion_mensual"
    ] = obtener_desglose_bonificacion_mensual_personal
    with st.spinner("Cargando datos del periodo..."):
        render_gestion_personal(
            lista_lavadores,
            datetime.now(ZONA_HORARIA_PERU).date(),
            st.session_state.usuario_actual,
            obtener_bonificacion_mensual_personal,
        )
    mostrar_notificacion_exito()
    st.stop()

@st.dialog("Confirmar eliminación definitiva")
def mostrar_confirmacion_eliminar_supervisor(nombre):
    st.warning(
        "¿Desea eliminar definitivamente los registros y el historial de este supervisor?"
    )
    clave = hashlib.sha256(nombre.casefold().encode("utf-8")).hexdigest()[:12]
    col_confirmar, col_cancelar = st.columns(2)
    with col_confirmar:
        confirmar = st.button(
            "Eliminar definitivamente",
            key=f"confirmar_eliminar_supervisor_{clave}",
            type="primary",
            use_container_width=True,
        )
    with col_cancelar:
        cancelar = st.button(
            "Cancelar",
            key=f"cancelar_eliminar_supervisor_{clave}",
            type="secondary",
            use_container_width=True,
        )

    if cancelar:
        st.rerun()
    if not confirmar:
        return

    cuenta = usuarios.get(nombre)
    if (
        cuenta is None
        or cuenta["activo"]
        or cuenta["rol"] != "Supervisor"
    ):
        st.error("El supervisor ya no está disponible para eliminación.")
        return

    usuarios_actualizados = {
        nombre_usuario: detalle.copy()
        for nombre_usuario, detalle in usuarios.items()
    }
    del usuarios_actualizados[nombre]
    eliminados_actualizados = set(usuarios_eliminados_definitivamente)
    eliminados_actualizados.discard(nombre.casefold())

    try:
        guardar_eliminacion_supervisor(
            usuarios_actualizados,
            nombre,
            eliminados_actualizados,
        )
    except (OSError, psycopg2.Error) as error:
        st.error(f"No se pudo completar la eliminación definitiva: {error}")
    else:
        st.session_state.pop("supervisor_detalle_finanzas", None)
        st.session_state.pop("responsable_ajustes", None)
        st.session_state["mensaje_gestion_usuarios"] = (
            f"Se eliminaron la cuenta y los registros de {nombre}."
        )
        st.rerun()

if seccion == "Personal":
   st.markdown(
       '<div id="personal-theme-marker" aria-hidden="true"></div>',
       unsafe_allow_html=True,
   )
   mostrar_titulo_seccion(seccion)
   ahora_personal = datetime.now(ZONA_HORARIA_PERU)
   col_lavador_personal, col_periodo_personal, col_fecha_personal = st.columns(
       [1.4, 1.0, 1.5]
   )
   with col_lavador_personal:
       lavadores_disponibles = lista_lavadores or ["Sin lavadores registrados"]
       lavador_personal = st.selectbox(
           "Lavador",
           lavadores_disponibles,
           key="lavador_rendimiento_personal",
           disabled=not lista_lavadores,
       )
   with col_periodo_personal:
       periodo_personal = st.selectbox(
           "Periodo",
           ["Día", "Mes"],
           key="periodo_rendimiento_personal",
       )
   with col_fecha_personal:
       fecha_personal = seleccionar_fecha(
           "Fecha de referencia",
           "fecha_rendimiento_personal",
           ahora_personal.date(),
       )

   if not lista_lavadores:
       st.info("Agrega lavadores desde Gestión para consultar su rendimiento.")
   else:
       registros_personal_periodo = (
           filtrar_registros_por_fecha(df_registros, fecha_personal)
           if periodo_personal == "Día"
           else filtrar_registros_por_mes(df_registros, fecha_personal)
       )
       metricas_personal, _ = calcular_metricas_lavador(
           registros_personal_periodo,
           lavador_personal,
       )
       _, detalle_personal_dia = calcular_metricas_lavador(
           filtrar_registros_por_fecha(df_registros, fecha_personal),
           lavador_personal,
       )
       _, detalle_personal_mes = calcular_metricas_lavador(
           filtrar_registros_por_mes(df_registros, fecha_personal),
           lavador_personal,
       )
       registros_acumulado_mes_personal = filtrar_registros_acumulados_mes(
           df_registros,
           fecha_personal,
       )
       metricas_acumulado_mes_personal, _ = calcular_metricas_lavador(
           registros_acumulado_mes_personal,
           lavador_personal,
       )
       etiqueta_fecha_personal = (
           fecha_personal.strftime("%d/%m/%Y")
           if periodo_personal == "Día"
           else fecha_personal.strftime("%m/%Y")
       )
       tarjetas_personal = (
           (
               "directions_car",
               "Vehículos lavados",
               metricas_personal["vehiculos"],
               "servicios realizados",
               "total",
               False,
           ),
           (
               "person",
               "Solitario",
               metricas_personal["solitarios"],
               "lavador único",
               "acumulado",
               False,
           ),
           (
               "group",
               "Compartido (2 lavadores)",
               metricas_personal["compartidos"],
               "servicios compartidos",
               "compartido",
               False,
           ),
           (
               "groups",
               "Grupal (3 lavadores)",
               metricas_personal["grupales"],
               "servicios grupales",
               "grupal",
               False,
           ),
       )
       tarjetas_html_personal = []
       for (
           icono_personal,
           etiqueta_tarjeta_personal,
           valor_tarjeta_personal,
           unidad_tarjeta_personal,
           clase_tarjeta_personal,
           es_monto_personal,
       ) in tarjetas_personal:
           valor_formateado_personal = (
               f"S/ {valor_tarjeta_personal:.2f}"
               if es_monto_personal
               else f"{valor_tarjeta_personal:d}"
           )
           tarjetas_html_personal.append(
               '<div class="reportes-kpi-card '
               f'{clase_tarjeta_personal}">'
               '<div class="reportes-kpi-acento"></div>'
               '<div class="reportes-kpi-icono" aria-hidden="true">'
               f"{icono_personal}</div>"
               f'<div class="reportes-kpi-etiqueta">'
               f"{escape(etiqueta_tarjeta_personal)}</div>"
               '<div class="reportes-kpi-valor">'
               f"{valor_formateado_personal}</div>"
               '<div class="reportes-kpi-unidad">'
               f"{escape(unidad_tarjeta_personal)}</div></div>"
           )
       st.markdown(
           '<section class="reportes-kpi-panel personal-resumen-panel">'
           '<div class="reportes-kpi-header">'
           f'<span class="reportes-kpi-heading">'
           f'{escape(lavador_personal)} · Resumen del periodo</span>'
           f'<span class="reportes-kpi-periodo">{periodo_personal} · '
           f'{etiqueta_fecha_personal}</span>'
           '</div>'
           f'<div class="personal-resumen-grid">{"".join(tarjetas_html_personal)}</div>'
           '</section>',
           unsafe_allow_html=True,
       )

       tarjetas_ingresos_personal = (
           (
               "person",
               "Monto Solitario",
               metricas_personal["monto_solitario"],
               "parte proporcional asignada",
               "efectivo",
           ),
           (
               "group",
               "Monto Compartido (2 lavadores)",
               metricas_personal["monto_compartido"],
               "parte proporcional asignada",
               "yape",
           ),
           (
               "groups",
               "Monto Grupal (3 lavadores)",
               metricas_personal["monto_grupal"],
               "parte proporcional asignada",
               "plin",
           ),
           (
               "payments",
               "Monto Total Acumulado",
               metricas_personal["monto"],
               "total producido",
               "transferencia",
           ),
       )
       tarjetas_html_ingresos_personal = "".join(
           '<div class="reportes-kpi-card supervisor finanzas-metodo-card '
           f'finanzas-metodo-{clase_ingreso_personal}">'
           '<div class="reportes-kpi-acento"></div>'
           '<div class="reportes-kpi-icono" aria-hidden="true">'
           f"{icono_ingreso_personal}</div>"
           f'<div class="reportes-kpi-etiqueta">'
           f"{escape(etiqueta_ingreso_personal)}</div>"
           '<div class="reportes-kpi-valor">'
           f"S/ {monto_ingreso_personal:.2f}</div>"
           '<div class="reportes-kpi-unidad">'
           f"{escape(unidad_ingreso_personal)}</div></div>"
           for (
               icono_ingreso_personal,
               etiqueta_ingreso_personal,
               monto_ingreso_personal,
               unidad_ingreso_personal,
               clase_ingreso_personal,
           ) in tarjetas_ingresos_personal
       )
       st.markdown(
           '<section class="reportes-kpi-panel personal-ingresos-panel">'
           '<div class="reportes-kpi-header">'
           '<span class="reportes-kpi-heading">'
           f"Ingresos Producidos · {periodo_personal} seleccionado"
           '</span>'
           f'<span class="reportes-kpi-periodo">{etiqueta_fecha_personal}</span>'
           '</div>'
           f'<div class="finanzas-kpi-grid">'
           f"{tarjetas_html_ingresos_personal}</div>"
           '</section>',
           unsafe_allow_html=True,
       )

       with st.container(border=True, key="personal_bonificacion_panel"):
           st.markdown(
               '<div class="reportes-kpi-header">'
               '<span class="reportes-kpi-heading">'
               f'{escape(lavador_personal)} · Bonificación Acumulada del Mes'
               '</span>'
               f'<span class="reportes-kpi-periodo">Hasta '
               f'{fecha_personal.strftime("%d/%m/%Y")}</span>'
               '</div>',
               unsafe_allow_html=True,
           )
           col_bonificacion_personal, col_grafico_modalidad_personal = st.columns(
               [1, 1.5]
           )
           with col_bonificacion_personal:
               st.markdown(
                   '<div class="personal-bonificacion-card">'
                   '<div class="personal-bonificacion-etiqueta">'
                   'Monto de Bonificación</div>'
                   '<div class="personal-bonificacion-valor">'
                   f'S/ {metricas_acumulado_mes_personal["monto"] / 30:.2f}'
                   '</div>'
                   '<div class="personal-bonificacion-pie">'
                   'Acumulado en lo que va del mes</div></div>',
                   unsafe_allow_html=True,
               )

           with col_grafico_modalidad_personal:
               datos_modalidad_personal = pd.DataFrame(
                   [
                       {
                           "Modalidad": "Solitario",
                           "Autos": metricas_acumulado_mes_personal["solitarios"],
                           "Monto acumulado": (
                               "S/ "
                               f'{metricas_acumulado_mes_personal["monto_solitario"]:.2f}'
                           ),
                       },
                       {
                           "Modalidad": "Compartido",
                           "Autos": metricas_acumulado_mes_personal["compartidos"],
                           "Monto acumulado": (
                               "S/ "
                               f'{metricas_acumulado_mes_personal["monto_compartido"]:.2f}'
                           ),
                       },
                       {
                           "Modalidad": "Grupal",
                           "Autos": metricas_acumulado_mes_personal["grupales"],
                           "Monto acumulado": (
                               "S/ "
                               f'{metricas_acumulado_mes_personal["monto_grupal"]:.2f}'
                           ),
                       },
                   ]
               )
               grafico_modalidad_personal = (
                   alt.Chart(datos_modalidad_personal)
                   .mark_arc(innerRadius=58, outerRadius=105)
                   .encode(
                       theta=alt.Theta("Autos:Q", stack=True),
                       color=alt.Color(
                           "Modalidad:N",
                           scale=alt.Scale(
                               domain=["Solitario", "Compartido", "Grupal"],
                               range=["#20d38a", "#36b8ff", "#b779f7"],
                           ),
                           legend=alt.Legend(title=None),
                       ),
                       tooltip=[
                           alt.Tooltip("Modalidad:N", title="Categoría"),
                           alt.Tooltip("Autos:Q", title="Cantidad de Autos", format="d"),
                           alt.Tooltip(
                               "Monto acumulado:N",
                               title="Monto Acumulado",
                           ),
                       ],
                   )
                   .properties(height=250)
                   .configure(background="transparent")
                   .configure_legend(
                       labelColor="#dce6f2",
                       titleColor="#dce6f2",
                   )
                   .configure_view(strokeOpacity=0)
               )
               with st.container(
                   border=True,
                   key="personal_bonificacion_chart",
               ):
                   st.markdown(
                       '<div class="reportes-kpi-etiqueta">'
                       'Desglose de Trabajo por Tipo de Servicio</div>',
                       unsafe_allow_html=True,
                   )
                   if metricas_acumulado_mes_personal["vehiculos"]:
                       st.altair_chart(
                           grafico_modalidad_personal,
                           use_container_width=True,
                       )
                   else:
                       st.info(
                           "Aún no hay servicios para este lavador "
                           "en el mes seleccionado."
                       )

       st.subheader("Detalle de servicios")
       for titulo_detalle_personal, detalle_personal, periodo_detalle_personal in (
           (
               "Más detalles de servicios (Día)",
               detalle_personal_dia,
               "el día seleccionado",
           ),
           (
               "Más detalles de servicios (Mes)",
               detalle_personal_mes,
               "el mes seleccionado",
           ),
       ):
           with st.expander(titulo_detalle_personal, expanded=False):
               if detalle_personal.empty:
                   st.info(
                       "No hay servicios para este lavador en "
                       f"{periodo_detalle_personal}."
                   )
               else:
                   st.dataframe(
                       detalle_personal,
                       use_container_width=True,
                       hide_index=True,
                       column_config={
                           "Monto del lavado": st.column_config.NumberColumn(
                               format="S/ %.2f"
                           ),
                           "Monto producido": st.column_config.NumberColumn(
                               format="S/ %.2f"
                           ),
                       },
                   )

   mostrar_notificacion_exito()
   st.stop()


if seccion == "Finanzas":
    df_finanzas, responsables_finanzas = obtener_registros_financieros_sesion(
        df_registros
    )
    st.markdown(
        '<div id="finanzas-theme-marker" aria-hidden="true"></div>',
        unsafe_allow_html=True,
    )
    mostrar_titulo_seccion(seccion)
    ahora_finanzas = datetime.now(ZONA_HORARIA_PERU)
    nombres_meses_finanzas = [
        "Enero",
        "Febrero",
        "Marzo",
        "Abril",
        "Mayo",
        "Junio",
        "Julio",
        "Agosto",
        "Septiembre",
        "Octubre",
        "Noviembre",
        "Diciembre",
    ]
    st.subheader("Balance de Ingresos")
    fecha_referencia_balance = seleccionar_fecha(
        "Fecha de referencia",
        "fecha_referencia_balance_finanzas",
        ahora_finanzas.date(),
    )
    fecha_referencia_mes = fecha_referencia_balance
    mes_referencia_finanzas = nombres_meses_finanzas[
        fecha_referencia_mes.month - 1
    ]
    periodo_mensual_finanzas = (
        f"{mes_referencia_finanzas} {fecha_referencia_mes.year}"
    )
    registros_balance_mes = filtrar_registros_financieros(
        df_finanzas, fecha_referencia_mes, "Mes"
    )
    ingresos_mes, _ = desglosar_registros_financieros(
        registros_balance_mes
    )
    ganancia_mes = sum(ingresos_mes.values())

    registros_balance_ingresos_dia = filtrar_registros_financieros(
        df_finanzas, fecha_referencia_balance, "Día"
    )
    ingresos_dia, _ = desglosar_registros_financieros(
        registros_balance_ingresos_dia
    )
    ganancia_dia = sum(ingresos_dia.values())

    def renderizar_desglose_metodos(
        titulo, fecha, montos, periodo_tipo, categoria
    ):
        orden_metodos = [
            metodo
            for metodo in (
                "Efectivo",
                "Yape",
                "Plin",
                "Tarjeta",
                "Transferencia",
            )
            if metodo in montos
        ]
        orden_metodos.extend(
            sorted(
                (
                    metodo
                    for metodo, monto in montos.items()
                    if metodo not in orden_metodos and monto != 0
                ),
                key=str.casefold,
            )
        )
        clases_metodo = {
            "efectivo": "efectivo",
            "yape": "yape",
            "plin": "plin",
            "transferencia": "transferencia",
            "tarjeta": "tarjeta",
        }
        etiqueta_categoria = "Ingresos" if categoria == "ingreso" else "Gastos"
        tarjetas = "".join(
            f'<div class="reportes-kpi-card '
            f'{"gasto" if categoria == "gasto" else "supervisor"} '
            f'finanzas-metodo-card finanzas-metodo-{clases_metodo.get(metodo.casefold(), "otros")}" '
            f'tabindex="0" role="group" '
            f'aria-label="{escape(metodo)}: S/ {montos[metodo]:.2f}, {etiqueta_categoria}">'
            '<div class="reportes-kpi-acento"></div>'
            '<div class="reportes-kpi-icono" aria-hidden="true">'
            'payments'
            '</div>'
            f'<div class="reportes-kpi-etiqueta">{escape(metodo)}</div>'
            f'<div class="reportes-kpi-valor">S/ {montos[metodo]:.2f}</div>'
            '<div class="reportes-kpi-unidad">'
            f'{etiqueta_categoria}'
            '</div></div>'
            for metodo in orden_metodos
        )
        clase_panel = (
            "finanzas-expense-panel" if categoria == "gasto" else "finanzas-income-panel"
        )
        periodo = (
            fecha.strftime("%d/%m/%Y")
            if periodo_tipo == "dia"
            else f"{nombres_meses_finanzas[fecha.month - 1]} {fecha.year}"
        )
        st.markdown(
            f'<section class="reportes-kpi-panel {clase_panel}">'
            '<div class="reportes-kpi-header">'
            f'<span class="reportes-kpi-heading">{escape(titulo)}</span>'
            f'<span class="reportes-kpi-periodo">{periodo}</span>'
            '</div>'
            f'<div class="finanzas-kpi-grid">{tarjetas}</div>'
            '</section>',
            unsafe_allow_html=True,
        )

    st.markdown(
        '<div class="finanzas-balance-grid">'
        '<div class="finanzas-balance-card ingreso">'
        '<span class="finanzas-balance-icon" aria-hidden="true">payments</span>'
        '<div class="finanzas-balance-content">'
        '<div class="finanzas-balance-etiqueta">Ingresos netos del día</div>'
        f'<div class="finanzas-balance-valor">S/ {ganancia_dia:.2f}</div>'
        '</div>'
        '</div>'
        '<div class="finanzas-balance-card ingreso">'
        '<span class="finanzas-balance-icon" aria-hidden="true">calendar_month</span>'
        '<div class="finanzas-balance-content">'
        '<div class="finanzas-balance-etiqueta">Ingresos netos del mes</div>'
        f'<div class="finanzas-balance-valor">S/ {ganancia_mes:.2f}</div>'
        '</div>'
        '</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    with st.expander("Ver ingresos por método (Día)", expanded=False):
        renderizar_desglose_metodos(
            "Ingresos por método · Día seleccionado",
            fecha_referencia_balance,
            ingresos_dia,
            "dia",
            "ingreso",
        )
    with st.expander("Ver ingresos por método (Mes)", expanded=False):
        renderizar_desglose_metodos(
            f"Ingresos por método · {periodo_mensual_finanzas}",
            fecha_referencia_mes,
            ingresos_mes,
            "mes",
            "ingreso",
        )

    st.subheader("Balance de Gastos")
    fecha_referencia_gastos = seleccionar_fecha(
        "Fecha de referencia",
        "fecha_referencia_gastos_finanzas",
        ahora_finanzas.date(),
    )
    fecha_referencia_mes_gastos = fecha_referencia_gastos
    periodo_mensual_gastos = (
        f"{nombres_meses_finanzas[fecha_referencia_mes_gastos.month - 1]} "
        f"{fecha_referencia_mes_gastos.year}"
    )
    registros_balance_gastos_mes = filtrar_registros_financieros(
        df_finanzas, fecha_referencia_mes_gastos, "Mes"
    )
    _, gastos_mes = desglosar_registros_financieros(
        registros_balance_gastos_mes
    )
    gasto_mes = sum(gastos_mes.values())
    registros_balance_gastos_dia = filtrar_registros_financieros(
        df_finanzas, fecha_referencia_gastos, "Día"
    )
    _, gastos_dia = desglosar_registros_financieros(
        registros_balance_gastos_dia
    )
    gasto_dia = sum(gastos_dia.values())
    st.markdown(
        '<div class="finanzas-balance-grid">'
        '<div class="finanzas-balance-card gasto">'
        '<span class="finanzas-balance-icon" aria-hidden="true">account_balance_wallet</span>'
        '<div class="finanzas-balance-content">'
        '<div class="finanzas-balance-etiqueta">Gastos netos del día</div>'
        f'<div class="finanzas-balance-valor">S/ {gasto_dia:.2f}</div>'
        '</div>'
        '</div>'
        '<div class="finanzas-balance-card gasto">'
        '<span class="finanzas-balance-icon" aria-hidden="true">calendar_month</span>'
        '<div class="finanzas-balance-content">'
        '<div class="finanzas-balance-etiqueta">Gastos netos del mes</div>'
        f'<div class="finanzas-balance-valor">S/ {gasto_mes:.2f}</div>'
        '</div>'
        '</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    with st.expander("Ver método de gasto (Día)", expanded=False):
        renderizar_desglose_metodos(
            "Gastos por método · Día seleccionado",
            fecha_referencia_gastos,
            gastos_dia,
            "dia",
            "gasto",
        )
    with st.expander("Ver método de gasto (Mes)", expanded=False):
        renderizar_desglose_metodos(
            f"Gastos por método · {periodo_mensual_gastos}",
            fecha_referencia_mes_gastos,
            gastos_mes,
            "mes",
            "gasto",
        )

    st.subheader("Detalle de ingresos del supervisor")
    with st.container():
        col_usuario_detalle, col_periodo_detalle, col_fecha_detalle, _ = st.columns(
            [1.4, 1.0, 1.5, 2.1]
        )
        with col_usuario_detalle:
            supervisor_detalle = st.selectbox(
                "Selecciona un usuario",
                USUARIOS_SUPERVISORES,
                key="supervisor_detalle_finanzas",
                disabled=not USUARIOS_SUPERVISORES,
            )
        with col_periodo_detalle:
            periodo_detalle_finanzas = st.selectbox(
                "Periodo del detalle",
                ["Día", "Mes"],
                index=1,
                key="periodo_detalle_supervisor_finanzas_v2",
            )
        with col_fecha_detalle:
            fecha_detalle_finanzas = seleccionar_fecha(
                "Fecha del detalle",
                "fecha_detalle_finanzas",
                ahora_finanzas.date(),
            )

        registros_supervisor_detalle = df_finanzas
        if supervisor_detalle is not None:
            registros_supervisor_detalle = df_finanzas.loc[
                df_finanzas["_responsable_busqueda_finanzas"]
                == str(supervisor_detalle).strip().casefold()
            ]
        if periodo_detalle_finanzas == "Día":
            registros_supervisor_periodo = filtrar_registros_financieros(
                registros_supervisor_detalle,
                fecha_detalle_finanzas,
                "Día",
            )
            tipo_periodo_detalle = "dia"
            etiqueta_periodo_detalle = fecha_detalle_finanzas.strftime("%d/%m/%Y")
            sufijo_periodo_detalle = "del día"
        else:
            registros_supervisor_periodo = filtrar_registros_financieros(
                registros_supervisor_detalle,
                fecha_detalle_finanzas,
                "Mes",
            )
            tipo_periodo_detalle = "mes"
            etiqueta_periodo_detalle = (
                f"{nombres_meses_finanzas[fecha_detalle_finanzas.month - 1]} "
                f"{fecha_detalle_finanzas.year}"
            )
            sufijo_periodo_detalle = "del mes"

        ingresos_detalle_metodos, gastos_detalle_metodos = (
            desglosar_registros_financieros(registros_supervisor_periodo)
        )
        ingreso_periodo_detalle = sum(ingresos_detalle_metodos.values())
        gasto_periodo_detalle = sum(gastos_detalle_metodos.values())
        tarjetas_detalle_supervisor = (
            '<div class="reportes-kpi-card supervisor">'
            '<div class="reportes-kpi-acento"></div>'
            '<div class="reportes-kpi-icono" aria-hidden="true">payments</div>'
            f'<div class="reportes-kpi-etiqueta">Ingresos netos '
            f'{sufijo_periodo_detalle}</div>'
            f'<div class="reportes-kpi-valor">S/ {ingreso_periodo_detalle:.2f}</div>'
            f'<div class="reportes-kpi-unidad">{etiqueta_periodo_detalle}</div>'
            '</div>'
            '<div class="reportes-kpi-card gasto">'
            '<div class="reportes-kpi-acento"></div>'
            '<div class="reportes-kpi-icono" aria-hidden="true">'
            'account_balance_wallet</div>'
            f'<div class="reportes-kpi-etiqueta">Gastos netos '
            f'{sufijo_periodo_detalle}</div>'
            f'<div class="reportes-kpi-valor">S/ {gasto_periodo_detalle:.2f}</div>'
            f'<div class="reportes-kpi-unidad">{etiqueta_periodo_detalle}</div>'
            '</div>'
        )
        st.markdown(
            '<section class="reportes-kpi-panel">'
            '<div class="reportes-kpi-header">'
            f'<span class="reportes-kpi-heading">{escape(supervisor_detalle or "")}</span>'
            f'<span class="reportes-kpi-periodo">{periodo_detalle_finanzas} · '
            f'{etiqueta_periodo_detalle}</span>'
            '</div>'
            f'<div class="finanzas-kpi-grid">{tarjetas_detalle_supervisor}</div>'
            '</section>',
            unsafe_allow_html=True,
        )
        renderizar_desglose_metodos(
            f"Ingresos por método · {supervisor_detalle} · "
            f"{periodo_detalle_finanzas.lower()}",
            fecha_detalle_finanzas,
            ingresos_detalle_metodos,
            tipo_periodo_detalle,
            "ingreso",
        )
        renderizar_desglose_metodos(
            f"Gastos por método · {supervisor_detalle} · "
            f"{periodo_detalle_finanzas.lower()}",
            fecha_detalle_finanzas,
            gastos_detalle_metodos,
            tipo_periodo_detalle,
            "gasto",
        )

    st.divider()
    with st.expander("Rendimiento de ingresos por supervisor", expanded=False):
        col_periodo_finanzas, col_fecha_finanzas, _ = st.columns(
            [1.2, 1.5, 3.3]
        )
        with col_periodo_finanzas:
            periodo_finanzas = st.selectbox(
                "Periodo del desglose",
                ["Día", "Mes"],
                key="periodo_desglose_finanzas",
            )
        with col_fecha_finanzas:
            fecha_finanzas = seleccionar_fecha(
                "Fecha de referencia",
                "fecha_desglose_finanzas",
                ahora_finanzas.date(),
            )

        ingresos_responsables = calcular_ingresos_financieros_por_responsable(
            df_finanzas,
            fecha_finanzas,
            periodo_finanzas,
            responsables_finanzas,
        )
        etiqueta_periodo_finanzas = (
            fecha_finanzas.strftime("%d/%m/%Y")
            if periodo_finanzas == "Día"
            else fecha_finanzas.strftime("%m/%Y")
        )
        tarjetas_finanzas = []
        for responsable, monto in ingresos_responsables.items():
            tarjetas_finanzas.append(
                '<div class="reportes-kpi-card supervisor">'
                '<div class="reportes-kpi-acento"></div>'
                '<div class="reportes-kpi-icono" aria-hidden="true">support_agent</div>'
                f'<div class="reportes-kpi-etiqueta">{escape(responsable)}</div>'
                f'<div class="reportes-kpi-valor">S/ {monto:.2f}</div>'
                '<div class="reportes-kpi-unidad">ingresos registrados</div>'
                '</div>'
            )
        tarjetas_finanzas.append(
            '<div class="reportes-kpi-card total">'
            '<div class="reportes-kpi-acento"></div>'
            '<div class="reportes-kpi-icono" aria-hidden="true">payments</div>'
            '<div class="reportes-kpi-etiqueta">Total del equipo</div>'
            f'<div class="reportes-kpi-valor">S/ {ingresos_responsables.sum():.2f}</div>'
            '<div class="reportes-kpi-unidad">ingresos del periodo</div>'
            '</div>'
        )
        st.markdown(
            '<section class="reportes-kpi-panel">'
            '<div class="reportes-kpi-header">'
            '<span class="reportes-kpi-heading">Ingresos por supervisor</span>'
            f'<span class="reportes-kpi-periodo">{periodo_finanzas} · '
            f'{etiqueta_periodo_finanzas}</span>'
            '</div>'
            f'<div class="finanzas-kpi-grid">{"".join(tarjetas_finanzas)}</div>'
            '</section>',
            unsafe_allow_html=True,
        )

    mostrar_notificacion_exito()
    st.stop()

if seccion == "Gestión":
    mostrar_titulo_seccion(seccion)
    st.markdown(
        """
        <style>
        .st-key-tabla_gestion_personal,
        .st-key-tabla_gestion_vehiculo,
        .st-key-tabla_gestion_servicio {
            gap: 0 !important;
        }
        .st-key-encabezado_tabla_gestion_personal,
        .st-key-encabezado_tabla_gestion_vehiculo,
        .st-key-encabezado_tabla_gestion_servicio {
            margin-bottom: 1.5rem !important;
        }
        .gestion-catalog-table-header {
            padding: 0.65rem 0.9rem;
            background: rgba(128, 139, 150, 0.12);
            border-bottom: 1px solid rgba(128, 139, 150, 0.24);
            color: #c5ccd4;
            font-size: 0.9rem;
            font-weight: 600;
        }
        [class*="st-key-fila_gestion_"] button {
            justify-content: flex-start !important;
            text-align: left !important;
            border-radius: 0 !important;
            border-top: 0 !important;
            border-color: rgba(128, 139, 150, 0.2) !important;
            background: rgba(20, 24, 32, 0.72) !important;
            color: #e5e7eb !important;
            -webkit-text-fill-color: #e5e7eb !important;
        }
        [class*="st-key-fila_gestion_"] button > div {
            justify-content: flex-start !important;
            width: 100%;
        }
        [class*="st-key-fila_gestion_"] button span {
            display: block;
            width: 100%;
            text-align: left !important;
        }
        [class*="st-key-fila_gestion_"] button:hover {
            background: rgba(179, 38, 45, 0.16) !important;
            border-color: rgba(179, 38, 45, 0.55) !important;
        }
        [class*="st-key-fila_gestion_"] button[kind="primary"] {
            background: #b3262d !important;
            border-color: #b3262d !important;
            color: #ffffff !important;
            -webkit-text-fill-color: #ffffff !important;
            box-shadow: none !important;
        }
        [class*="st-key-fila_gestion_"] button[kind="primary"]:hover {
            background: #b3262d !important;
            border-color: #b3262d !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    catalogos_gestion = (
        (
            "personal",
            "Lavadores",
            "Control de Lavadores",
            lista_lavadores,
            "personas registradas",
            "Agregar Personal +",
            "abrir_agregar_personal",
            "Eliminar Persona",
            "dar_baja_personal_button",
        ),
        (
            "vehiculo",
            "Vehículos",
            "Tipos de Vehículos",
            lista_tipos,
            "tipos registrados",
            "Agregar Tipo +",
            "abrir_agregar_tipo_vehiculo",
            "Eliminar Vehículo",
            "quitar_tipo_button",
        ),
        (
            "servicio",
            "Servicio",
            "Tipos de Servicios",
            lista_servicios,
            "servicios registrados",
            "Agregar Servicio +",
            "abrir_agregar_servicio",
            "Eliminar Servicio",
            "quitar_servicio_button",
        ),
    )
    elementos_por_tipo = {
        "personal": lista_lavadores,
        "vehiculo": lista_tipos,
        "servicio": lista_servicios,
    }
    for (
        tipo_catalogo,
        titulo,
        titulo_tabla,
        elementos,
        etiqueta,
        etiqueta_agregar,
        clave_agregar,
        etiqueta_quitar,
        clave_quitar,
    ) in catalogos_gestion:
        with st.container(key=f"gestion_{tipo_catalogo}_panel", border=True):
            mostrar_notificacion_exito(tipo_catalogo)
            col_titulo, col_agregar, col_quitar = st.columns([3, 1.25, 1.6])
            with col_titulo:
                st.subheader(titulo)
                st.caption(f"{len(elementos)} {etiqueta}")
            with col_agregar:
                if st.button(
                    etiqueta_agregar,
                    key=clave_agregar,
                    type="primary",
                    use_container_width=True,
                ):
                    mostrar_formulario_catalogo(tipo_catalogo)

            seleccion_activa = st.session_state.get("gestion_seleccion_activa")
            if (
                not isinstance(seleccion_activa, dict)
                and seleccion_activa is not None
            ):
                st.session_state.pop("gestion_seleccion_activa", None)
                seleccion_activa = None
            if (
                seleccion_activa is not None
                and (
                    seleccion_activa.get("tipo") not in elementos_por_tipo
                    or seleccion_activa.get("elemento")
                    not in elementos_por_tipo.get(
                        seleccion_activa.get("tipo"), []
                    )
                )
            ):
                st.session_state.pop("gestion_seleccion_activa", None)
                seleccion_activa = None

            with col_quitar:
                if st.button(
                    etiqueta_quitar,
                    key=clave_quitar,
                    type="secondary",
                    use_container_width=True,
                ):
                    seleccion_activa = st.session_state.get(
                        "gestion_seleccion_activa"
                    )
                    if (
                        seleccion_activa is None
                        or seleccion_activa.get("tipo") != tipo_catalogo
                    ):
                        mensajes_sin_seleccion = {
                            "personal": "Selecciona un personal antes de darlo de baja.",
                            "vehiculo": "Se necesita seleccionar un tipo de vehículo.",
                            "servicio": "Se necesita seleccionar un servicio.",
                        }
                        st.error(mensajes_sin_seleccion[tipo_catalogo])
                    elif tipo_catalogo == "personal":
                        mostrar_confirmacion_baja_personal(
                            seleccion_activa["elemento"]
                        )
                    elif len(elementos) <= 1:
                        st.error(
                            "No se puede quitar el último tipo de vehículo."
                            if tipo_catalogo == "vehiculo"
                            else "No se puede quitar el último servicio."
                        )
                    else:
                        elemento_seleccionado = seleccion_activa["elemento"]
                        elementos_actualizados = [
                            elemento
                            for elemento in elementos
                            if elemento != elemento_seleccionado
                        ]
                        archivo_catalogo = (
                            ARCHIVO_VEHICULOS
                            if tipo_catalogo == "vehiculo"
                            else ARCHIVO_SERVICIOS
                        )
                        guardar_catalogo(archivo_catalogo, elementos_actualizados)
                        clave_input = (
                            "input_tipo"
                            if tipo_catalogo == "vehiculo"
                            else "input_servicio"
                        )
                        if st.session_state.get(clave_input) == elemento_seleccionado:
                            st.session_state[clave_input] = elementos_actualizados[0]
                        st.session_state.pop("gestion_seleccion_activa", None)
                        notificar_exito(
                            f"{'Tipo de vehículo' if tipo_catalogo == 'vehiculo' else 'Servicio'} "
                            f"{elemento_seleccionado} quitado correctamente.",
                            "eliminacion",
                            destino=tipo_catalogo,
                        )
                        st.rerun()

            with st.container(
                key=f"tabla_gestion_{tipo_catalogo}", border=True
            ):
                with st.container(
                    key=f"encabezado_tabla_gestion_{tipo_catalogo}"
                ):
                    st.markdown(
                        f'<div class="gestion-catalog-table-header">'
                        f"{escape(titulo_tabla)}</div>",
                        unsafe_allow_html=True,
                    )
                if elementos:
                    for elemento in elementos:
                        clave_elemento = hashlib.sha256(
                            elemento.casefold().encode("utf-8")
                        ).hexdigest()[:12]
                        clave_fila = (
                            f"fila_gestion_{tipo_catalogo}_{clave_elemento}"
                        )
                        seleccion_actual = st.session_state.get(
                            "gestion_seleccion_activa"
                        )
                        seleccionado = (
                            seleccion_actual is not None
                            and seleccion_actual.get("tipo") == tipo_catalogo
                            and seleccion_actual.get("elemento") == elemento
                        )
                        if st.button(
                            elemento,
                            key=clave_fila,
                            type="primary" if seleccionado else "secondary",
                            use_container_width=True,
                        ):
                            if seleccionado:
                                st.session_state.pop(
                                    "gestion_seleccion_activa", None
                                )
                            else:
                                st.session_state["gestion_seleccion_activa"] = {
                                    "tipo": tipo_catalogo,
                                    "elemento": elemento,
                                }
                            st.rerun()
                else:
                    st.info("No hay elementos registrados.")

        if tipo_catalogo == "vehiculo" and len(elementos) <= 1:
            st.caption("Debe quedar al menos un tipo de vehículo disponible.")
        elif tipo_catalogo == "servicio" and len(elementos) <= 1:
            st.caption("Debe quedar al menos un servicio disponible.")

    st.stop()

if seccion == "Control":
    mostrar_titulo_seccion(
        seccion,
        "Registro de servicios y gastos para supervisión."
        if is_supervisor
        else None,
    )
    st.markdown("---")
    st.subheader(
        "Registro de Servicios y Gastos - Supervisor"
        if is_supervisor
        else "Registros de Servicios y Gastos"
    )
    if is_supervisor:
        col_fecha_ajustes, col_periodo_ajustes, _ = st.columns(
            [1.4, 1.1, 2.5]
        )
    else:
        col_fecha_ajustes, col_periodo_ajustes, col_responsable_ajustes = st.columns(
            [1, 1, 1.6]
        )
    with col_fecha_ajustes:
        fecha_registros_ajustes = seleccionar_fecha(
            "Fecha" if is_supervisor else "Ver Fecha",
            "fecha_registros_ajustes",
            datetime.now(ZONA_HORARIA_PERU).date(),
        )
    with col_periodo_ajustes:
        periodo_ajustes = st.selectbox(
            "Periodo", ["Día", "Mes"], key="periodo_ajustes"
        )
    if not is_supervisor:
        with col_responsable_ajustes:
            responsables_filtro = obtener_responsables_reporte(df_registros)
            responsable_ajustes = st.selectbox(
                "Responsable",
                ["Todos", *responsables_filtro],
                key="responsable_ajustes",
            )

    if periodo_ajustes == "Día":
        registros_periodo_ajustes = filtrar_registros_por_fecha(
            df_registros, fecha_registros_ajustes
        )
        registros_supervisor_ajustes = (
            consultar_dataframe(
                fecha=fecha_registros_ajustes,
                supervisor=st.session_state.usuario_actual,
            )
            if is_supervisor
            else None
        )
    else:
        registros_periodo_ajustes = filtrar_registros_por_mes(
            df_registros, fecha_registros_ajustes
        )
        registros_supervisor_ajustes = (
            consultar_dataframe(
                mes=fecha_registros_ajustes,
                supervisor=st.session_state.usuario_actual,
            )
            if is_supervisor
            else None
        )

    if is_supervisor:
        registros_ajustes = registros_supervisor_ajustes
    else:
        conteos_responsables = (
            registros_periodo_ajustes["Registrado por"]
            .map(normalizar_responsable)
            .value_counts()
        )
        registros_ajustes = registros_periodo_ajustes.copy()
        if responsable_ajustes != "Todos":
            registros_ajustes = registros_ajustes.loc[
                registros_ajustes["Registrado por"].map(normalizar_responsable)
                == responsable_ajustes
            ].copy()

    etiqueta_periodo_ajustes = (
        fecha_registros_ajustes.strftime("%d/%m/%Y")
        if periodo_ajustes == "Día"
        else fecha_registros_ajustes.strftime("%m/%Y")
    )
    if not is_supervisor:
        tarjetas_responsables = []
        for responsable in obtener_responsables_reporte(registros_periodo_ajustes):
            if responsable == "Administrador":
                icono, clase = "admin_panel_settings", "administrador"
            else:
                icono, clase = "support_agent", "supervisor"
            conteo = int(conteos_responsables.get(responsable, 0))
            tarjetas_responsables.append(
                f'<div class="reportes-kpi-card {clase}">'
                f'<div class="reportes-kpi-acento"></div>'
                f'<div class="reportes-kpi-icono" aria-hidden="true">{icono}</div>'
                f'<div class="reportes-kpi-etiqueta">{escape(responsable)}</div>'
                f'<div class="reportes-kpi-valor">{conteo}</div>'
                '<div class="reportes-kpi-unidad">reportes</div>'
                '</div>'
            )
        tarjetas_responsables.append(
            '<div class="reportes-kpi-card total">'
            '<div class="reportes-kpi-acento"></div>'
            '<div class="reportes-kpi-icono" aria-hidden="true">summarize</div>'
            '<div class="reportes-kpi-etiqueta">Total registrado</div>'
            f'<div class="reportes-kpi-valor">{len(registros_periodo_ajustes)}</div>'
            '<div class="reportes-kpi-unidad">reportes</div>'
            '</div>'
        )
    if registros_ajustes.empty:
        st.info("No hay registros que coincidan con los filtros seleccionados.")
    else:
        registros_ajustes = registros_ajustes.copy()
        registros_ajustes["Registrado por"] = registros_ajustes[
            "Registrado por"
        ].replace("", "No disponible")
        st.dataframe(
            crear_tabla_estilizada(registros_ajustes, incluir_usuario=True),
            use_container_width=True,
            hide_index=True,
        )

    if is_supervisor:
        ingresos_supervisor, gastos_supervisor = desglosar_metodos_financieros(
            registros_ajustes
        )
        clases_metodo_supervisor = {
            "efectivo": "efectivo",
            "yape": "yape",
            "plin": "plin",
            "tarjeta": "tarjeta",
            "transferencia": "transferencia",
        }
        periodo_resumen_supervisor = (
            f"{periodo_ajustes} · {etiqueta_periodo_ajustes}"
        )
        tarjetas_ingresos_supervisor = "".join(
            '<div class="reportes-kpi-card supervisor '
            'finanzas-metodo-card finanzas-metodo-'
            f'{clases_metodo_supervisor.get(metodo.casefold(), "otros")}">'
            '<div class="reportes-kpi-acento"></div>'
            '<div class="reportes-kpi-icono" aria-hidden="true">payments</div>'
            f'<div class="reportes-kpi-etiqueta">{escape(metodo)}</div>'
            f'<div class="reportes-kpi-valor">S/ {monto:.2f}</div>'
            '<div class="reportes-kpi-unidad">ingresos</div></div>'
            for metodo, monto in ingresos_supervisor.items()
        )
        tarjetas_gastos_supervisor = "".join(
            '<div class="reportes-kpi-card gasto '
            'finanzas-metodo-card finanzas-metodo-'
            f'{clases_metodo_supervisor.get(metodo.casefold(), "otros")}">'
            '<div class="reportes-kpi-acento"></div>'
            '<div class="reportes-kpi-icono" aria-hidden="true">'
            'account_balance_wallet</div>'
            f'<div class="reportes-kpi-etiqueta">{escape(metodo)}</div>'
            f'<div class="reportes-kpi-valor">S/ {monto:.2f}</div>'
            '<div class="reportes-kpi-unidad">gastos</div></div>'
            for metodo, monto in gastos_supervisor.items()
        )
        tarjetas_balance_supervisor = (
            '<div class="finanzas-balance-card">'
            '<div class="finanzas-balance-etiqueta">Ingresos totales del período</div>'
            f'<div class="finanzas-balance-valor">S/ '
            f'{sum(ingresos_supervisor.values()):.2f}</div></div>'
            '<div class="finanzas-balance-card gasto">'
            '<div class="finanzas-balance-etiqueta">Gastos totales del período</div>'
            f'<div class="finanzas-balance-valor">S/ '
            f'{sum(gastos_supervisor.values()):.2f}</div></div>'
        )
        st.markdown(
            '<section class="reportes-kpi-panel">'
            '<div class="reportes-kpi-header">'
            '<span class="reportes-kpi-heading">Balance del período</span>'
            f'<span class="reportes-kpi-periodo">{periodo_resumen_supervisor}</span>'
            '</div>'
            f'<div class="finanzas-balance-grid">{tarjetas_balance_supervisor}</div>'
            '</section>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<section class="reportes-kpi-panel">'
            '<div class="reportes-kpi-header">'
            '<span class="reportes-kpi-heading">Ingresos por método de pago</span>'
            f'<span class="reportes-kpi-periodo">{periodo_resumen_supervisor}</span>'
            '</div>'
            f'<div class="finanzas-kpi-grid">{tarjetas_ingresos_supervisor}</div>'
            '</section>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<section class="reportes-kpi-panel">'
            '<div class="reportes-kpi-header">'
            '<span class="reportes-kpi-heading">Gastos por método de pago</span>'
            f'<span class="reportes-kpi-periodo">{periodo_resumen_supervisor}</span>'
            '</div>'
            f'<div class="finanzas-kpi-grid">{tarjetas_gastos_supervisor}</div>'
            '</section>',
            unsafe_allow_html=True,
        )
        st.stop()

    registros_mes = filtrar_registros_por_mes(
        df_registros, fecha_registros_ajustes
    )
    registros_dia = filtrar_registros_por_fecha(
        df_registros, fecha_registros_ajustes
    )
    if responsable_ajustes != "Todos":
        registros_mes = registros_mes.loc[
            registros_mes["Registrado por"].map(normalizar_responsable)
            == responsable_ajustes
        ].copy()
        registros_dia = registros_dia.loc[
            registros_dia["Registrado por"].map(normalizar_responsable)
            == responsable_ajustes
        ].copy()

    etiqueta_responsable_exportacion = (
        f" del {responsable_ajustes}"
        if responsable_ajustes != "Todos"
        else ""
    )
    col_exportar_dia, col_exportar_mes = st.columns(2)
    with col_exportar_dia:
        st.download_button(
            (
                f"Exportar Excel{etiqueta_responsable_exportacion} (Día)"
                if responsable_ajustes != "Todos"
                else "Exportar Excel por Día"
            ),
            data=generar_excel_registros(registros_dia),
            file_name=f"registros_{fecha_registros_ajustes:%Y-%m-%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="exportar_excel_ajustes_dia",
            disabled=registros_dia.empty,
            use_container_width=True,
        )
    with col_exportar_mes:
        st.download_button(
            (
                f"Exportar Excel{etiqueta_responsable_exportacion} (Mes)"
                if responsable_ajustes != "Todos"
                else "Exportar Excel por Mes"
            ),
            data=generar_excel_registros(registros_mes),
            file_name=f"registros_{fecha_registros_ajustes:%Y-%m}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="exportar_excel_ajustes_mes",
            disabled=registros_mes.empty,
            use_container_width=True,
        )

    st.markdown(
        f'<section class="reportes-kpi-panel">'
        f'<div class="reportes-kpi-header">'
        f'<span class="reportes-kpi-heading">Reportes por responsable</span>'
        f'<span class="reportes-kpi-periodo">{periodo_ajustes} · '
        f'{etiqueta_periodo_ajustes}</span>'
        f'</div><div class="reportes-kpi-grid">'
        f'{"".join(tarjetas_responsables)}'
        '</div></section>',
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <style>
        .st-key-gestion_usuarios_panel {
            padding: 1.25rem;
            border: 1px solid rgba(173, 190, 174, 0.2);
            border-radius: 14px;
            background: rgba(13, 19, 15, 0.72);
            backdrop-filter: blur(14px);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04),
                0 12px 32px rgba(0, 0, 0, 0.22);
        }
        .st-key-gestion_usuarios_panel h3 {
            color: #f1f5ef;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.container(key="gestion_usuarios_panel"):
        st.subheader("Gestión y Control de Usuarios")
        st.caption("Consulta las cuentas registradas y administra los accesos al sistema.")

        mensaje_usuarios = st.session_state.pop("mensaje_gestion_usuarios", None)
        if mensaje_usuarios:
            st.success(mensaje_usuarios)
        usuarios_activos = [
            (nombre, cuenta)
            for nombre, cuenta in usuarios.items()
            if cuenta["activo"]
        ]
        usuarios_activos.sort(
            key=lambda elemento: (
                elemento[1]["rol"] != "Administrador",
                elemento[1]["rol"],
                elemento[0].casefold(),
            )
        )
        tabla_usuarios = pd.DataFrame(
            [
                {
                    "Nombre de usuario": nombre,
                    "Rol": cuenta["rol"],
                    "Último inicio de sesión": (
                        datetime.fromisoformat(cuenta["ultimo_inicio_sesion"]).strftime(
                            "%d/%m/%Y %H:%M"
                        )
                        if cuenta["ultimo_inicio_sesion"]
                        else "Sin registros"
                    ),
                }
                for nombre, cuenta in usuarios_activos
            ]
        )
        st.dataframe(
            tabla_usuarios,
            use_container_width=True,
            hide_index=True,
        )

        supervisores_inactivos = [
            nombre
            for nombre, cuenta in usuarios.items()
            if not cuenta["activo"] and cuenta["rol"] == "Supervisor"
        ]
        if supervisores_inactivos:
            st.markdown("### Supervisores dados de baja")
            st.caption(
                "Elimina definitivamente una cuenta y todos sus registros asociados."
            )
            st.markdown(
                """
                <style>
                [class*="st-key-supervisor_baja_"] [data-testid="stBaseButton-secondary"] {
                    width: 2.35rem;
                    min-width: 2.35rem;
                    height: 2.35rem;
                    padding: 0;
                    border: 0;
                    border-radius: 50%;
                    background: #c62828;
                    color: #ffffff;
                    font-size: 1.25rem;
                    font-weight: 700;
                }
                [class*="st-key-supervisor_baja_"] [data-testid="stBaseButton-secondary"]:hover {
                    border: 0;
                    background: #a61f1f;
                    color: #ffffff;
                }
                </style>
                """,
                unsafe_allow_html=True,
            )
            columnas_supervisores = st.columns(3)
            for indice, nombre in enumerate(supervisores_inactivos):
                clave = hashlib.sha256(
                    nombre.casefold().encode("utf-8")
                ).hexdigest()[:12]
                with columnas_supervisores[indice % len(columnas_supervisores)]:
                    with st.container(key=f"supervisor_baja_{clave}", border=True):
                        col_nombre, col_eliminar = st.columns([5, 1])
                        with col_nombre:
                            st.markdown(f"**{escape(nombre)}**")
                        with col_eliminar:
                            if st.button(
                                "×",
                                key=f"eliminar_supervisor_{clave}",
                                help="Eliminar definitivamente los registros del supervisor",
                                type="secondary",
                            ):
                                mostrar_confirmacion_eliminar_supervisor(nombre)
                        st.caption("Cuenta dada de baja")

        col_baja, col_editar, col_nuevo = st.columns([1.3, 1, 1])
        with col_baja:
            if st.button(
                "Dar de baja una cuenta",
                key="abrir_formulario_baja_usuario",
                type="secondary",
                use_container_width=True,
            ):
                mostrar_formulario_baja_usuario()

        with col_editar:
            if st.button(
                "Editar usuario",
                key="abrir_formulario_editar_usuario",
                type="secondary",
                use_container_width=True,
            ):
                mostrar_formulario_editar_usuario()

        with col_nuevo:
            if st.button(
                "Agregar Nuevo Usuario",
                key="abrir_formulario_nuevo_usuario",
                type="primary",
                use_container_width=True,
            ):
                mostrar_formulario_nuevo_usuario()

    st.stop()

with st.container(key="registros_hero"):
    st.markdown('<div id="registros-theme-marker"></div>', unsafe_allow_html=True)
    _, logo_columna = st.columns([8, 2])
    with logo_columna:
        st.markdown(
            f'<div class="main-header-logo"><img src="data:image/png;base64,{logo_base64}" alt="The Bunker Car Wash"></div>',
            unsafe_allow_html=True,
        )
    columnas_banner = st.columns([1, 3])
    col_fecha = columnas_banner[0]
    with col_fecha:
        fecha_ver = seleccionar_fecha(
            "Ver Fecha",
            "ver_fecha_registros",
            datetime.now(ZONA_HORARIA_PERU).date(),
            on_change=cambiar_fecha_tabla,
        )
mostrar_titulo_seccion("Registros")
st.caption(f"Fecha: {fecha_ver.strftime('%d/%m/%Y')}")

# --- TABLA DE REGISTROS (PROCESADA PRIMERO PARA CARGAR LA SELECCIÓN) ---
df_registros_fecha = consultar_dataframe(
    fecha=fecha_ver,
)
df_filtrado = df_registros_fecha.copy()
df_exportacion = df_registros_fecha.copy()

if is_admin:
    st.subheader("Selecciona una fila para editar")

    with st.container(border=True):
        col_f1, col_f2, col_f3 = st.columns(3)
        with col_f1:
            busqueda_placa = st.text_input(
                "Buscar por Placa",
                key="busqueda_placa_input",
                on_change=reiniciar_seleccion_tabla,
            )
        with col_f2:
            filtro_pago = st.selectbox(
                "Filtrar por Método de Pago",
                ["Todos", *lista_pagos, "Yape/Plin"],
                key="filtro_pago_select",
                on_change=reiniciar_seleccion_tabla,
            )
        with col_f3:
            filtro_responsable = st.selectbox(
                "Filtrar por Responsable",
                [
                    "Todos",
                    "Supervisor 1",
                    "Supervisor 2",
                    "Supervisor 3",
                    "Supervisor 4",
                ],
                key="filtro_responsable_registros",
                on_change=reiniciar_seleccion_tabla,
            )
        
    if busqueda_placa:
        df_filtrado = df_filtrado[df_filtrado['Placa'].astype(str).str.contains(busqueda_placa, case=False, na=False)]
    if filtro_pago != "Todos":
        df_filtrado = df_filtrado[df_filtrado['Pago'] == filtro_pago]
    if filtro_responsable != "Todos":
        responsables = (
            df_filtrado["Registrado por"]
            .map(normalizar_responsable)
            .astype(str)
            .str.replace(r"[\s_-]+", "", regex=True)
            .str.casefold()
        )
        responsable_seleccionado = re.sub(
            r"[\s_-]+", "", filtro_responsable
        ).casefold()
        df_filtrado = df_filtrado.loc[
            responsables == responsable_seleccionado
        ]
    df_exportacion = df_filtrado.copy()

    clave_tabla = f"tabla_admin_{st.session_state.revision_tabla_fecha}"
    st.session_state.tabla_admin_key_activa = clave_tabla
    filas_seleccionadas = st.session_state.get(f"filas_{clave_tabla}", set())

    with st.container(border=True):
        evento_tabla = st.dataframe(
            crear_tabla_estilizada(
                df_filtrado,
                filas_seleccionadas,
                incluir_usuario=True,
            ),
            use_container_width=True,
            column_order=obtener_columnas_visibles_registros(
                df_filtrado,
                incluir_usuario=True,
            ),
            selection_mode="multi-row",
            on_select=guardar_seleccion_tabla,
            key=clave_tabla,
            hide_index=True,
        )
    
    seleccion_tabla = (
        evento_tabla.get("selection", {})
        if evento_tabla is not None
        else {}
    )
    filas_seleccionadas = seleccion_tabla.get("rows", [])
    ids_seleccionados = [
        df_filtrado.iloc[indice]["ID"]
        for indice in filas_seleccionadas
        if 0 <= indice < len(df_filtrado)
    ]
    if len(ids_seleccionados) == 1:
        indice_seleccionado = filas_seleccionadas[0]
        cargar_fila_en_formulario(df_filtrado.iloc[indice_seleccionado])
    elif len(ids_seleccionados) >= 2:
        st.session_state.edit_id = None
        st.session_state.registro_cargado_id = None
        restablecer_campos_formulario()
    elif st.session_state.edit_id:
        st.session_state.edit_id = None
        st.session_state.registro_cargado_id = None
        restablecer_campos_formulario()
    else:
        st.session_state.registro_cargado_id = None

    if df_filtrado.empty:
        if df_registros_fecha.empty:
            st.info("No hay registros para la fecha seleccionada.")
        else:
            st.info("No hay movimientos que coincidan con los filtros.")

if not is_admin and not df_registros_fecha.empty:
    with st.container(border=True):
        st.dataframe(
            crear_tabla_estilizada(
                df_registros_fecha,
                incluir_usuario=True,
            ),
            use_container_width=True,
            column_order=obtener_columnas_visibles_registros(
                df_registros_fecha,
                incluir_usuario=True,
            ),
            hide_index=True,
            key="tabla_registros_supervisor",
        )
elif not is_admin:
    st.info(f"No hay registros para el {fecha_ver.strftime('%d/%m/%Y')}.")

st.download_button(
    "Exportar a Excel",
    data=generar_excel_registros(df_exportacion),
    file_name=(
        f"registros_{fecha_ver.strftime('%Y-%m-%d')}_bunker.xlsx"
    ),
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    key="descargar_registros_excel",
    type="primary",
    use_container_width=True,
)

if is_admin and len(ids_seleccionados) >= 2:
    st.markdown("---")
    st.subheader(f"{len(ids_seleccionados)} registros seleccionados")
    with st.container(border=True):
        placas_seleccionadas = [
            str(df_filtrado.iloc[indice]["Placa"])
            for indice in filas_seleccionadas
            if 0 <= indice < len(df_filtrado)
        ]
        st.info(
            "Modifique la fecha o elimine los registros de las placas: "
            + ", ".join(placas_seleccionadas)
        )
        fecha_nueva = st.date_input(
            "Nueva fecha",
            value=fecha_ver,
            key="fecha_cambio_seleccionados",
        )
        if st.button(
            "Actualizar fecha",
            key="cambiar_fecha_seleccionados",
            type="primary",
            use_container_width=True,
        ):
            try:
                actualizados = actualizar_fecha_registros(
                    ids_seleccionados,
                    fecha_nueva,
                )
            except (psycopg2.Error, ValueError) as error:
                st.error(f"No se pudo cambiar la fecha: {error}")
            else:
                refrescar_registros_sesion()
                reiniciar_seleccion_tabla()
                notificar_exito(
                    f"Se cambió la fecha de {actualizados} registros."
                )
                st.rerun()

    if st.button(
        f"Eliminar {len(ids_seleccionados)} registros seleccionados",
        key="eliminar_registros_seleccionados",
        type="primary",
        use_container_width=True,
    ):
        try:
            eliminados = eliminar_registros(ids_seleccionados)
        except (psycopg2.Error, ValueError) as error:
            st.error(f"No se pudieron eliminar los registros: {error}")
        else:
            if eliminados:
                refrescar_registros_sesion()
                reiniciar_seleccion_tabla()
                limpiar_formulario()
                notificar_exito(
                    f"Se eliminaron {eliminados} registros.",
                    "eliminacion",
                )
                st.rerun()
            else:
                st.error("No se encontró ningún registro seleccionado.")
    st.stop()

st.markdown("---")

# --- FORMULARIO DE REGISTRO / ACTUALIZACIÓN ---
titulo_form = "Actualizar Registro Seleccionado" if st.session_state.edit_id else "Registrar Nuevo Servicio / Gasto"
st.subheader(titulo_form)

if st.session_state.edit_id:
    st.info(
        f"Modificando el registro de la placa: "
        f"**{st.session_state.input_placa}**. "
        "Modifica los campos y haz clic en Guardar Cambios."
    )
    st.date_input(
        "Fecha",
        key="fecha_edicion_registro",
    )

with st.container(border=True):
    st.markdown("### Vehículo y Pago")
    c1, c2, c3, c4, c5 = st.columns(5)
    opciones_pago_formulario = list(lista_pagos)
    if (
        st.session_state.edit_id
        and st.session_state.input_pago == "Yape/Plin"
    ):
        opciones_pago_formulario.append("Yape/Plin")
    
    with c1:
        st.selectbox("Tipo de Vehículo", lista_tipos, key="input_tipo")
    with c2:
        st.text_input(
            "Placa / Identificador",
            placeholder="Ej. ABC-123",
            key="input_placa",
        )
    with c3:
        st.selectbox(
            "Servicio",
            lista_servicios,
            key="input_servicio",
            accept_new_options=True,
        )
    with c4:
        st.text_input(
            "Monto Cobrado (S/)",
            placeholder="Ej. 25.00",
            key="input_monto",
        )
    with c5:
        st.selectbox(
            "Forma de Pago",
            opciones_pago_formulario,
            key="input_pago",
        )
        
with st.container(border=True):
    st.markdown("### Cliente")
    cliente_ruc, cliente_razon_social, cliente_numero, cliente_correo = st.columns(4)
    with cliente_ruc:
        st.text_input(
            "RUC/DNI (opcional)",
            placeholder="Ej. 12345678",
            key="input_ruc_cliente",
        )
    with cliente_razon_social:
        st.text_input(
            "Razón Social/Nombre (opcional)",
            placeholder="Nombre del cliente",
            key="input_razon_social_cliente",
        )
    with cliente_numero:
        st.text_input(
            "Número Cliente (opcional)",
            placeholder="Ej. 999 999 999",
            key="input_numero_cliente",
        )
    with cliente_correo:
        st.text_input(
            "Correo Cliente (opcional)",
            placeholder="cliente@ejemplo.com",
            key="input_correo_cliente",
        )

with st.container(border=True):
    st.markdown("### Lavadores")
    l1, l2, l3 = st.columns(3)
    with l1:
        st.selectbox("Lavador 1", l1_opts, key="input_l1")
    with l2:
        st.selectbox("Lavador 2", l2_opts, key="input_l2")
    with l3:
        st.selectbox("Lavador 3", l3_opts, key="input_l3")
        
with st.container(border=True):
    st.markdown("### Gastos")
    g1, g2, g3 = st.columns(3)
    with g1:
        opciones_motivo_formulario = list(OPCIONES_MOTIVO_GASTO)
        for motivo_personalizado in st.session_state.motivos_gasto_personalizados:
            if motivo_personalizado not in opciones_motivo_formulario:
                opciones_motivo_formulario.append(motivo_personalizado)
        motivo_actual = str(st.session_state.input_motivo or "-").strip() or "-"
        if motivo_actual not in opciones_motivo_formulario:
            opciones_motivo_formulario.append(motivo_actual)
            if motivo_actual != "-" and not es_motivo_adelanto(motivo_actual):
                st.session_state.motivos_gasto_personalizados.append(motivo_actual)

        st.text_input(
            "Motivo Gasto (opcional)",
            key="input_motivo",
            placeholder="Escribe un motivo o selecciona uno abajo",
        )
        indice_sugerencia = (
            opciones_motivo_formulario.index(motivo_actual)
            if motivo_actual in opciones_motivo_formulario
            else 0
        )
        seleccion_sugerida = st.selectbox(
            "Seleccionar motivo",
            opciones_motivo_formulario,
            index=indice_sugerencia,
            key="input_motivo_sugerencia",
            on_change=seleccionar_motivo_sugerencia,
        )
        if es_motivo_adelanto(st.session_state.input_motivo):
            opciones_lavadores_adelanto = ["-", *lista_lavadores]
            if (
                st.session_state.input_lavador_adelanto
                not in opciones_lavadores_adelanto
            ):
                st.session_state.input_lavador_adelanto = "-"
            st.selectbox(
                "Seleccionar Lavador",
                opciones_lavadores_adelanto,
                key="input_lavador_adelanto",
            )
    with g2:
        st.text_input(
            "Precio Gasto (opcional)",
            placeholder="Ej. 10.00",
            key="input_precio_gasto",
        )
    with g3:
        st.selectbox(
            "Método Gasto",
            ["-", *lista_pagos],
            key="input_metodo_gasto",
        )

col_acc1, col_acc2 = st.columns(2)
with col_acc1:
    texto_boton = "Guardar Cambios de Actualización" if st.session_state.edit_id else "Guardar Registro"
    btn_guardar = st.button(
        texto_boton, key="guardar_registro_btn", use_container_width=True, type="primary"
    )
with col_acc2:
    if st.session_state.edit_id:
        btn_eliminar = st.button(
            "Eliminar este Registro", key="eliminar_registro_btn", type="secondary",
            use_container_width=True
        )
    else:
        btn_eliminar = False

mostrar_notificacion_exito()

# Lógica de Guardar / Actualizar
if btn_guardar:
    campos_obligatorios = {
        "Tipo de Vehículo": st.session_state.input_tipo,
        "Placa / Identificador": st.session_state.input_placa,
        "Servicio": st.session_state.input_servicio,
        "Monto Cobrado (S/)": st.session_state.input_monto,
        "Forma de Pago": st.session_state.input_pago,
    }
    campos_vacios = [
        nombre
        for nombre, valor in campos_obligatorios.items()
        if valor is None or not str(valor).strip() or str(valor).strip() == "-"
    ]
    m_val = str(st.session_state.input_monto).strip()
    pg_val = str(st.session_state.input_precio_gasto).strip()
    errores_validacion = []

    if campos_vacios:
        errores_validacion.append(
            "Completa los campos obligatorios: " + ", ".join(campos_vacios) + "."
        )
    if m_val and not re.fullmatch(r"\d+(?:\.\d+)?", m_val):
        errores_validacion.append(
            "Monto Cobrado solo acepta números y un punto decimal."
        )
    if pg_val not in {"", "-"} and not re.fullmatch(r"\d+(?:\.\d+)?", pg_val):
        errores_validacion.append(
            "Precio Gasto solo acepta números y un punto decimal."
        )
    errores_validacion.extend(
        validar_campos_gasto(
            pg_val,
            st.session_state.input_metodo_gasto,
        )
    )
    lavador_1 = str(st.session_state.input_l1).strip()
    lavador_2 = str(st.session_state.input_l2).strip()
    lavador_3 = str(st.session_state.input_l3).strip()
    lavadores_seleccionados = [
        lavador
        for lavador in (lavador_1, lavador_2, lavador_3)
        if lavador != "-"
    ]
    if not lavadores_seleccionados:
        errores_validacion.append("Selecciona al menos un lavador para el servicio.")
    elif len({lavador.casefold() for lavador in lavadores_seleccionados}) < len(
        lavadores_seleccionados
    ):
        errores_validacion.append(
            "No puedes seleccionar el mismo lavador más de una vez."
        )

    if errores_validacion:
        st.error("No se pudo guardar:\n\n- " + "\n- ".join(errores_validacion))
    elif st.session_state.input_placa.strip() != "":
        monto_fmt = f"S/ {float(m_val):.2f}"
        motivo_ingresado = str(
            st.session_state.input_motivo or "-"
        ).strip()
        if (
            motivo_ingresado
            and motivo_ingresado != "-"
            and motivo_ingresado not in OPCIONES_MOTIVO_GASTO
            and motivo_ingresado not in st.session_state.motivos_gasto_personalizados
        ):
            st.session_state.motivos_gasto_personalizados.append(
                motivo_ingresado
            )
        motivo_gasto_fmt = formatear_motivo_gasto(
            st.session_state.input_motivo,
            st.session_state.input_lavador_adelanto,
        )
        gasto_fmt = (
            f"S/ {float(pg_val):.2f}"
            if pg_val not in {"", "-"}
            else "-"
        )
        
        if st.session_state.edit_id:
            cambios_registro = {
                "Tipo": st.session_state.input_tipo,
                "Placa": st.session_state.input_placa.upper(),
                "Servicio": st.session_state.input_servicio,
                "Pago": st.session_state.input_pago,
                "Monto": monto_fmt,
                "Fecha": (
                    datetime.combine(
                        st.session_state.fecha_edicion_registro,
                        st.session_state.fecha_edicion_hora_original.time(),
                    ).strftime("%Y/%m/%d %H:%M:%S")
                    if st.session_state.fecha_edicion_hora_original
                    else st.session_state.fecha_edicion_registro.strftime(
                        "%Y/%m/%d"
                    )
                ),
                "Lavador 1": lavador_1 or "-",
                "Lavador 2": lavador_2 or "-",
                "Lavador 3": lavador_3 or "-",
                "Motivo Gasto": motivo_gasto_fmt,
                "Precio Gasto": gasto_fmt,
                "Método Gasto": st.session_state.input_metodo_gasto or "-",
                "RUC/DNI": st.session_state.input_ruc_cliente.strip() or "-",
                "Razón Social/Nombre": (
                    st.session_state.input_razon_social_cliente.strip() or "-"
                ),
                "Número Cliente": st.session_state.input_numero_cliente.strip() or "-",
                "Correo Cliente": st.session_state.input_correo_cliente.strip() or "-",
            }
            try:
                actualizado = actualizar_registro(
                    st.session_state.edit_id,
                    cambios_registro,
                )
            except (psycopg2.Error, ValueError) as error:
                st.error(f"No se pudo actualizar el registro: {error}")
            else:
                if actualizado:
                    refrescar_registros_sesion()
                    limpiar_formulario()
                    notificar_exito("¡Se actualizó correctamente!")
                    st.rerun()
                else:
                    st.error("No se encontró el registro a actualizar.")
        else:
            ahora_registro = datetime.now(ZONA_HORARIA_PERU)
            fecha_registro = (
                f"{fecha_ver:%Y/%m/%d} {ahora_registro:%H:%M}"
            )
            nuevo_registro = {
                "Tipo": st.session_state.input_tipo,
                "Placa": st.session_state.input_placa.upper(),
                "Servicio": st.session_state.input_servicio,
                "Pago": st.session_state.input_pago,
                "Monto": monto_fmt,
                "Fecha": fecha_registro,
                "Lavador 1": st.session_state.input_l1,
                "Lavador 2": st.session_state.input_l2,
                "Lavador 3": st.session_state.input_l3,
                "Motivo Gasto": motivo_gasto_fmt,
                "Precio Gasto": gasto_fmt,
                "Método Gasto": st.session_state.input_metodo_gasto or "-",
                NOMBRE_COLUMNA_LOCAL: "Búnker 1",
                "RUC/DNI": (
                    st.session_state.input_ruc_cliente.strip() or "-"
                ),
                "Razón Social/Nombre": (
                    st.session_state.input_razon_social_cliente.strip() or "-"
                ),
                "Número Cliente": (
                    st.session_state.input_numero_cliente.strip() or "-"
                ),
                "Correo Cliente": (
                    st.session_state.input_correo_cliente.strip() or "-"
                ),
                "Registrado por": st.session_state.usuario_actual,
            }
            try:
                insertar_registro(nuevo_registro)
            except (psycopg2.Error, ValueError) as error:
                st.error(f"No se pudo guardar el registro: {error}")
            else:
                refrescar_registros_sesion()
                limpiar_formulario()
                notificar_exito("¡Se registró correctamente!")
                st.rerun()
    else:
        st.warning("Por favor, ingresa al menos la placa o identificador.")

if btn_eliminar and st.session_state.edit_id:
    try:
        eliminados = eliminar_registros(ids_seleccionados)
    except (psycopg2.Error, ValueError) as error:
        st.error(f"No se pudo eliminar el registro: {error}")
    else:
        if eliminados == 1:
            refrescar_registros_sesion()
            reiniciar_seleccion_tabla()
            limpiar_formulario()
            notificar_exito(
                "El registro se eliminó correctamente.", "eliminacion"
            )
            st.rerun()
        else:
            st.error("No se encontró el registro a eliminar.")
