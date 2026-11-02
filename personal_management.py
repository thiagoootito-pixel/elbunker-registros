import calendar
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from html import escape
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import psycopg2
import streamlit as st

from database import (
    consultar_asistencias_personal,
    consultar_asistencias_personal_fecha,
    consultar_asistencias_personal_periodo,
    consultar_detalle_adelantos_registros_personal,
    consultar_detalle_servicios_personal,
    consultar_historial_personal,
    consultar_liquidacion_personal_periodo,
    consultar_liquidaciones_personal,
    consultar_pagos_personal,
    consultar_perfiles_personal,
    consultar_resumen_personal_periodo,
    consultar_resumen_adelantos_registros_personal,
    consultar_resumen_servicios_personal,
    crear_liquidacion_personal,
    guardar_asistencias_personal,
    guardar_perfil_personal,
    registrar_pago_personal,
    revertir_pagos_liquidacion_personal,
)

_PERSONAL_QUERY_CACHE_TTL = 300
_PERSONAL_QUERY_SCHEMA_VERSION = 5


def _fecha_actual_personal():
    return datetime.now(ZoneInfo("America/Lima")).date()


_CONSULTAS_LECTURA_PERSONAL = {
    consulta.__name__: consulta
    for consulta in (
        consultar_asistencias_personal,
        consultar_asistencias_personal_fecha,
        consultar_asistencias_personal_periodo,
        consultar_detalle_adelantos_registros_personal,
        consultar_detalle_servicios_personal,
        consultar_historial_personal,
        consultar_liquidacion_personal_periodo,
        consultar_liquidaciones_personal,
        consultar_pagos_personal,
        consultar_perfiles_personal,
        consultar_resumen_personal_periodo,
        consultar_resumen_adelantos_registros_personal,
        consultar_resumen_servicios_personal,
    )
}


@st.cache_resource
def _ejecutor_precarga_personal():
    return ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="precarga-personal",
    )


@st.cache_data(ttl=300, show_spinner=False)
def _consultar_personal_cache_global(
    nombre_consulta, argumentos, cache_version=1
):
    return _CONSULTAS_LECTURA_PERSONAL[nombre_consulta](*argumentos)


def _consultar_personal_cacheado(
    consulta, *argumentos, cargar=True, cache_version=1
):
    cache = st.session_state.setdefault(
        "_gestion_personal_consultas_cache", {}
    )
    argumentos_clave = tuple(
        tuple(argumento) if isinstance(argumento, list) else argumento
        for argumento in argumentos
    )
    clave = (consulta.__name__, cache_version, *argumentos_clave)
    ahora = time.monotonic()
    entrada = cache.get(clave)
    if entrada and ahora - entrada[0] < _PERSONAL_QUERY_CACHE_TTL:
        return entrada[1]
    if not cargar:
        return None
    resultado = _consultar_personal_cache_global(
        consulta.__name__, argumentos_clave, cache_version
    )
    cache[clave] = (time.monotonic(), resultado)
    return resultado


def invalidar_cache_gestion_personal():
    st.session_state.pop("_gestion_personal_consultas_cache", None)
    st.session_state.pop("_gestion_personal_prefetch", None)
    st.session_state.pop("_gestion_personal_prefetch_future", None)
    _consultar_personal_cache_global.clear()


def _invalidar_consultas_personal():
    invalidar_cache_gestion_personal()


def _cargar_precarga_personal(nombres, inicio, fin, referencia, alcance):
    with ThreadPoolExecutor(max_workers=4) as executor:
        futuro_perfiles = executor.submit(
            _consultar_personal_cache_global,
            consultar_perfiles_personal.__name__,
            (nombres,),
            _PERSONAL_QUERY_SCHEMA_VERSION,
        )
        futuro_asistencias = executor.submit(
            _consultar_personal_cache_global,
            consultar_asistencias_personal_periodo.__name__,
            (inicio, fin),
            _PERSONAL_QUERY_SCHEMA_VERSION,
        )
        futuro_servicios = executor.submit(
            _consultar_personal_cache_global,
            consultar_resumen_servicios_personal.__name__,
            (inicio, fin),
        )
        futuro_adelantos = executor.submit(
            _consultar_personal_cache_global,
            consultar_resumen_adelantos_registros_personal.__name__,
            (inicio, fin),
        )
        perfiles = futuro_perfiles.result()
        asistencias = futuro_asistencias.result()
        resumen_servicios = futuro_servicios.result()
        resumen_adelantos = futuro_adelantos.result()

    periodos_por_modalidad = {
        modalidad: _periodo_liquidacion(modalidad, referencia)
        for modalidad in ("Diario", "Quincenal", "Mensual", "Semanal")
    }
    modalidades_activas = {
        perfil["modalidad_pago"]
        for perfil in perfiles
        if perfil["activo"]
        and perfil["modalidad_pago"] in periodos_por_modalidad
    }
    with ThreadPoolExecutor(
        max_workers=min(4, len(modalidades_activas) or 1)
    ) as executor:
        futuros_resumenes = {
            modalidad: executor.submit(
                _consultar_personal_cache_global,
                consultar_resumen_personal_periodo.__name__,
                periodos_por_modalidad[modalidad],
                _PERSONAL_QUERY_SCHEMA_VERSION,
            )
            for modalidad in modalidades_activas
        }
        resumenes_por_modalidad = {
            modalidad: futuro.result()
            for modalidad, futuro in futuros_resumenes.items()
        }
    return {
        "cargado": True,
        "alcance": alcance,
        "cargado_en": time.monotonic(),
        "lavadores": nombres,
        "perfiles": pd.DataFrame.from_records(perfiles),
        "asistencias": pd.DataFrame.from_records(
            asistencias,
            columns=[
                "trabajador_id",
                "nombre",
                "fecha",
                "estado",
                "observacion",
                "registrado_por",
                "fecha_hora_registro",
            ],
        ),
        "periodo_asistencias": (inicio, fin),
        "resumenes_por_modalidad": resumenes_por_modalidad,
        "resumen_servicios": pd.DataFrame.from_records(resumen_servicios),
        "resumen_adelantos": pd.DataFrame.from_records(resumen_adelantos),
    }


def precargar_datos_gestion_personal(nombres_trabajadores, referencia):
    inicio = referencia.replace(day=1)
    fin = referencia.replace(
        day=calendar.monthrange(referencia.year, referencia.month)[1]
    )
    nombres = tuple(str(nombre).strip() for nombre in nombres_trabajadores)
    alcance = (
        st.session_state.get("usuario_actual"),
        st.session_state.get("rol_usuario"),
        nombres,
        inicio,
        fin,
        _PERSONAL_QUERY_SCHEMA_VERSION,
    )
    existente = st.session_state.get("_gestion_personal_prefetch")
    if (
        existente
        and existente["alcance"] == alcance
        and time.monotonic() - existente["cargado_en"]
        < _PERSONAL_QUERY_CACHE_TTL
    ):
        return existente

    solicitud = st.session_state.get(
        "_gestion_personal_prefetch_future"
    )
    if solicitud and solicitud["alcance"] == alcance:
        futuro = solicitud["futuro"]
        if not futuro.done():
            return None
        st.session_state.pop("_gestion_personal_prefetch_future", None)
        precarga = futuro.result()
    else:
        futuro: Future = _ejecutor_precarga_personal().submit(
            _cargar_precarga_personal,
            nombres,
            inicio,
            fin,
            referencia,
            alcance,
        )
        st.session_state["_gestion_personal_prefetch_future"] = {
            "alcance": alcance,
            "futuro": futuro,
        }
        return None

    st.session_state["_gestion_personal_prefetch"] = precarga
    return precarga


def _obtener_precarga_personal(nombres_trabajadores, referencia):
    precarga = st.session_state.get("_gestion_personal_prefetch")
    if not precarga:
        return None
    inicio = referencia.replace(day=1)
    fin = referencia.replace(
        day=calendar.monthrange(referencia.year, referencia.month)[1]
    )
    alcance = (
        st.session_state.get("usuario_actual"),
        st.session_state.get("rol_usuario"),
        tuple(str(nombre).strip() for nombre in nombres_trabajadores),
        inicio,
        fin,
        _PERSONAL_QUERY_SCHEMA_VERSION,
    )
    if (
        precarga["alcance"] != alcance
        or time.monotonic() - precarga["cargado_en"]
        >= _PERSONAL_QUERY_CACHE_TTL
    ):
        return None
    return precarga


def _obtener_asistencias_precargadas(
    fecha_inicio, fecha_fin, fecha=None, trabajador_id=None
):
    precarga = st.session_state.get("_gestion_personal_prefetch")
    if (
        not precarga
        or precarga.get("periodo_asistencias")
        != (fecha_inicio, fecha_fin)
        or "trabajador_id" not in precarga["asistencias"].columns
    ):
        return None
    asistencias = precarga["asistencias"]
    if fecha is not None:
        asistencias = asistencias.loc[asistencias["fecha"] == fecha]
    if trabajador_id is not None:
        asistencias = asistencias.loc[
            asistencias["trabajador_id"] == trabajador_id
        ]
    return asistencias.to_dict("records")


def _moneda(valor):
    if valor is None:
        return "—"
    return f"S/ {Decimal(str(valor)):.2f}"


def _mostrar_tarjetas_kpi_personal(tarjetas):
    clase_rejilla = (
        "gestion-personal-kpis analytics-kpis analytics-kpis-single"
        if len(tarjetas) == 1
        else "gestion-personal-kpis analytics-kpis"
    )
    contenido = "".join(
        '<div class="gestion-personal-kpi '
        f'{clase}"><div class="gestion-personal-kpi-icon">{escape(icono)}</div>'
        '<div class="gestion-personal-kpi-copy">'
        f'<div class="gestion-personal-kpi-label">{escape(etiqueta)}</div>'
        f'<div class="gestion-personal-kpi-value">{escape(str(valor))}</div>'
        "</div></div>"
        for clase, icono, etiqueta, valor in (
            tarjeta[:4] for tarjeta in tarjetas
        )
    )
    st.markdown(
        f'<div class="{clase_rejilla}">{contenido}</div>',
        unsafe_allow_html=True,
    )


def _tabla_importe_fecha_registro(movimientos):
    filas = []
    for movimiento in movimientos:
        fecha_registro = movimiento.get("fecha_registro")
        if (
            hasattr(fecha_registro, "astimezone")
            and fecha_registro.utcoffset() is not None
        ):
            fecha_registro = fecha_registro.astimezone(
                ZoneInfo("America/Lima")
            )
        fecha_texto = (
            fecha_registro.strftime("%d/%m/%Y %H:%M")
            if hasattr(fecha_registro, "strftime")
            else "—"
        )
        filas.append(
            "<tr>"
            '<td class="personal-movement-amount">'
            f"{escape(_moneda(movimiento.get('importe')))}</td>"
            '<td class="personal-movement-date">'
            f"{escape(fecha_texto)}</td>"
            "</tr>"
        )
    return (
        '<div class="gestion-personal-attendance-history personal-movement-history">'
        "<table><thead><tr><th>Monto</th><th>Fecha de registro</th>"
        "</tr></thead><tbody>"
        f'{"".join(filas)}'
        "</tbody></table></div>"
    )


def _formatear_fecha_hora_personal(valor):
    if isinstance(valor, datetime):
        if valor.utcoffset() is not None:
            valor = valor.astimezone(ZoneInfo("America/Lima"))
        return valor.strftime("%d/%m/%Y %H:%M")
    texto = str(valor or "").strip()
    for formato in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %I:%M:%S %p",
        "%Y/%m/%d %I:%M %p",
    ):
        try:
            return datetime.strptime(texto, formato).strftime(
                "%d/%m/%Y %H:%M"
            )
        except ValueError:
            continue
    for formato in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(texto, formato).strftime("%d/%m/%Y")
        except ValueError:
            continue
    return texto or "—"


def _tabla_historial_detallado_personal(
    movimientos,
    columnas,
    clase_monto="personal-history-amount",
    clase_tabla="personal-detail-history",
):
    filas = []
    for movimiento in movimientos:
        celdas = []
        for clave, _, formato in columnas:
            valor = movimiento.get(clave)
            if formato == "fecha":
                texto = _formatear_fecha_hora_personal(valor)
                clase = "personal-history-date"
            elif formato == "importe":
                texto = _moneda(valor)
                clase = clase_monto
            else:
                texto = str(valor or "—")
                clase = ""
            celdas.append(
                f'<td class="{clase}">{escape(texto)}</td>'
            )
        filas.append(f"<tr>{''.join(celdas)}</tr>")
    encabezados = "".join(
        f"<th>{escape(etiqueta)}</th>"
        for _, etiqueta, _ in columnas
    )
    return (
        '<div class="gestion-personal-attendance-history '
        f'{escape(clase_tabla)}">'
        f"<table><thead><tr>{encabezados}</tr></thead><tbody>"
        f'{"".join(filas)}'
        "</tbody></table></div>"
    )


def _redondear_bonificaciones_personal(importes, total):
    centavo = Decimal("0.01")
    importes_redondeados = [
        importe.quantize(centavo) for importe in importes
    ]
    diferencia = Decimal(str(total)).quantize(centavo) - sum(
        importes_redondeados,
        Decimal("0.00"),
    )
    ajuste_centavos = int(diferencia / centavo)
    if ajuste_centavos:
        indices = sorted(
            range(len(importes)),
            key=lambda indice: (
                importes[indice] - importes_redondeados[indice]
            ),
            reverse=ajuste_centavos > 0,
        )
        ajuste = centavo if ajuste_centavos > 0 else -centavo
        for indice in indices[:abs(ajuste_centavos)]:
            importes_redondeados[indice] += ajuste
    return importes_redondeados


def _decimal_moneda(valor):
    texto = str(valor or "").replace("S/", "").strip()
    if texto in {"", "—", "-"}:
        return Decimal("0.00")
    return Decimal(texto)


def _periodo_liquidacion(modalidad, referencia):
    if modalidad == "Diario":
        return referencia, referencia
    if modalidad == "Semanal":
        inicio_semana = referencia - timedelta(days=referencia.weekday())
        return inicio_semana, inicio_semana + timedelta(days=6)
    inicio_mes = referencia.replace(day=1)
    fin_mes = referencia.replace(
        day=calendar.monthrange(referencia.year, referencia.month)[1]
    )
    if modalidad == "Mensual":
        return inicio_mes, fin_mes
    if referencia.day <= 15:
        return inicio_mes, referencia.replace(day=15)
    return referencia.replace(day=16), fin_mes


def _fecha_limite_pago(modalidad, fecha_periodo):
    if modalidad == "Semanal":
        return fecha_periodo + timedelta(days=6 - fecha_periodo.weekday())
    if modalidad == "Mensual":
        ultimo_dia = calendar.monthrange(
            fecha_periodo.year, fecha_periodo.month
        )[1]
        return fecha_periodo.replace(day=ultimo_dia)
    if modalidad == "Quincenal":
        if fecha_periodo.day <= 15:
            return fecha_periodo.replace(day=15)
        ultimo_dia = calendar.monthrange(
            fecha_periodo.year, fecha_periodo.month
        )[1]
        return fecha_periodo.replace(day=ultimo_dia)
    if modalidad == "Diario":
        return fecha_periodo + timedelta(days=1)
    return None


def _sueldo_correspondiente(modalidad, sueldo_base, resumen):
    sueldo = Decimal(str(sueldo_base))
    if modalidad == "Quincenal":
        return (sueldo / 2).quantize(Decimal("0.01"))
    if modalidad == "Diario":
        dias_trabajados = Decimal(str(resumen.get("asistio", 0))) + (
            Decimal(str(resumen.get("medio_dia", 0))) / 2
        )
        return (sueldo * dias_trabajados).quantize(Decimal("0.01"))
    if modalidad == "Mensual":
        return sueldo.quantize(Decimal("0.01"))
    if modalidad == "Semanal":
        return sueldo.quantize(Decimal("0.01"))
    return None


def _descuento_por_faltas(modalidad, sueldo_base, resumen):
    divisores_diarios = {
        "Mensual": Decimal("30"),
        "Quincenal": Decimal("15"),
        "Semanal": Decimal("7"),
    }
    divisor = divisores_diarios.get(modalidad)
    if divisor is None:
        return Decimal("0.00")
    dias_faltados = int(resumen.get("falto", 0) or 0)
    semanas_con_falta = int(resumen.get("semanas_con_falta", 0) or 0)
    dias_a_descontar = dias_faltados + semanas_con_falta
    return (
        Decimal(str(sueldo_base))
        / divisor
        * dias_a_descontar
    ).quantize(Decimal("0.01"))


def _estado_pago(
    bruto,
    adelantos,
    pagado,
    existe_liquidacion,
    periodo_vencido=False,
):
    saldo = max(Decimal(bruto) - Decimal(adelantos) - Decimal(pagado), 0)
    if saldo == 0 and existe_liquidacion:
        return "🟢 Pagado", saldo
    if Decimal(pagado) > 0:
        return "🟡 Parcial", saldo
    if periodo_vencido:
        return "🔴 Retrasado", saldo
    return "🔴 Pendiente", saldo


def _estilos_personal():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,500,0,0');
        :root {
            --personal-bg: #07111E;
            --personal-card: #0D1B2B;
            --personal-secondary: #102238;
            --personal-border: #294158;
            --personal-red: #FF3545;
            --personal-turquoise: #00D6B2;
            --personal-blue: #29A8FF;
            --personal-purple: #B779F7;
            --personal-green: #27D98B;
            --personal-orange: #FF9B43;
            --personal-text: #F4F7FC;
            --personal-muted: #A5B4C7;
        }
        .gestion-personal-kpis {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: .7rem;
            margin: .45rem 0 .8rem;
        }
        .gestion-personal-kpi {
            position: relative;
            overflow: hidden;
            min-width: 0;
            display: flex;
            align-items: center;
            gap: .8rem;
            min-height: 6.25rem;
            padding: .8rem .9rem;
            border: 1px solid color-mix(in srgb, var(--kpi-accent) 48%, #294158);
            border-top: 3px solid var(--kpi-accent);
            border-radius: 13px;
            background: linear-gradient(145deg, #0D1B2B, #091727);
            box-shadow: 0 8px 20px rgba(0,0,0,.15), inset 0 1px 0 rgba(255,255,255,.035);
            transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
        }
        .gestion-personal-kpi:hover {
            transform: translateY(-2px);
            border-color: color-mix(in srgb, var(--kpi-accent) 72%, #294158);
            box-shadow: 0 12px 24px rgba(0,0,0,.2), inset 0 1px 0 rgba(255,255,255,.04);
        }
        .gestion-personal-kpi::after {
            position: absolute;
            right: 0;
            bottom: 0;
            width: 3rem;
            height: 1.8rem;
            background: linear-gradient(135deg, transparent 38%, #27384b 39% 54%, #ff3545 55% 69%, transparent 70%);
            content: "";
            opacity: .82;
        }
        .gestion-personal-kpi-icon {
            display: grid;
            flex: 0 0 2.6rem;
            width: 2.6rem;
            height: 2.6rem;
            place-items: center;
            border: 1.5px solid currentColor;
            border-radius: 50%;
            background: color-mix(in srgb, currentColor 13%, transparent);
            font-family: "Material Symbols Rounded";
            font-size: 1.35rem;
        }
        .gestion-personal-kpi-copy { min-width: 0; }
        .gestion-personal-kpi-label {
            color: #A5B4C7;
            font-size: .82rem;
            font-weight: 650;
        }
        .gestion-personal-kpi-value {
            margin-top: .12rem;
            color: #F4F7FC;
            font-size: clamp(1.25rem, 2vw, 1.8rem);
            font-weight: 800;
            font-variant-numeric: tabular-nums;
            line-height: 1.15;
            overflow-wrap: anywhere;
        }
        .gestion-personal-kpi.staff { --kpi-accent: #00D6B2; }
        .gestion-personal-kpi.salary { --kpi-accent: #29A8FF; }
        .gestion-personal-kpi.bonus { --kpi-accent: #B779F7; }
        .gestion-personal-kpi.pending { --kpi-accent: #FF9B43; }
        .gestion-personal-kpi .gestion-personal-kpi-icon { color: var(--kpi-accent); }
        .gestion-personal-kpis.analytics-kpis {
            grid-template-columns: repeat(3, minmax(0, 1fr));
        }
        .gestion-personal-kpis.analytics-kpis-single {
            grid-template-columns: minmax(0, 1fr);
        }
        .gestion-personal-analytics-title {
            margin: 0 0 .35rem;
            color: #DCE6F2;
            font-size: .9rem;
            font-weight: 750;
        }
        .gestion-personal-analytics-note {
            margin: -.2rem 0 .65rem;
            color: #A5B4C7;
            font-size: .75rem;
        }
        [class*="st-key-gestion_personal_asistencia_chart_"] [data-testid="stVerticalBlockBorderWrapper"],
        [class*="st-key-gestion_personal_adelantos_chart_"] [data-testid="stVerticalBlockBorderWrapper"] {
            border-color: color-mix(in srgb, #29A8FF 42%, #294158) !important;
            background: linear-gradient(145deg, #0D1B2B, #091727) !important;
        }
        [class*="st-key-gestion_personal_bonificacion_chart_"] [data-testid="stVerticalBlockBorderWrapper"] {
            border-color: color-mix(in srgb, #B779F7 45%, #294158) !important;
            background: linear-gradient(145deg, #0D1B2B, #091727) !important;
        }
        .gestion-personal-heading {
            margin: .05rem 0 .12rem;
            color: #F4F7FC;
            font-size: clamp(1.55rem, 2.7vw, 2.2rem);
            font-weight: 800;
            letter-spacing: -.04em;
            line-height: 1.1;
        }
        .gestion-personal-heading .brand { color: #FF3545; }
        .gestion-personal-heading-icon {
            margin-right: .35rem;
            color: #f4f7fb;
            font-family: "Material Symbols Rounded";
            font-size: .9em;
            font-style: normal;
            vertical-align: -.08em;
        }
        .gestion-personal-heading-rule {
            width: 2.4rem;
            height: 4px;
            margin: .05rem 0 .45rem;
            border-radius: 4px;
            background: #FF3545;
        }
        .gestion-personal-subtitle {
            margin: 0;
            color: #A5B4C7;
            font-size: .88rem;
        }
        .gestion-personal-period-label {
            margin: .05rem 0 .35rem;
            color: #A5B4C7;
            font-size: .82rem;
            text-align: right;
        }
        .gestion-personal-period-label span {
            padding: .25rem .55rem;
            border: 1px solid rgba(41,168,255,.34);
            border-radius: 8px;
            background: rgba(16,34,56,.7);
            color: #D9EAFB;
        }
        .gestion-personal-period-label span::first-letter {
            font-family: "Material Symbols Rounded";
        }
        .gestion-personal-table-title {
            color: #eaf1fa;
            font-size: 1.08rem;
            font-weight: 750;
        }
        .gestion-personal-grid-cell .money-positive { color: #00D6A0; font-weight: 700; }
        .gestion-personal-grid-cell .money-zero { color: #A5B4C7; }
        .gestion-personal-grid-cell .money-negative { color: #FF9B43; font-weight: 700; }
        .gestion-personal-grid-cell .money-bonus { color: #A855F7; font-weight: 700; }
        .gestion-personal-grid-cell .pay-status,
        .gestion-personal-grid-cell .pay-type {
            display: inline-block;
            min-width: 3.25rem;
            padding: .14rem .42rem;
            border: 1px solid currentColor;
            border-radius: 6px;
            background: color-mix(in srgb, currentColor 12%, transparent);
            text-align: center;
            font-size: .8rem;
        }
        .gestion-personal-grid-cell .pay-paid { color: #00D6A0; }
        .gestion-personal-grid-cell .pay-partial { color: #00AFFF; }
        .gestion-personal-grid-cell .pay-pending { color: #FF9B43; }
        .gestion-personal-grid-cell .pay-overdue { color: #FF3545; }
        .gestion-personal-grid-cell .pay-config { color: #A5B4C7; }
        [class*="st-key-gestion_personal_registrar_pago_"]
        [data-testid="stBaseButton-primary"] {
            border-color: #159957 !important;
            background: linear-gradient(110deg, #12804a, #18a85f) !important;
            color: #fff !important;
            -webkit-text-fill-color: #fff !important;
        }
        [class*="st-key-gestion_personal_registrar_pago_"]
        [data-testid="stBaseButton-primary"]:hover {
            border-color: #20c875 !important;
            background: linear-gradient(110deg, #159957, #20c875) !important;
        }
        .gestion-personal-grid-cell .type-monthly { color: #00AFFF; }
        .gestion-personal-grid-cell .type-fortnight { color: #A855F7; }
        .gestion-personal-grid-cell .type-daily { color: #00D6A0; }
        .gestion-personal-grid-cell .type-weekly { color: #FFB547; }
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_asistencia_celda_"] {
            width: 100%;
        }
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_asistencia_celda_"] [data-testid="stVerticalBlock"],
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_asistencia_celda_"] [data-testid="stElementContainer"],
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_asistencia_celda_"] [data-testid="stButton"] {
            width: 100% !important;
        }
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_asistencia_celda_"] [data-testid="stElementContainer"],
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_asistencia_celda_"] [data-testid="stButton"] {
            display: flex;
            justify-content: center;
        }
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_asistencia_celda_"] button {
            margin-inline: auto;
            width: 1.75rem;
            min-width: 1.75rem;
            min-height: 1.75rem;
            padding: 0;
            color: #29A8FF;
        }
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_asistencia_celda_"] button:hover {
            border-color: #29A8FF;
            color: #8BD3FF;
        }
        section[role="dialog"]:has(.st-key-gestion_personal_calendar_dialog) {
            position: fixed !important;
            inset: 50% auto auto 50% !important;
            z-index: 1002 !important;
            width: min(920px, 94vw) !important;
            max-width: none !important;
            max-height: 90dvh !important;
            overflow-x: hidden !important;
            overflow-y: auto !important;
            margin: 0 !important;
            transform: translate(-50%, -50%) !important;
            border: 1px solid #16415B;
            border-radius: 12px;
            background: #061525;
            box-shadow: 0 24px 70px rgba(0,0,0,.68);
        }
        section[role="dialog"]:has(.st-key-gestion_personal_calendar_dialog) > h2 {
            height: 0;
            margin: 0;
            padding: 0 !important;
            border: 0;
            overflow: hidden;
            font-size: 0;
            line-height: 0;
        }
        section[role="dialog"]:has(.st-key-gestion_personal_calendar_dialog) > h2::before {
            content: none;
        }
        section[role="dialog"]:has(.st-key-gestion_personal_calendar_dialog) > button[aria-label="Close"] {
            position: absolute;
            top: .72rem;
            right: .8rem;
            z-index: 3;
            width: 2rem;
            height: 2rem;
            border: 1px solid transparent;
            border-radius: 7px;
            color: #8CA6BC;
        }
        section[role="dialog"]:has(.st-key-gestion_personal_calendar_dialog) > button[aria-label="Close"]:hover {
            border-color: #16415B;
            background: #0B2338;
            color: #EAF6FF;
        }
        .st-key-gestion_personal_calendar_dialog [data-testid="stVerticalBlock"] {
            gap: .7rem;
        }
        .gestion-personal-calendar-header {
            display: flex;
            min-height: 3.2rem;
            align-items: center;
            gap: .7rem;
            padding: .2rem 2.7rem .75rem .15rem;
            border-bottom: 1px solid rgba(22,65,91,.8);
        }
        .gestion-personal-calendar-header-icon {
            color: #38C8FF;
            font-family: "Material Symbols Rounded";
            font-size: 1.55rem;
            line-height: 1;
        }
        .gestion-personal-calendar-header h3 {
            margin: 0;
            color: #B7E9FF;
            font-size: 1.12rem;
            font-weight: 750;
        }
        .gestion-personal-calendar-header p {
            margin: .18rem 0 0;
            color: #89A9C1;
            font-size: .76rem;
        }
        .st-key-gestion_personal_calendar_toolbar [data-testid="stHorizontalBlock"] {
            align-items: stretch;
            gap: .65rem;
        }
        .st-key-gestion_personal_calendar_month_nav {
            height: 100%;
            padding: .45rem .55rem;
            border: 1px solid #16415B;
            border-radius: 9px;
            background: #081B2D;
            box-shadow: inset 0 0 18px rgba(56,200,255,.025);
        }
        .st-key-gestion_personal_calendar_month_nav [data-testid="stHorizontalBlock"] {
            min-height: 3.15rem;
            align-items: center;
            gap: .25rem;
        }
        .st-key-gestion_personal_calendar_month_nav [data-testid="stBaseButton-tertiary"] {
            width: 2.25rem;
            min-width: 2.25rem;
            height: 2.25rem;
            border: 1px solid #16415B;
            border-radius: 7px;
            background: #061525;
            color: #6DD7FF;
        }
        .st-key-gestion_personal_calendar_month_nav [data-testid="stBaseButton-tertiary"]:hover {
            border-color: #38C8FF;
            background: #0B2940;
        }
        .gestion-personal-calendar-month-label {
            display: flex;
            min-height: 2.6rem;
            align-items: center;
            justify-content: center;
            gap: .45rem;
            color: #D9F3FF;
            font-size: .9rem;
            font-weight: 700;
            text-transform: capitalize;
            white-space: nowrap;
        }
        .gestion-personal-calendar-month-label i {
            color: #38C8FF;
            font-family: "Material Symbols Rounded";
            font-size: 1.1rem;
            font-style: normal;
        }
        .gestion-personal-calendar-kpis {
            display: grid;
            height: 100%;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: .45rem;
        }
        .gestion-personal-calendar-kpi {
            display: flex;
            min-height: 4.05rem;
            align-items: center;
            justify-content: center;
            flex-direction: column;
            gap: .22rem;
            border: 1px solid #12334A;
            border-radius: 9px;
            background: #081B2D;
        }
        .gestion-personal-calendar-kpi strong {
            display: inline-flex;
            width: 1.8rem;
            height: 1.8rem;
            align-items: center;
            justify-content: center;
            border: 1px solid currentColor;
            border-radius: 50%;
            background: color-mix(in srgb, currentColor 15%, #061525);
            box-shadow: 0 0 10px color-mix(in srgb, currentColor 28%, transparent);
            font-size: .82rem;
            font-variant-numeric: tabular-nums;
        }
        .gestion-personal-calendar-kpi span {
            color: #A7C0D3;
            font-size: .68rem;
            white-space: nowrap;
        }
        .gestion-personal-calendar-kpi.kpi-a { color: #00D6B2; }
        .gestion-personal-calendar-kpi.kpi-m { color: #FF9B43; }
        .gestion-personal-calendar-kpi.kpi-f { color: #FF4757; }
        .st-key-gestion_personal_calendar_content [data-testid="stHorizontalBlock"] {
            align-items: stretch;
            gap: .7rem;
        }
        .gestion-personal-calendar-grid {
            display: grid;
            grid-template-columns: repeat(7, minmax(0, 1fr));
            overflow: hidden;
            border: 1px solid #16415B;
            border-radius: 9px;
            background: #081B2D;
            box-shadow: 0 8px 22px rgba(0,0,0,.14);
        }
        .gestion-personal-calendar-weekday,
        .gestion-personal-calendar-day {
            display: flex;
            min-height: 2.8rem;
            align-items: center;
            justify-content: center;
            flex-direction: column;
            border-right: 1px solid rgba(22,65,91,.38);
            border-bottom: 1px solid rgba(22,65,91,.38);
        }
        .gestion-personal-calendar-weekday {
            min-height: 2rem;
            background: #0B2338;
            color: #8FAEC4;
            font-size: .7rem;
            font-weight: 700;
        }
        .gestion-personal-calendar-day {
            min-height: 2.75rem;
            color: #DCEAF5;
            font-size: .78rem;
            font-variant-numeric: tabular-nums;
        }
        .gestion-personal-calendar-day > span {
            display: inline-flex;
            width: 1.72rem;
            height: 1.72rem;
            align-items: center;
            justify-content: center;
            border: 1px solid #31516A;
            border-radius: 50%;
            background: #102A40;
            color: #B8CAD8;
            line-height: 1;
        }
        .gestion-personal-calendar-day.status-a > span {
            border-color: #00D6B2;
            background: #00D6B2;
            box-shadow: 0 0 9px rgba(0,214,178,.32);
            color: #062127;
        }
        .gestion-personal-calendar-day.status-m > span {
            border-color: #FF9B43;
            background: #FF9B43;
            box-shadow: 0 0 9px rgba(255,155,67,.28);
            color: #2A1B0E;
        }
        .gestion-personal-calendar-day.status-f > span {
            border-color: #FF3545;
            background: #FF3545;
            box-shadow: 0 0 9px rgba(255,53,69,.3);
            color: #FFFFFF;
        }
        .gestion-personal-calendar-day.is-day-off > span {
            border-color: #72C9FF;
            background: #72C9FF;
            box-shadow: 0 0 9px rgba(114,201,255,.32);
            color: #08243A;
        }
        .gestion-personal-calendar-day.is-other-month > span {
            border-color: transparent;
            background: transparent;
            box-shadow: none;
            color: #49647A;
            opacity: .65;
        }
        .gestion-personal-calendar-day:nth-child(7n) ,
        .gestion-personal-calendar-weekday:nth-child(7n) {
            border-right: 0;
        }
        .gestion-personal-calendar-panel {
            padding: .75rem .8rem;
            border: 1px solid #16415B;
            border-radius: 9px;
            background: #081B2D;
        }
        .gestion-personal-calendar-panel h4 {
            margin: 0 0 .55rem;
            color: #B7E9FF;
            font-size: .8rem;
            font-weight: 750;
        }
        .gestion-personal-calendar-summary-row,
        .gestion-personal-calendar-legend-row {
            display: flex;
            align-items: center;
            gap: .45rem;
            min-height: 1.75rem;
            color: #AFC2D2;
            font-size: .74rem;
        }
        .gestion-personal-calendar-summary-row strong {
            margin-left: auto;
            color: #E9F4FB;
            font-variant-numeric: tabular-nums;
        }
        .gestion-personal-calendar-dot {
            width: .55rem;
            height: .55rem;
            flex: 0 0 .55rem;
            border-radius: 50%;
            box-shadow: 0 0 8px color-mix(in srgb, currentColor 40%, transparent);
        }
        .gestion-personal-calendar-dot.dot-a { background: #00D6B2; color: #00D6B2; }
        .gestion-personal-calendar-dot.dot-m { background: #FF9B43; color: #FF9B43; }
        .gestion-personal-calendar-dot.dot-f { background: #FF3545; color: #FF3545; }
        .gestion-personal-calendar-dot.dot-rest { background: #72C9FF; color: #72C9FF; }
        .gestion-personal-calendar-dot.dot-empty { background: #607A91; color: #607A91; }
        .st-key-gestion_personal_calendar_content .gestion-personal-calendar-panel + .gestion-personal-calendar-panel {
            margin-top: .65rem;
        }
        .gestion-personal-attendance-history {
            display: inline-block;
            width: max-content;
            max-width: 100%;
            box-sizing: border-box;
            overflow: hidden;
            border: 1px solid #294158;
            border-radius: 9px;
            background: #0D1B2B;
        }
        .gestion-personal-attendance-title {
            display: inline-flex;
            width: max-content;
            max-width: 100%;
            box-sizing: border-box;
            align-items: center;
            margin: .45rem 0 .35rem;
            padding: .28rem .58rem;
            border: 1px solid #294158;
            border-radius: 7px;
            background: #0D1B2B;
            color: #DCE6F2;
            font-size: .8rem;
            line-height: 1.25;
            font-weight: 700;
        }
        .gestion-personal-attendance-history table {
            width: max-content;
            max-width: 100%;
            table-layout: auto;
            border-collapse: collapse;
            color: #DCE6F2;
            font-size: .88rem;
        }
        .gestion-personal-attendance-history th,
        .gestion-personal-attendance-history td {
            box-sizing: border-box;
            padding: .52rem .55rem;
            border-bottom: 1px solid rgba(41,65,88,.7);
        }
        .gestion-personal-attendance-history th:first-child,
        .gestion-personal-attendance-history td:first-child {
            width: 6rem;
            min-width: 6rem;
        }
        .gestion-personal-attendance-history th:nth-child(2),
        .gestion-personal-attendance-history td:nth-child(2) {
            width: 11.25rem;
            min-width: 11.25rem;
        }
        .gestion-personal-attendance-history th:last-child,
        .gestion-personal-attendance-history td:last-child {
            width: 4rem;
            min-width: 4rem;
        }
        .gestion-personal-attendance-history th {
            background: #102238;
            color: #A5B4C7;
            font-size: .78rem;
            font-weight: 700;
            text-align: center;
        }
        .gestion-personal-attendance-history tbody tr:last-child td {
            border-bottom: 0;
        }
        .gestion-personal-attendance-history tbody tr:hover {
            background: rgba(41,168,255,.06);
        }
        .gestion-personal-attendance-history .attendance-day,
        .gestion-personal-attendance-history .attendance-state {
            text-align: center;
            font-variant-numeric: tabular-nums;
        }
        .gestion-personal-attendance-history .attendance-day {
            color: #DCE6F2;
            font-weight: 650;
            white-space: nowrap;
        }
        .gestion-personal-attendance-history .attendance-datetime {
            color: #B6C6D7;
            text-align: center;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        .gestion-personal-attendance-history .attendance-state strong {
            display: inline-flex;
            width: 1.55rem;
            height: 1.55rem;
            align-items: center;
            justify-content: center;
            border-radius: 50%;
            background: color-mix(in srgb, currentColor 13%, #07111E);
            font-size: .85rem;
        }
        .gestion-personal-attendance-history .status-a { color: #22C55E; }
        .gestion-personal-attendance-history .status-m { color: #F59E0B; }
        .gestion-personal-attendance-history .status-f { color: #EF4444; }
        .gestion-personal-attendance-history .status-empty {
            color: #71869B;
        }
        .personal-movement-history th:first-child,
        .personal-movement-history td:first-child {
            width: 8rem;
            min-width: 8rem;
        }
        .personal-movement-history th:last-child,
        .personal-movement-history td:last-child {
            width: 11.5rem;
            min-width: 11.5rem;
        }
        .personal-movement-history .personal-movement-amount {
            color: #27D98B;
            font-weight: 700;
            text-align: center;
            white-space: nowrap;
        }
        .personal-movement-history .personal-movement-date {
            color: #B6C6D7;
            font-variant-numeric: tabular-nums;
            text-align: center;
            white-space: nowrap;
        }
        .gestion-personal-attendance-history.personal-detail-history {
            display: block;
            width: 100%;
            overflow-x: auto;
        }
        .personal-detail-history table {
            width: 100%;
            min-width: 42rem;
        }
        .personal-detail-history th:first-child,
        .personal-detail-history td:first-child {
            width: 10rem;
            min-width: 10rem;
        }
        .personal-detail-history th:last-child,
        .personal-detail-history td:last-child {
            width: 9rem;
            min-width: 9rem;
        }
        .personal-bonus-history table {
            min-width: 54rem;
            table-layout: fixed;
        }
        .personal-bonus-history th:nth-child(1),
        .personal-bonus-history td:nth-child(1) {
            width: 10rem;
            min-width: 10rem;
        }
        .personal-bonus-history th:nth-child(2),
        .personal-bonus-history td:nth-child(2) {
            width: 13rem;
            min-width: 13rem;
        }
        .personal-bonus-history th:nth-child(3),
        .personal-bonus-history td:nth-child(3) {
            width: 15rem;
            min-width: 15rem;
            white-space: normal;
        }
        .personal-bonus-history th:nth-child(4),
        .personal-bonus-history td:nth-child(4) {
            width: 8rem;
            min-width: 8rem;
        }
        .personal-bonus-history th:nth-child(5),
        .personal-bonus-history td:nth-child(5) {
            width: 9rem;
            min-width: 9rem;
        }
        .personal-advance-history table {
            min-width: 48rem;
            table-layout: fixed;
        }
        .personal-advance-history th:nth-child(1),
        .personal-advance-history td:nth-child(1) {
            width: 10rem;
            min-width: 10rem;
        }
        .personal-advance-history th:nth-child(2),
        .personal-advance-history td:nth-child(2) {
            width: 8rem;
            min-width: 8rem;
        }
        .personal-advance-history th:nth-child(3),
        .personal-advance-history td:nth-child(3) {
            width: 18rem;
            min-width: 18rem;
            white-space: normal;
        }
        .personal-advance-history th:nth-child(4),
        .personal-advance-history td:nth-child(4) {
            width: 10rem;
            min-width: 10rem;
            white-space: normal;
        }
        .personal-detail-history .personal-history-amount {
            color: #27D98B;
            font-weight: 700;
            text-align: center;
            white-space: nowrap;
        }
        .personal-detail-history .personal-bonus-amount {
            color: #B779F7;
        }
        .personal-detail-history .personal-history-date {
            color: #B6C6D7;
            font-variant-numeric: tabular-nums;
            text-align: center;
            white-space: nowrap;
        }
        .gestion-personal-worker-line .worker-status-active { color: #27D98B; }
        .gestion-personal-worker-line .worker-status-inactive { color: #A5B4C7; }
        .st-key-gestion_personal_table_grid {
            max-height: 20.875rem;
            overflow: auto;
            padding: 0;
            border: 1px solid #294158;
            border-radius: 8px;
            background: #061321;
            gap: 0 !important;
            row-gap: 0 !important;
            border-spacing: 0 !important;
            scrollbar-color: #294158 #061321;
            scrollbar-width: thin;
        }
        .st-key-gestion_personal_table_grid [data-testid="stHorizontalBlock"] {
            min-width: 1420px;
            gap: 0 !important;
            row-gap: 0 !important;
        }
        .st-key-gestion_personal_grid_header {
            position: sticky;
            top: 0;
            z-index: 2;
            width: 1420px;
            min-width: 1420px;
            min-height: 2.65rem;
            padding: 0;
            border-bottom: 1px solid #294158;
            background: #0B1B2D;
            overflow: visible;
        }
        .st-key-gestion_personal_grid_header [data-testid="stVerticalBlock"] {
            gap: 0 !important;
            row-gap: 0 !important;
        }
        .st-key-gestion_personal_grid_header [data-testid="stColumn"],
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_fila_"] [data-testid="stColumn"] {
            min-width: 0;
            padding: 0 .5rem !important;
            border: 0 !important;
        }
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_fila_par_"],
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_fila_impar_"] {
            width: 1420px !important;
            min-width: 1420px !important;
            min-height: 2.5rem !important;
            margin-block: 0 !important;
            padding: 0 !important;
            border: 0 !important;
            border-bottom: 1px solid #294158 !important;
            border-radius: 0 !important;
            box-shadow: none !important;
            background: #061321 !important;
            transition: background .14s ease;
        }
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_fila_par_"] {
            background: #061321 !important;
        }
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_fila_par_"]:hover,
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_fila_impar_"]:hover {
            background: #102238 !important;
        }
        .st-key-gestion_personal_table_grid
        [data-testid="stLayoutWrapper"]:has(> [data-testid="stVerticalBlock"][class*="st-key-gestion_personal_fila_"]) {
            width: 1420px !important;
            min-width: 1420px !important;
            margin: 0 !important;
            padding: 0 !important;
            border: 0 !important;
            border-radius: 0 !important;
            box-shadow: none !important;
        }
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_fila_par_"] > [data-testid="stVerticalBlock"],
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_fila_impar_"] > [data-testid="stVerticalBlock"] {
            gap: 0 !important;
            row-gap: 0 !important;
        }
        .st-key-gestion_personal_table_grid [data-testid="stElementContainer"] {
            margin: 0 !important;
            padding-block: 0 !important;
        }
        .gestion-personal-grid-header-cell,
        .gestion-personal-grid-cell {
            min-height: 2.5rem;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #DCE6F2;
            font-size: .875rem;
            line-height: 1.15;
            overflow-wrap: break-word;
            text-align: center;
        }
        .gestion-personal-grid-header-cell {
            min-height: 2.65rem;
            color: #A5B4C7;
            font-size: .76rem;
            font-weight: 750;
        }
        .gestion-personal-grid-cell.money-cell {
            justify-content: center;
            width: 100%;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        .st-key-gestion_personal_table_grid [data-testid="stButton"],
        .st-key-gestion_personal_table_grid [data-testid="stPopover"] {
            display: flex;
            justify-content: center;
        }
        .st-key-gestion_personal_table_grid [data-testid="stButton"] button,
        .st-key-gestion_personal_table_grid [data-testid="stPopover"] > button {
            width: 2rem;
            min-width: 2rem;
            min-height: 2rem;
            padding: .12rem;
            border: 1px solid transparent;
            border-radius: 6px;
            background: transparent;
            color: #A5B4C7;
            font-size: .82rem;
            font-weight: 600;
            white-space: nowrap;
        }
        .st-key-gestion_personal_table_grid [data-testid="stButton"] button:hover,
        .st-key-gestion_personal_table_grid [data-testid="stPopover"] > button:hover {
            border-color: #294158;
            background: #102238;
            color: #FFFFFF;
        }
        .st-key-gestion_personal_table_grid [data-testid="stButton"] button:focus-visible {
            outline: 2px solid #00AFFF;
            outline-offset: 2px;
        }
        .st-key-gestion_personal_table_grid div[class*="st-key-gestion_personal_action_editar_"] button {
            color: #D9E8F6;
        }
        .st-key-gestion_personal_table_grid div[class*="st-key-gestion_personal_configurar_"] button {
            width: auto;
            min-width: 0;
            min-height: 2rem;
            margin: 0;
            padding: 0 .3rem;
            border-color: #294158;
            color: #A5B4C7;
            font-size: .82rem;
        }
        .st-key-gestion_personal_table_grid
        div[class*="st-key-gestion_personal_configurar_"] [data-testid="stElementContainer"] {
            width: max-content;
        }
        .gestion-personal-detail {
            border: 1px solid rgba(41,168,255,.32);
            border-radius: 12px;
            background: linear-gradient(145deg, rgba(9,26,43,.97), rgba(5,17,30,.98));
            box-shadow: 0 10px 30px rgba(0,0,0,.18);
        }
        .gestion-personal-worker-card,
        .gestion-personal-info-card {
            height: 100%;
            padding: .9rem;
            border: 1px solid rgba(41,168,255,.24);
            border-radius: 12px;
            background: rgba(13,27,43,.86);
        }
        .gestion-personal-worker-name {
            margin: .45rem 0 .1rem;
            color: #F4F7FC;
            font-size: 1.18rem;
            font-weight: 750;
        }
        .gestion-personal-worker-role {
            margin: 0 0 .8rem;
            color: #A5B4C7;
            font-size: .82rem;
        }
        .gestion-personal-worker-heading {
            margin: .4rem 0 .65rem;
            padding: .55rem .7rem;
            border: 1px solid rgba(41,168,255,.28);
            border-radius: 9px;
            background: rgba(8,24,40,.7);
            color: #F4F7FC;
            font-size: 1.02rem;
            font-weight: 750;
        }
        .gestion-personal-worker-heading small {
            color: #A5B4C7;
            font-size: .8rem;
            font-weight: 500;
        }
        .stApp:has(.gestion-personal-heading) [data-testid="stMainBlockContainer"] {
            padding-top: 1.1rem;
        }
        .stApp:has(.gestion-personal-heading) [data-testid="stElementContainer"]:has(.gestion-personal-heading) {
            margin-bottom: -.35rem;
        }
        .stApp:has(.gestion-personal-heading) [data-testid="stVerticalBlockBorderWrapper"] {
            border-radius: 10px !important;
        }
        .gestion-personal-activity-empty {
            padding: .5rem 0;
            color: #A5B4C7;
            font-size: .8rem;
        }
        .gestion-personal-worker-avatar {
            display: grid;
            width: 2.5rem;
            height: 2.5rem;
            place-items: center;
            border: 1.5px solid #00D6B2;
            border-radius: 50%;
            background: rgba(0,214,178,.12);
            color: #00D6B2;
            font-family: "Material Symbols Rounded";
            font-size: 1.3rem;
        }
        .gestion-personal-worker-line,
        .gestion-personal-summary-line {
            display: flex;
            justify-content: space-between;
            gap: .5rem;
            padding: .36rem 0;
            border-bottom: 1px solid rgba(41,65,88,.5);
            color: #A5B4C7;
            font-size: .83rem;
        }
        .gestion-personal-worker-line strong,
        .gestion-personal-summary-line strong { color: #F4F7FC; text-align: right; }
        .gestion-personal-summary-total {
            margin-top: .25rem;
            padding-top: .55rem;
            border-top: 1px solid rgba(41,65,88,.72);
            color: #F4F7FC;
            font-size: .92rem;
            font-weight: 750;
        }
        .gestion-personal-summary-total strong {
            color: #27D98B;
            font-size: 1.3rem;
        }
        .gestion-personal-activity-title {
            margin: 0 0 .45rem;
            color: #EAF2FB;
            font-size: .9rem;
            font-weight: 700;
        }
        .gestion-personal-activity-row {
            display: grid;
            grid-template-columns: 1.6rem 1fr auto;
            align-items: center;
            gap: .45rem;
            padding: .38rem 0;
            border-bottom: 1px solid rgba(41,65,88,.42);
            color: #A5B4C7;
            font-size: .78rem;
        }
        .gestion-personal-activity-icon {
            display: grid;
            width: 1.45rem;
            height: 1.45rem;
            place-items: center;
            border-radius: 50%;
            background: color-mix(in srgb, var(--activity-color) 18%, transparent);
            color: var(--activity-color);
            font-family: "Material Symbols Rounded";
            font-size: .9rem;
        }
        .gestion-personal-activity-row strong { color: #DCE8F5; font-weight: 600; }
        .gestion-personal-activity-row span:last-child { color: #A5B4C7; }
        div[class*="st-key-gestion_personal_detail_tabs_"] [data-testid="stTabs"] [role="tablist"] {
            gap: .3rem;
            border: 0;
            border-bottom: 1px solid #294158;
        }
        div[class*="st-key-gestion_personal_detail_tabs_"] [data-testid="stTabs"] [role="tab"] {
            padding: .58rem .72rem;
            border-radius: 7px 7px 0 0;
            color: #A5B4C7 !important;
            font-size: .84rem;
            font-weight: 650;
            outline-offset: 2px;
        }
        div[class*="st-key-gestion_personal_detail_tabs_"] [data-testid="stTabs"] [role="tab"]:hover {
            background: rgba(41,168,255,.1);
            color: #F4F7FC !important;
        }
        div[class*="st-key-gestion_personal_detail_tabs_"] [data-testid="stTabs"] [role="tab"]:focus-visible {
            outline: 2px solid #29A8FF;
        }
        div[class*="st-key-gestion_personal_detail_tabs_"] [data-testid="stTabs"] [role="tab"][aria-selected="true"] {
            border-bottom: 3px solid #FF3545;
            background: rgba(255,53,69,.1);
            color: #FFFFFF !important;
        }
        .gestion-personal-table-panel [data-testid="stVerticalBlockBorderWrapper"] {
            border-color: rgba(41,168,255,.3) !important;
            background: linear-gradient(145deg,rgba(13,27,43,.96),rgba(7,17,30,.98)) !important;
        }
        .gestion-personal-table-panel [data-testid="stDownloadButton"] button {
            border-color: rgba(41,65,88,.8);
            background: #102238;
            color: #EAF2FB;
            font-size: .82rem;
        }
        .stApp:has(.gestion-personal-heading) [data-testid="stTextInput"] input,
        .stApp:has(.gestion-personal-heading) [data-testid="stSelectbox"] [data-baseweb="select"] > div,
        .stApp:has(.gestion-personal-heading) [data-testid="stNumberInput"] input,
        .stApp:has(.gestion-personal-heading) [data-testid="stDateInput"] input,
        .stApp:has(.gestion-personal-heading) textarea {
            border-color: #294158;
            background: #102238;
            color: #F4F7FC;
        }
        .stApp:has(.gestion-personal-heading) [data-testid="stTextInput"] input:focus,
        .stApp:has(.gestion-personal-heading) [data-testid="stNumberInput"] input:focus,
        .stApp:has(.gestion-personal-heading) textarea:focus {
            border-color: #29A8FF;
            box-shadow: 0 0 0 1px #29A8FF;
        }
        .stApp:has(.gestion-personal-heading) [data-testid="stBaseButton-primary"] {
            border: 1px solid #FF3545;
            border-radius: 9px;
            background: #FF3545;
            color: #FFFFFF;
            font-weight: 700;
            transition: filter .15s ease, transform .15s ease;
        }
        .stApp:has(.gestion-personal-heading) [data-testid="stBaseButton-primary"]:hover {
            filter: brightness(1.08);
            transform: translateY(-1px);
        }
        .st-key-gestion_personal_asistencia_button [data-testid="stBaseButton-primary"] {
            min-height: 2.75rem;
            border: 1px solid #FF3545;
            border-radius: 9px;
            background: linear-gradient(135deg, #FF3545, #D92139);
            box-shadow: 0 5px 16px rgba(255,53,69,.2);
            color: #FFFFFF;
            font-weight: 700;
        }
        .st-key-gestion_personal_asistencia_button [data-testid="stBaseButton-primary"]::after {
            margin-left: .35rem;
            content: "expand_more";
            font-family: "Material Symbols Rounded";
            font-size: 1.1rem;
            vertical-align: -.2em;
        }
        section[role="dialog"]:has(.st-key-gestion_personal_asistencia_dialog) {
            position: fixed !important;
            inset: 50% auto auto 50% !important;
            z-index: 1002 !important;
            width: min(840px, 90vw) !important;
            max-width: none !important;
            max-height: 90dvh !important;
            overflow-x: hidden !important;
            overflow-y: auto !important;
            margin: 0 !important;
            transform: translate(-50%, -50%) !important;
            border: 1px solid #00AFFF;
            border-radius: 16px;
            background: #07111E;
            box-shadow: 0 24px 70px rgba(0,0,0,.62);
            animation: gestion-personal-asistencia-in .18s ease-out both;
        }
        @keyframes gestion-personal-asistencia-in {
            from { opacity: 0; transform: translate(-50%, calc(-50% + 8px)); }
            to { opacity: 1; transform: translate(-50%, -50%); }
        }
        section[role="dialog"]:has(.st-key-gestion_personal_asistencia_dialog) > h2 {
            margin: 0;
            padding: 1rem 3rem .65rem 1.2rem;
            border-bottom: 1px solid rgba(41,65,88,.7);
            color: #F4F7FC;
            font-size: 1.2rem;
            font-weight: 750;
        }
        section[role="dialog"]:has(.st-key-gestion_personal_asistencia_dialog) > h2::before {
            margin-right: .55rem;
            color: #00D6B2;
            content: "calendar_month";
            font-family: "Material Symbols Rounded";
            font-size: 1.25rem;
            vertical-align: -.18em;
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stVerticalBlock"] {
            gap: .55rem;
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stDateInput"] input {
            border: 1px solid #294158;
            border-radius: 8px;
            background: #102238;
            color: #F4F7FC;
        }
        .gestion-personal-asistencia-subtitle {
            margin: -.35rem 0 .45rem;
            color: #A5B4C7;
            font-size: .86rem;
        }
        .st-key-gestion_personal_asistencia_header {
            border: 1px solid rgba(41,168,255,.26);
            border-radius: 8px 8px 0 0;
            background: #102238;
            padding: 0 .7rem;
        }
        .st-key-gestion_personal_asistencia_header [data-testid="stHorizontalBlock"] {
            align-items: center;
            padding: .45rem 0;
            min-height: 2.5rem;
        }
        .st-key-gestion_personal_asistencia_header [data-testid="stMarkdownContainer"] {
            color: #A5B4C7;
            font-size: .8rem;
            font-weight: 700;
            text-align: center;
        }
        .st-key-gestion_personal_asistencia_header [data-testid="stMarkdownContainer"] p {
            margin: 0;
            line-height: 1.35;
            white-space: nowrap;
        }
        .st-key-gestion_personal_asistencia_header [data-testid="stCheckbox"] {
            display: flex;
            justify-content: center;
        }
        .st-key-gestion_personal_asistencia_header [data-testid="stCheckbox"] label {
            min-height: 2rem;
            padding: .25rem .55rem;
            border: 1px solid rgba(143,174,196,.24);
            border-radius: 7px;
            background: rgba(8,27,45,.58);
            gap: .35rem;
            color: #A5B4C7;
            font-size: .8rem;
            font-weight: 700;
            white-space: nowrap;
            transition: background .15s ease, border-color .15s ease;
        }
        .st-key-gestion_personal_asistencia_header [data-testid="stCheckbox"] label:has(input:checked) {
            border-color: #29A8FF;
            background: rgba(41,168,255,.16);
            color: #D9F1FF;
        }
        .st-key-gestion_personal_asistencia_header [data-testid="stCheckbox"] input {
            accent-color: #29A8FF;
        }
        [class*="st-key-gestion_personal_asistencia_row_"] {
            padding: .45rem .7rem;
            border-bottom: 1px solid #294158;
            background: rgba(13,27,43,.42);
        }
        [class*="st-key-gestion_personal_asistencia_row_"]:nth-child(even) {
            background: rgba(16,34,56,.36);
        }
        .gestion-personal-asistencia-name {
            overflow-wrap: anywhere;
            color: #F4F7FC;
            font-size: .9rem;
            font-weight: 600;
            text-align: center;
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadio"] > div[role="radiogroup"] {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            align-items: center;
            justify-items: center;
            width: 100%;
            gap: 0;
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadio"] {
            width: calc(100% + 6px);
            margin-left: 4px;
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadioGroup"] > div {
            display: flex;
            width: 100%;
            justify-content: center;
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadioOption"] {
            justify-content: center;
            width: 100%;
            gap: 0;
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadioOption"] > div {
            justify-content: center;
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadioOption"] [data-testid="stMarkdownContainer"] {
            position: absolute;
            width: 1px;
            height: 1px;
            padding: 0;
            margin: -1px;
            overflow: hidden;
            clip: rect(0, 0, 0, 0);
            white-space: nowrap;
            border: 0;
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadioGroup"] > div:nth-child(1) [data-testid="stRadioOption"]:has(input:checked) > div > div:first-child {
            border-color: #00D6B2;
            box-shadow: 0 0 0 2px rgba(0,214,178,.12);
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadioGroup"] > div:nth-child(1) [data-testid="stRadioOption"]:has(input:checked) > div > div:first-child > div {
            background: #00D6B2;
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadioGroup"] > div:nth-child(2) [data-testid="stRadioOption"]:has(input:checked) > div > div:first-child {
            border-color: #FF9B43;
            box-shadow: 0 0 0 2px rgba(255,155,67,.12);
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadioGroup"] > div:nth-child(2) [data-testid="stRadioOption"]:has(input:checked) > div > div:first-child > div {
            background: #FF9B43;
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadioGroup"] > div:nth-child(3) [data-testid="stRadioOption"]:has(input:checked) > div > div:first-child {
            border-color: #FF3545;
            box-shadow: 0 0 0 2px rgba(255,53,69,.12);
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadioGroup"] > div:nth-child(3) [data-testid="stRadioOption"]:has(input:checked) > div > div:first-child > div {
            background: #FF3545;
        }
        .st-key-gestion_personal_asistencia_dialog [data-testid="stRadio"] input[type="radio"] {
            accent-color: #6A91BD;
        }
        .st-key-gestion_personal_asistencia_rows {
            max-height: min(48dvh, 390px);
            overflow-x: hidden;
            overflow-y: auto;
            border: 1px solid #294158;
            border-top: 0;
            border-radius: 0 0 8px 8px;
            scrollbar-color: #39536B #0D1B2B;
        }
        .st-key-gestion_personal_asistencia_cancel [data-testid="stBaseButton-secondary"] {
            min-height: 2.7rem;
            border: 1px solid #39536B;
            background: #102238;
            color: #DCE8F5;
        }
        .st-key-gestion_personal_asistencia_save [data-testid="stBaseButton-primary"] {
            min-height: 2.7rem;
            background: #FF3545;
        }
        div[class*="st-key-gestion_personal_payment_form_"] [data-testid="stBaseButton-primary"] {
            border-color: #27D98B !important;
            background: #27D98B !important;
            color: #07111E !important;
        }
        div[class*="st-key-gestion_personal_advance_form_"] [data-testid="stBaseButton-primary"] {
            border-color: #B779F7 !important;
            background: #B779F7 !important;
            color: #07111E !important;
        }
        div[class*="st-key-gestion_personal_delete_"] [data-testid="stBaseButton-primary"] {
            border-color: #FF3545 !important;
            background: #FF3545 !important;
            color: #FFFFFF !important;
        }
        section[role="dialog"]:has(.st-key-gestion_personal_edit_dialog) {
            position: fixed !important;
            inset: 50% auto auto 50% !important;
            z-index: 1001 !important;
            width: min(560px, calc(100vw - 2rem)) !important;
            max-width: none !important;
            max-height: 88dvh !important;
            overflow-x: hidden !important;
            overflow-y: auto !important;
            margin: 0 !important;
            transform: translate(-50%, -50%) !important;
            border: 1px solid #294158;
            border-radius: 16px;
            background: #07111E;
            box-shadow: 0 24px 70px rgba(0,0,0,.58);
        }
        section[role="dialog"]:has(.st-key-gestion_personal_edit_dialog) > h2 {
            margin: 0;
            padding: 1rem 3rem .75rem 1.2rem;
            color: #F4F7FC;
            font-size: 1.15rem;
            font-weight: 750;
        }
        section[role="dialog"]:has(.st-key-gestion_personal_edit_dialog) > h2::before {
            margin-right: .45rem;
            color: #EAF2FB;
            content: "groups";
            font-family: "Material Symbols Rounded";
            font-size: 1.2rem;
            vertical-align: -.15em;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stVerticalBlock"] {
            gap: .55rem;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stVerticalBlockBorderWrapper"] {
            padding: .65rem .8rem;
        }
        .gestion-personal-edit-section-title {
            display: flex;
            align-items: center;
            gap: .45rem;
            margin: 0 0 .35rem;
            color: #DCEAF8;
            font-size: .96rem;
            font-weight: 700;
        }
        .gestion-personal-edit-section-icon {
            display: grid;
            flex: 0 0 1.55rem;
            width: 1.55rem;
            height: 1.55rem;
            place-items: center;
            border: 1px solid rgba(0,175,255,.42);
            border-radius: 6px;
            background: rgba(0,175,255,.14);
            color: #00AFFF;
            font-family: "Material Symbols Rounded";
            font-size: 1rem;
        }
        .gestion-personal-edit-section-icon.salary {
            border-color: rgba(0,214,160,.42);
            background: rgba(0,214,160,.12);
            color: #00D6A0;
        }
        .gestion-personal-edit-section-icon.advance {
            border-color: rgba(183,121,247,.48);
            background: rgba(183,121,247,.15);
            color: #C793FF;
        }
        .gestion-personal-edit-avatar {
            display: grid;
            width: 4.4rem;
            height: 4.4rem;
            place-items: center;
            border: 1px solid #00AFFF;
            border-radius: 50%;
            background: rgba(0,175,255,.09);
            color: #00AFFF;
            font-family: "Material Symbols Rounded";
            font-size: 2.3rem;
        }
        .gestion-personal-edit-field-label {
            margin: .1rem 0 .35rem;
            color: #A5B4C7;
            font-size: .82rem;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stCheckbox"] {
            padding-top: .25rem;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stAlert"] {
            margin: .25rem 0 0;
            border: 1px solid rgba(0,175,255,.3);
            border-radius: 8px;
            background: rgba(0,175,255,.08);
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stFormSubmitButton"] button {
            min-height: 2.25rem;
            border: 1px solid #B779F7;
            border-radius: 8px;
            background: rgba(183,121,247,.16);
            color: #D7B7FF;
            font-weight: 650;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stFormSubmitButton"] button:hover {
            background: #B779F7;
            color: #07111E;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stTabs"] [role="tablist"] {
            gap: .25rem;
            border-bottom: 1px solid #294158;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stTabs"] [role="tab"] {
            padding: .55rem .7rem;
            border-radius: 7px 7px 0 0;
            color: #A5B4C7 !important;
            font-size: .84rem;
            font-weight: 650;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stTabs"] [role="tab"]:hover {
            background: rgba(41,168,255,.1);
            color: #F4F7FC !important;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stTabs"] [role="tab"]:focus-visible {
            outline: 2px solid #29A8FF;
            outline-offset: 2px;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stTabs"] [role="tab"][aria-selected="true"] {
            border-bottom: 3px solid #FF3545;
            background: rgba(255,53,69,.09);
            color: #FFFFFF !important;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stVerticalBlockBorderWrapper"] {
            border-color: #294158 !important;
            border-radius: 12px !important;
            background: rgba(13,27,43,.62);
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stTextInput"] input,
        .st-key-gestion_personal_edit_dialog [data-testid="stNumberInput"] input,
        .st-key-gestion_personal_edit_dialog [data-testid="stDateInput"] input,
        .st-key-gestion_personal_edit_dialog [data-testid="stSelectbox"] [data-baseweb="select"] > div,
        .st-key-gestion_personal_edit_dialog textarea {
            border-color: #294158;
            background: #102238;
            color: #F4F7FC;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stTextInput"] input:focus,
        .st-key-gestion_personal_edit_dialog [data-testid="stNumberInput"] input:focus,
        .st-key-gestion_personal_edit_dialog [data-testid="stDateInput"] input:focus,
        .st-key-gestion_personal_edit_dialog textarea:focus {
            border-color: #29A8FF;
            box-shadow: 0 0 0 1px #29A8FF;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stBaseButton-primary"] {
            border-color: #FF3545;
            background: #FF3545;
            color: #FFFFFF;
            font-weight: 700;
        }
        .st-key-gestion_personal_edit_dialog div[class*="st-key-gestion_personal_edit_advance_form_"] [data-testid="stBaseButton-primary"] {
            border-color: #B779F7;
            background: #B779F7;
            color: #07111E;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stBaseButton-secondary"] {
            border: 1px solid #39536B;
            background: #102238;
            color: #EAF2FB;
        }
        .st-key-gestion_personal_edit_dialog [data-testid="stDataFrame"] {
            border: 1px solid #294158;
            border-radius: 9px;
        }
        .stApp:has(.gestion-personal-heading) [data-testid="stBaseButton-secondary"] {
            border: 1px solid #39536B;
            border-radius: 9px;
            background: #102238;
            color: #EAF2FB;
            font-weight: 650;
        }
        .stApp:has(.gestion-personal-heading) button:disabled {
            cursor: not-allowed;
            opacity: .55;
        }
        @media (max-width: 900px) {
            .gestion-personal-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .gestion-personal-kpis.analytics-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .gestion-personal-kpis.analytics-kpis-single { grid-template-columns: minmax(0, 1fr); }
            .gestion-personal-detail-columns { flex-direction: column; }
        }
        @media (max-width: 560px) {
            section[role="dialog"]:has(.st-key-gestion_personal_calendar_dialog) {
                width: 94vw !important;
                max-width: none !important;
                max-height: 94dvh !important;
                padding: .5rem !important;
                border-radius: 10px;
            }
            .st-key-gestion_personal_calendar_dialog [data-testid="stVerticalBlock"] {
                gap: .5rem;
            }
            .gestion-personal-calendar-header {
                min-height: 2.8rem;
                gap: .45rem;
                padding: .1rem 2rem .55rem .1rem;
            }
            .gestion-personal-calendar-header-icon { font-size: 1.25rem; }
            .gestion-personal-calendar-header h3 { font-size: .95rem; }
            .gestion-personal-calendar-header p { font-size: .68rem; }
            .st-key-gestion_personal_calendar_toolbar > [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"],
            .st-key-gestion_personal_calendar_content > [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"] {
                flex-direction: column !important;
                flex-wrap: nowrap !important;
                gap: .5rem;
            }
            .st-key-gestion_personal_calendar_toolbar [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
            .st-key-gestion_personal_calendar_content [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
                width: 100% !important;
                min-width: 0 !important;
                flex: 1 1 auto !important;
            }
            .st-key-gestion_personal_calendar_month_nav {
                padding: .3rem;
            }
            .st-key-gestion_personal_calendar_month_nav [data-testid="stHorizontalBlock"] {
                flex-direction: row !important;
                flex-wrap: nowrap !important;
            }
            .gestion-personal-calendar-kpis {
                gap: .35rem;
            }
            .gestion-personal-calendar-kpi {
                min-height: 3.5rem;
            }
            .gestion-personal-calendar-grid {
                grid-template-columns: repeat(7, minmax(0, 1fr));
            }
            .gestion-personal-calendar-day {
                min-height: 2.35rem;
            }
            .gestion-personal-calendar-day > span {
                width: 1.55rem;
                height: 1.55rem;
                font-size: .72rem;
            }
            .gestion-personal-calendar-weekday {
                min-height: 1.8rem;
                font-size: .64rem;
            }
            .st-key-gestion_personal_calendar_content .gestion-personal-calendar-panel + .gestion-personal-calendar-panel {
                margin-top: .45rem;
            }
            section[role="dialog"]:has(.st-key-gestion_personal_asistencia_dialog) {
                width: 94vw !important;
                max-width: none !important;
                max-height: 94dvh !important;
                padding: .45rem !important;
                border-radius: 12px;
            }
            section[role="dialog"]:has(.st-key-gestion_personal_asistencia_dialog) > h2 {
                padding: .8rem 2.5rem .55rem .8rem;
                font-size: 1.05rem;
            }
            .st-key-gestion_personal_asistencia_dialog .st-key-gestion_personal_asistencia_header,
            .st-key-gestion_personal_asistencia_dialog [class*="st-key-gestion_personal_asistencia_row_"] {
                padding: .38rem .25rem;
            }
            .st-key-gestion_personal_asistencia_header [data-testid="stHorizontalBlock"],
            .st-key-gestion_personal_asistencia_dialog [class*="st-key-gestion_personal_asistencia_row_"] [data-testid="stHorizontalBlock"] {
                flex-direction: row !important;
                flex-wrap: nowrap !important;
                align-items: center;
            }
            .st-key-gestion_personal_asistencia_header [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
            .st-key-gestion_personal_asistencia_dialog [class*="st-key-gestion_personal_asistencia_row_"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
                min-width: 0 !important;
            }
            .st-key-gestion_personal_asistencia_header [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(1),
            .st-key-gestion_personal_asistencia_dialog [class*="st-key-gestion_personal_asistencia_row_"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(1) {
                flex: 1.7 1 0% !important;
            }
            .st-key-gestion_personal_asistencia_header [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(2),
            .st-key-gestion_personal_asistencia_dialog [class*="st-key-gestion_personal_asistencia_row_"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(2) {
                flex: 3 1 0% !important;
            }
            .st-key-gestion_personal_asistencia_header [data-testid="stColumn"]:nth-child(2) [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
                flex: 1 1 0% !important;
                min-width: 0 !important;
            }
            .st-key-gestion_personal_asistencia_header [data-testid="stMarkdownContainer"] {
                white-space: nowrap;
                font-size: .63rem;
            }
            .st-key-gestion_personal_asistencia_dialog [data-testid="stRadio"] {
                width: calc(100% + 9px);
                margin-left: 3.5px;
            }
            .st-key-gestion_personal_asistencia_footer [data-testid="stHorizontalBlock"] {
                flex-direction: row !important;
                flex-wrap: nowrap !important;
            }
            .st-key-gestion_personal_asistencia_footer [data-testid="stColumn"] {
                min-width: 0 !important;
                flex: 1 1 0% !important;
            }
            .gestion-personal-asistencia-name { font-size: .78rem; }
            section[role="dialog"]:has(.st-key-gestion_personal_edit_dialog) {
                width: calc(100vw - 1rem) !important;
                max-width: none !important;
                max-height: 94dvh !important;
                margin: 0 !important;
                transform: translate(-50%, -50%) !important;
                padding: .65rem !important;
                border-radius: 12px;
            }
            .st-key-gestion_personal_edit_dialog [data-testid="stVerticalBlockBorderWrapper"] {
                padding: .55rem;
            }
            .gestion-personal-edit-avatar {
                width: 3.25rem;
                height: 3.25rem;
                font-size: 1.8rem;
            }
            .gestion-personal-edit-section-title { font-size: .86rem; }
            .st-key-gestion_personal_edit_dialog [data-testid="stTabs"] [role="tab"] {
                padding: .42rem .3rem;
                font-size: .7rem;
            }
            .gestion-personal-kpis { grid-template-columns: 1fr 1fr; gap: .4rem; }
            .gestion-personal-kpis.analytics-kpis { grid-template-columns: 1fr 1fr; }
            .gestion-personal-kpis.analytics-kpis-single { grid-template-columns: minmax(0, 1fr); }
            .gestion-personal-kpi { gap: .45rem; min-height: 5.4rem; padding: .55rem; }
            .gestion-personal-kpi-icon { flex-basis: 2rem; width: 2rem; height: 2rem; font-size: 1rem; }
            .gestion-personal-kpi-value { font-size: 1.08rem; }
            .gestion-personal-kpi-label { font-size: .62rem; }
            .gestion-personal-heading { font-size: 1.4rem; }
            .gestion-personal-grid-header-cell,
            .gestion-personal-grid-cell { font-size: .73rem; }
            .st-key-gestion_personal_table_grid [data-testid="stButton"] button {
                min-width: 2.3rem;
                min-height: 2.15rem;
                padding: .25rem;
                font-size: 0;
            }
            .st-key-gestion_personal_table_grid [data-testid="stButton"] button [data-testid="stIconMaterial"] {
                font-size: 1.1rem;
            }
            div[class*="st-key-gestion_personal_detail_tabs_"] [data-testid="stTabs"] [role="tab"] {
                padding: .45rem .4rem;
                font-size: .73rem;
            }
            .gestion-personal-activity-row { grid-template-columns: 1.5rem 1fr; }
            .gestion-personal-activity-row span:last-child { grid-column: 2; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _actividad_reciente_html(historial):
    configuracion = {
        "asistencia": ("person", "#00d1a4", "Asistencia"),
        "adelanto": ("payments", "#b779f7", "Adelanto"),
        "bonificacion": ("redeem", "#ffbe42", "Bonificación"),
        "liquidacion": ("calendar_month", "#36b8ff", "Liquidación"),
        "pago": ("credit_card", "#2388ff", "Pago"),
        "perfil": ("manage_accounts", "#93c5fd", "Perfil"),
    }
    filas = []
    movimientos_visibles = [
        movimiento
        for movimiento in historial
        if not movimiento.get("pago_anulado", False)
    ]
    for movimiento in movimientos_visibles[:5]:
        tipo = str(movimiento.get("tipo_movimiento") or "")
        icono, color, etiqueta = configuracion.get(
            tipo, ("history", "#93c5fd", "Movimiento")
        )
        detalle = movimiento.get("detalle") or {}
        if not isinstance(detalle, dict):
            detalle = {}
        cantidad = next(
            (
                detalle[clave]
                for clave in ("importe", "sueldo_base", "bonificacion")
                if detalle.get(clave) is not None
            ),
            None,
        )
        importe = (
            _moneda(cantidad)
            if cantidad is not None
            else escape(str(movimiento.get("accion") or ""))
        )
        creado = movimiento.get("creado_en")
        fecha = creado.strftime("%d/%m/%Y") if hasattr(creado, "strftime") else ""
        filas.append(
            '<div class="gestion-personal-activity-row">'
            f'<span class="gestion-personal-activity-icon" '
            f'style="--activity-color:{color}">{icono}</span>'
            f'<strong>{escape(etiqueta)} · {escape(fecha)}</strong>'
            f"<span>{importe}</span></div>"
        )
    if not filas:
        filas.append(
            '<div class="gestion-personal-activity-empty">'
            "Aún no hay movimientos registrados.</div>"
        )
    return "".join(filas)


def _celda_tabla_personal(clave, valor, estado_pago=""):
    texto = str(valor)
    if clave == "Tipo de pago":
        clases = {
            "Mensual": "type-monthly",
            "Quincenal": "type-fortnight",
            "Diario": "type-daily",
            "Semanal": "type-weekly",
        }
        clase = clases.get(texto, "pay-config")
        return f'<span class="pay-type {clase}">{escape(texto)}</span>'
    if clave == "Estado de pago":
        if "Pagado" in texto:
            clase, etiqueta = "pay-paid", "Pagado"
        elif "Parcial" in texto:
            clase, etiqueta = "pay-partial", "Parcial"
        elif "Retrasado" in texto:
            clase, etiqueta = "pay-overdue", "Retrasado"
        elif "Pendiente" in texto:
            clase, etiqueta = "pay-pending", "Pendiente"
        else:
            clase, etiqueta = "pay-config", texto.split(" ", 1)[-1]
        return f'<span class="pay-status {clase}">{escape(etiqueta)}</span>'
    if clave == "Total a pagar (S/)":
        clase = "money-zero" if texto == "—" else "money-positive"
        if any(
            estado in estado_pago
            for estado in ("Pendiente", "Retrasado")
        ) and texto != "—":
            clase = "money-negative"
        return f'<span class="{clase}">{escape(texto)}</span>'
    if clave in {
        "Sueldo base (S/)",
        "Adelantos (S/)",
    }:
        clase = "money-zero" if texto == "—" else "money-positive"
        return f'<span class="{clase}">{escape(texto)}</span>'
    if clave == "Bonificación (S/)":
        clase = "money-zero" if texto == "—" else "money-bonus"
        return f'<span class="{clase}">{escape(texto)}</span>'
    return escape(texto)


def _marcar_columna_asistencia(estado, fecha, perfil_ids, clave_checkbox):
    seleccionado = st.session_state.get(clave_checkbox, False)
    for perfil_id in perfil_ids:
        clave_radio = (
            f"gestion_personal_asistencia_v2_{fecha}_{perfil_id}"
        )
        if seleccionado:
            st.session_state[clave_radio] = estado
        elif st.session_state.get(clave_radio) == estado:
            st.session_state[clave_radio] = None


def _contenido_dialogo_asistencia_personal(perfiles, actor):
    clave_fecha = "gestion_personal_asistencia_fecha"
    st.session_state.setdefault(clave_fecha, _fecha_actual_personal())
    with st.container(key="gestion_personal_asistencia_dialog"):
        st.subheader("Registrar asistencia")
        st.markdown(
            '<p class="gestion-personal-asistencia-subtitle">'
            "Selecciona la asistencia de cada colaborador para la fecha elegida."
            "</p>",
            unsafe_allow_html=True,
        )
        fecha = st.date_input(
            "Fecha",
            key=clave_fecha,
            format="DD/MM/YYYY",
        )
        inicio_mes_asistencia = fecha.replace(day=1)
        fin_mes_asistencia = fecha.replace(
            day=calendar.monthrange(fecha.year, fecha.month)[1]
        )
        asistencia_fecha = _obtener_asistencias_precargadas(
            inicio_mes_asistencia,
            fin_mes_asistencia,
            fecha=fecha,
        )
        if asistencia_fecha is None:
            asistencia_fecha = _consultar_personal_cacheado(
                consultar_asistencias_personal_fecha,
                fecha,
                cache_version=_PERSONAL_QUERY_SCHEMA_VERSION,
            )
        asistencia_por_id = {
            registro["trabajador_id"]: registro
            for registro in asistencia_fecha
        }
        opciones = ("A", "M", "F")
        etiquetas = {
            "A": "Asistió",
            "M": "Medio día",
            "F": "Faltó",
        }
        if not perfiles:
            st.info("No hay trabajadores registrados para mostrar.")
            return

        selecciones = {}
        perfil_ids = tuple(perfil["id"] for perfil in perfiles)
        with st.container(key="gestion_personal_asistencia_header"):
            col_nombre, col_opciones = st.columns(
                [1.7, 3],
                gap="small",
                vertical_alignment="center",
            )
            with col_nombre:
                st.markdown("**Colaborador**")
            with col_opciones:
                cabeceras_opciones = st.columns(
                    [1, 1, 1],
                    gap=None,
                    vertical_alignment="center",
                )
                for columna, estado, titulo in zip(
                    cabeceras_opciones,
                    opciones,
                    ("Asistió", "Medio día", "Faltó"),
                ):
                    with columna:
                        clave_checkbox = (
                            "gestion_personal_asistencia_todos_"
                            f"{fecha}_{estado}"
                        )
                        st.checkbox(
                            titulo,
                            key=clave_checkbox,
                            help=f"Asignar {titulo.lower()} a todos.",
                            on_change=_marcar_columna_asistencia,
                            args=(
                                estado,
                                fecha,
                                perfil_ids,
                                clave_checkbox,
                            ),
                        )
        with st.form("gestion_personal_asistencia_form"):
            with st.container(key="gestion_personal_asistencia_rows"):
                for perfil in perfiles:
                    perfil_id = perfil["id"]
                    clave_radio = (
                        f"gestion_personal_asistencia_v2_{fecha}_{perfil_id}"
                    )
                    if clave_radio not in st.session_state:
                        st.session_state[clave_radio] = None
                    with st.container(
                        key=f"gestion_personal_asistencia_row_{perfil_id}"
                    ):
                        col_nombre, col_opciones = st.columns(
                            [1.7, 3],
                            gap="small",
                            vertical_alignment="center",
                        )
                        with col_nombre:
                            st.markdown(
                                '<div class="gestion-personal-asistencia-name">'
                                f'{escape(str(perfil["nombre"]))}</div>',
                                unsafe_allow_html=True,
                            )
                        with col_opciones:
                            selecciones[perfil_id] = st.radio(
                                f"Asistencia de {perfil['nombre']}",
                                opciones,
                                index=None,
                                format_func=lambda estado: etiquetas[estado],
                                horizontal=True,
                                width="stretch",
                                label_visibility="collapsed",
                                key=clave_radio,
                            )

            with st.container(key="gestion_personal_asistencia_footer"):
                col_cancelar, col_guardar = st.columns(2, gap="small")
                with col_cancelar:
                    cancelar = st.form_submit_button(
                        "Cancelar",
                        type="secondary",
                        width="stretch",
                        key="gestion_personal_asistencia_cancel",
                    )
                with col_guardar:
                    guardar = st.form_submit_button(
                        "Guardar asistencia",
                        type="primary",
                        width="stretch",
                        key="gestion_personal_asistencia_save",
                        icon=":material/check:",
                    )

        if cancelar:
            st.session_state["gestion_personal_asistencia_abierto"] = False
            st.session_state["gestion_personal_asistencia_limpiar"] = True
            st.rerun()
        if guardar:
            faltantes = [
                perfil["nombre"]
                for perfil in perfiles
                if selecciones.get(perfil["id"]) not in opciones
            ]
            if faltantes:
                st.error(
                    "Selecciona una opción para cada colaborador. "
                    f"Pendientes: {len(faltantes)}."
                )
                return
            registros = [
                (
                    perfil["id"],
                    selecciones[perfil["id"]],
                    (
                        asistencia_por_id.get(perfil["id"], {}).get(
                            "observacion", ""
                        )
                    ),
                )
                for perfil in perfiles
            ]
            try:
                with st.spinner("Guardando asistencia..."):
                    guardar_asistencias_personal(
                        registros,
                        fecha,
                        actor,
                    )
            except (ValueError, psycopg2.Error) as error:
                st.error(str(error))
            else:
                _invalidar_consultas_personal()
                st.session_state["gestion_personal_asistencia_abierto"] = False
                st.session_state["gestion_personal_asistencia_limpiar"] = True
                st.session_state["gestion_personal_asistencia_guardada"] = True
                st.rerun()


def _contenido_dialogo_calendario_asistencia_personal(
    nombre, cargo, perfil_id, dias_libres=()
):
    with st.container(key="gestion_personal_calendar_dialog"):
        st.markdown(
            '<div class="gestion-personal-calendar-header">'
            '<span class="gestion-personal-calendar-header-icon">'
            "calendar_month</span><div>"
            f"<h3>Asistencia de {escape(str(nombre))}</h3>"
            f"<p><span class=\"material-symbols-rounded\" "
            'style="font-size:.9rem;vertical-align:-.15rem;margin-right:.25rem">'
            f"person</span>Cargo: {escape(str(cargo or 'Sin configurar'))}</p>"
            "</div></div>",
            unsafe_allow_html=True,
        )
        clave_mes = f"gestion_personal_calendario_mes_{perfil_id}"
        mes = st.session_state.setdefault(
            clave_mes, _fecha_actual_personal().replace(day=1)
        )
        mes = mes.replace(day=1)
        nombre_mes = (
            "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre",
            "diciembre",
        )[mes.month - 1]

        fecha_fin = mes.replace(
            day=calendar.monthrange(mes.year, mes.month)[1]
        )
        registros = _obtener_asistencias_precargadas(
            mes,
            fecha_fin,
            trabajador_id=perfil_id,
        )
        if registros is None:
            registros = _consultar_personal_cacheado(
                consultar_asistencias_personal,
                nombre,
                mes,
                fecha_fin,
                cache_version=_PERSONAL_QUERY_SCHEMA_VERSION,
            )
        estados_por_fecha = {}
        resumen = {"A": 0, "M": 0, "F": 0}
        for registro in registros:
            fecha_registro = registro["fecha"]
            if hasattr(fecha_registro, "date"):
                fecha_registro = fecha_registro.date()
            estado = registro["estado"]
            estados_por_fecha[fecha_registro.day] = estado
            if estado in resumen:
                resumen[estado] += 1

        with st.container(key="gestion_personal_calendar_toolbar"):
            col_selector, col_indicadores = st.columns(
                [1.08, 1], gap="small", vertical_alignment="center"
            )
            with col_selector:
                with st.container(key="gestion_personal_calendar_month_nav"):
                    col_anterior, col_mes, col_siguiente = st.columns(
                        [.42, 3.1, .42],
                        gap="small",
                        vertical_alignment="center",
                    )
                    with col_anterior:
                        if st.button(
                            " ",
                            key=f"gestion_personal_calendario_anterior_{perfil_id}",
                            icon=":material/chevron_left:",
                            help="Ver mes anterior",
                            type="tertiary",
                        ):
                            indice_mes = mes.year * 12 + mes.month - 2
                            anio, indice = divmod(indice_mes, 12)
                            st.session_state[clave_mes] = date(
                                anio, indice + 1, 1
                            )
                            st.rerun()
                    with col_mes:
                        st.markdown(
                            '<div class="gestion-personal-calendar-month-label">'
                            '<i>calendar_month</i>'
                            f"<span>{nombre_mes} {mes.year}</span></div>",
                            unsafe_allow_html=True,
                        )
                    with col_siguiente:
                        if st.button(
                            " ",
                            key=f"gestion_personal_calendario_siguiente_{perfil_id}",
                            icon=":material/chevron_right:",
                            help="Ver mes siguiente",
                            type="tertiary",
                        ):
                            indice_mes = mes.year * 12 + mes.month
                            anio, indice = divmod(indice_mes, 12)
                            st.session_state[clave_mes] = date(
                                anio, indice + 1, 1
                            )
                            st.rerun()
            with col_indicadores:
                tarjetas_resumen = (
                    ("a", "Asistió", resumen["A"]),
                    ("m", "Medio día", resumen["M"]),
                    ("f", "Faltó", resumen["F"]),
                )
                indicadores_html = "".join(
                    '<div class="gestion-personal-calendar-kpi kpi-'
                    f'{estado}"><strong>{cantidad}</strong>'
                    f"<span>{etiqueta}</span></div>"
                    for estado, etiqueta, cantidad in tarjetas_resumen
                )
                st.markdown(
                    '<div class="gestion-personal-calendar-kpis">'
                    f"{indicadores_html}</div>",
                    unsafe_allow_html=True,
                )

        dias_semana = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
        dias_semana_completos = (
            "Lunes",
            "Martes",
            "Miércoles",
            "Jueves",
            "Viernes",
            "Sábado",
            "Domingo",
        )
        celdas = "".join(
            '<div class="gestion-personal-calendar-weekday">'
            f"{dia}</div>"
            for dia in dias_semana
        )
        for fecha_dia in calendar.Calendar(firstweekday=0).itermonthdates(
            mes.year, mes.month
        ):
            es_otro_mes = fecha_dia.month != mes.month
            estado = (
                None
                if es_otro_mes
                else estados_por_fecha.get(fecha_dia.day)
            )
            es_dia_libre = (
                not es_otro_mes
                and dias_semana_completos[fecha_dia.weekday()] in dias_libres
            )
            clases = ["gestion-personal-calendar-day"]
            if es_otro_mes:
                clases.append("is-other-month")
            else:
                if estado in resumen:
                    clases.append(f"status-{estado.lower()}")
            if es_dia_libre:
                clases.append("is-day-off")
            etiqueta_estado = (
                f" · {estado}" if estado and not es_otro_mes else ""
            )
            if es_dia_libre:
                etiqueta_estado = " · Día libre" + etiqueta_estado
            celdas += (
                f'<div class="{" ".join(clases)}"'
                f' title="{fecha_dia:%d/%m/%Y}{etiqueta_estado}">'
                f"<span>{fecha_dia.day}</span></div>"
            )

        with st.container(key="gestion_personal_calendar_content"):
            col_calendario, col_paneles = st.columns(
                [1.7, .95], gap="small", vertical_alignment="top"
            )
            with col_calendario:
                st.markdown(
                    '<div class="gestion-personal-calendar-grid">'
                    f"{celdas}</div>",
                    unsafe_allow_html=True,
                )
            with col_paneles:
                filas_resumen = "".join(
                    '<div class="gestion-personal-calendar-summary-row">'
                    f'<span class="gestion-personal-calendar-dot dot-{estado}">'
                    "</span>"
                    f"<span>{etiqueta}</span><strong>{cantidad} días</strong>"
                    "</div>"
                    for estado, etiqueta, cantidad in tarjetas_resumen
                )
                st.markdown(
                    '<div class="gestion-personal-calendar-panel">'
                    "<h4>Resumen del mes</h4>"
                    f"{filas_resumen}</div>"
                    '<div class="gestion-personal-calendar-panel">'
                    "<h4>Leyenda</h4>"
                    '<div class="gestion-personal-calendar-legend-row">'
                    '<span class="gestion-personal-calendar-dot dot-a"></span>'
                    "Asistió</div>"
                    '<div class="gestion-personal-calendar-legend-row">'
                    '<span class="gestion-personal-calendar-dot dot-m"></span>'
                    "Medio día</div>"
                    '<div class="gestion-personal-calendar-legend-row">'
                    '<span class="gestion-personal-calendar-dot dot-f"></span>'
                    "Faltó</div>"
                    '<div class="gestion-personal-calendar-legend-row">'
                    '<span class="gestion-personal-calendar-dot dot-empty"></span>'
                    "Sin registro</div>"
                    '<div class="gestion-personal-calendar-legend-row">'
                    '<span class="gestion-personal-calendar-dot dot-rest"></span>'
                    "Día libre</div></div>",
                    unsafe_allow_html=True,
                )


def _seleccionar_accion_personal(perfil_id, nombre, accion):
    pestañas = {
        "Editar": "Resumen",
        "Asistencia": "Asistencia",
        "Adelanto": "Adelantos",
        "Pagar": "Pagos",
        "Historial": "Resumen",
    }
    st.session_state["gestion_personal_trabajador_seleccionado"] = nombre
    st.session_state[f"gestion_personal_tabs_{perfil_id}"] = pestañas[accion]
    st.session_state["gestion_personal_editar_abierto"] = (
        perfil_id if accion == "Editar" else None
    )
    st.session_state["gestion_personal_asistencia_abierto"] = False
    st.session_state["gestion_personal_calendario_asistencia_abierto"] = False
    st.session_state["gestion_personal_calendario_trabajador_id"] = None
    st.session_state["gestion_personal_historial_abierto"] = (
        perfil_id if accion == "Historial" else None
    )


def _contenido_dialogo_editar_personal(nombre, perfil, actor, referencia):
    perfil_id = perfil["id"]
    prefijo = f"editar_personal_{perfil_id}"
    clave_inicializado = "gestion_personal_dialogo_perfil_id"
    if st.session_state.get(clave_inicializado) != perfil_id:
        st.session_state[f"{prefijo}_cargo"] = perfil.get("cargo") or ""
        st.session_state[f"{prefijo}_sueldo"] = float(
            perfil.get("sueldo_base") or 0
        )
        modalidad_actual = perfil.get("modalidad_pago")
        st.session_state[f"{prefijo}_modalidad"] = (
            modalidad_actual
            if modalidad_actual
            in {"Diario", "Quincenal", "Mensual", "Semanal"}
            else "Mensual"
        )
        st.session_state[f"{prefijo}_ingreso"] = (
            perfil.get("fecha_ingreso") or referencia
        )
        st.session_state[f"{prefijo}_activo"] = bool(
            perfil.get("activo", True)
        )
        st.session_state[f"{prefijo}_dias_libres"] = list(
            perfil.get("dias_libres") or []
        )
        st.session_state[clave_inicializado] = perfil_id

    st.subheader("Editar información del trabajador")
    with st.container(key="gestion_personal_edit_dialog"):
        pestaña_datos, pestaña_dias_libres = st.tabs(
            [":material/person: Datos generales", ":material/event_busy: Días Libres"]
        )

        with pestaña_datos:
            with st.container(border=True, key="gestion_personal_edit_profile"):
                st.markdown(
                    '<div class="gestion-personal-edit-section-title">'
                    '<span class="gestion-personal-edit-section-icon">'
                    "person</span>Información personal</div>",
                    unsafe_allow_html=True,
                )
                col_avatar, col_datos = st.columns(
                    [0.62, 3.2], vertical_alignment="center"
                )
                with col_avatar:
                    st.markdown(
                        '<div class="gestion-personal-edit-avatar">'
                        "person</div>",
                        unsafe_allow_html=True,
                    )
                with col_datos:
                    col_nombre, col_cargo = st.columns(2)
                    with col_nombre:
                        st.text_input(
                            "Nombre completo",
                            value=nombre,
                            disabled=True,
                            help=(
                                "El nombre se administra desde el catálogo "
                                "para conservar sus referencias históricas."
                            ),
                            key=f"{prefijo}_nombre",
                        )
                    with col_cargo:
                        st.text_input(
                            "Cargo *",
                            key=f"{prefijo}_cargo",
                            placeholder="Ej. Lavador",
                        )
                    col_ingreso, col_estado = st.columns(2)
                    with col_ingreso:
                        st.date_input(
                            "Fecha de ingreso *",
                            key=f"{prefijo}_ingreso",
                        )
                    with col_estado:
                        st.markdown(
                            '<div class="gestion-personal-edit-field-label">'
                            "Estado</div>",
                            unsafe_allow_html=True,
                        )
                        st.checkbox(
                            "Activo",
                            key=f"{prefijo}_activo",
                        )

            with st.container(
                border=True, key="gestion_personal_edit_salary"
            ):
                st.markdown(
                    '<div class="gestion-personal-edit-section-title">'
                    '<span class="gestion-personal-edit-section-icon '
                    'salary">work</span>Configuración laboral</div>',
                    unsafe_allow_html=True,
                )
                col_base, col_modalidad = st.columns(2)
                with col_base:
                    sueldo = st.number_input(
                        "Sueldo base (S/) *",
                        min_value=0.0,
                        step=10.0,
                        key=f"{prefijo}_sueldo",
                    )
                with col_modalidad:
                    modalidad = st.selectbox(
                        "Tipo de pago *",
                        ["Diario", "Quincenal", "Mensual", "Semanal"],
                        key=f"{prefijo}_modalidad",
                    )
                explicaciones_periodo = {
                    "Diario": (
                        "El sueldo base es la tarifa diaria; A cuenta como "
                        "1 día y M como medio día."
                    ),
                    "Quincenal": (
                        "Se paga la mitad del sueldo base en cada periodo: "
                        "del 1 al 15 o del 16 al fin de mes."
                    ),
                    "Mensual": "El periodo corresponde al mes calendario.",
                    "Semanal": (
                        "El periodo corresponde a la semana calendario, "
                        "de lunes a domingo."
                    ),
                }
                st.info(explicaciones_periodo[modalidad], icon=":material/info:")

        with pestaña_dias_libres:
            st.markdown(
                '<div class="gestion-personal-edit-section-title">'
                '<span class="gestion-personal-edit-section-icon">'
                "event_busy</span>Programación de descanso</div>",
                unsafe_allow_html=True,
            )
            st.multiselect(
                "Días libres",
                [
                    "Lunes",
                    "Martes",
                    "Miércoles",
                    "Jueves",
                    "Viernes",
                    "Sábado",
                    "Domingo",
                ],
                key=f"{prefijo}_dias_libres",
                help=(
                    "Selecciona los días de descanso habituales. "
                    "Se marcarán en azul claro en el calendario histórico."
                ),
            )

        with pestaña_datos:
            with st.container(
                border=True, key="gestion_personal_edit_payment_status"
            ):
                st.markdown(
                    '<div class="gestion-personal-edit-section-title">'
                    '<span class="gestion-personal-edit-section-icon '
                    'advance">payments</span>'
                    "Estado de pago del periodo</div>",
                    unsafe_allow_html=True,
                )
                modalidad_registrada = perfil.get("modalidad_pago")
                if (
                    not modalidad_registrada
                    or perfil.get("sueldo_base") is None
                ):
                    st.caption(
                        "Configura sueldo y modalidad para consultar el estado."
                    )
                else:
                    inicio_pago, fin_pago = _periodo_liquidacion(
                        modalidad_registrada, referencia
                    )
                    fecha_actual_pago = datetime.now(
                        ZoneInfo("America/Lima")
                    ).date()
                    fecha_limite_pago = _fecha_limite_pago(
                        modalidad_registrada, fin_pago
                    )
                    adelantos_registrados_pago = (
                        st.session_state[
                            "_gestion_personal_obtener_adelantos_registrados"
                        ](nombre, inicio_pago, fin_pago)
                    )
                    liquidacion_pago = _consultar_personal_cacheado(
                        consultar_liquidacion_personal_periodo,
                        nombre,
                        inicio_pago,
                        fin_pago,
                        cache_version=_PERSONAL_QUERY_SCHEMA_VERSION,
                    )
                    if liquidacion_pago:
                        bruto_pago = (
                            Decimal(liquidacion_pago["sueldo_base"])
                            + Decimal(liquidacion_pago["bonificacion"])
                            - Decimal(liquidacion_pago["descuento"])
                        )
                        pagos_registrados_pago = Decimal(
                            liquidacion_pago["pagado"]
                        )
                        estado_pago_actual, saldo_pago = _estado_pago(
                            bruto_pago,
                            adelantos_registrados_pago,
                            pagos_registrados_pago,
                            True,
                            periodo_vencido=(
                                fecha_limite_pago is not None
                                and fecha_actual_pago > fecha_limite_pago
                            ),
                        )
                        estado_pago_actual = estado_pago_actual.split(
                            " ", 1
                        )[1]
                    else:
                        resumen_periodo_pago = next(
                            (
                                fila
                                for fila in _consultar_personal_cacheado(
                                    consultar_resumen_personal_periodo,
                                    inicio_pago,
                                    fin_pago,
                                    cache_version=_PERSONAL_QUERY_SCHEMA_VERSION,
                                )
                                if fila["id"] == perfil_id
                            ),
                            {},
                        )
                        bonificacion_actual = (
                            Decimal(
                                str(
                                    st.session_state[
                                        "_gestion_personal_obtener_bonificacion_mensual"
                                    ](nombre, fin_pago)
                                )
                            )
                            if modalidad_registrada == "Mensual"
                            else Decimal("0.00")
                        )
                        sueldo_periodo_pago = _sueldo_correspondiente(
                            modalidad_registrada,
                            perfil["sueldo_base"],
                            resumen_periodo_pago,
                        )
                        descuento_periodo_pago = _descuento_por_faltas(
                            modalidad_registrada,
                            perfil["sueldo_base"],
                            resumen_periodo_pago,
                        )
                        bruto_pago = (
                            sueldo_periodo_pago
                            + bonificacion_actual
                            - descuento_periodo_pago
                        )
                        pagos_registrados_pago = Decimal("0.00")
                        estado_pago_actual, saldo_pago = _estado_pago(
                            bruto_pago,
                            adelantos_registrados_pago,
                            pagos_registrados_pago,
                            False,
                            periodo_vencido=(
                                fecha_limite_pago is not None
                                and fecha_actual_pago > fecha_limite_pago
                            ),
                        )
                        estado_pago_actual = estado_pago_actual.split(
                            " ", 1
                        )[1]

                    puede_revertir_pago = (
                        liquidacion_pago is not None
                        and pagos_registrados_pago > 0
                        and bruto_pago - adelantos_registrados_pago > 0
                    ) if liquidacion_pago else False
                    st.caption(
                        f"Periodo: {inicio_pago:%d/%m/%Y} – "
                        f"{fin_pago:%d/%m/%Y} · "
                        f"Saldo actual: {_moneda(saldo_pago)}"
                    )
                    opciones_estado = (
                        [estado_pago_actual]
                        if (
                            estado_pago_actual == "Pendiente"
                            or not puede_revertir_pago
                        )
                        else [estado_pago_actual, "Pendiente"]
                    )
                    clave_estado_pago = (
                        f"{prefijo}_estado_pago_"
                        f"{inicio_pago:%Y%m%d}_{fin_pago:%Y%m%d}"
                    )
                    if st.session_state.get(clave_estado_pago) not in (
                        opciones_estado
                    ):
                        st.session_state[clave_estado_pago] = (
                            estado_pago_actual
                        )
                    estado_pago_nuevo = st.selectbox(
                        "Estado de pago",
                        opciones_estado,
                        key=clave_estado_pago,
                    )
                    if estado_pago_actual != "Pendiente" and puede_revertir_pago:
                        st.warning(
                            "Al cambiar a Pendiente, los pagos de este periodo "
                            "se conservarán en auditoría, pero dejarán de "
                            "contar para el saldo."
                        )
                        confirmar_reversion = st.checkbox(
                            "Confirmo revertir los pagos de este periodo",
                            key=f"{clave_estado_pago}_confirmar",
                        )
                        if st.button(
                            "Actualizar estado de pago",
                            key=f"{clave_estado_pago}_guardar",
                            type="secondary",
                            disabled=(
                                estado_pago_nuevo != "Pendiente"
                                or not confirmar_reversion
                            ),
                            icon=":material/undo:",
                        ):
                            try:
                                with st.spinner("Revirtiendo pagos..."):
                                    importe_revertido = (
                                        revertir_pagos_liquidacion_personal(
                                            liquidacion_pago["id"],
                                            actor,
                                        )
                                    )
                            except (ValueError, psycopg2.Error) as error:
                                st.error(str(error))
                            else:
                                _invalidar_consultas_personal()
                                st.success(
                                    "Estado actualizado a Pendiente. "
                                    f"Se revirtieron {_moneda(importe_revertido)} "
                                    "de pagos."
                                )
                                st.rerun()
                    elif (
                        liquidacion_pago
                        and estado_pago_actual != "Pendiente"
                        and not puede_revertir_pago
                    ):
                        st.info(
                            "Este periodo no tiene pagos registrados que "
                            "puedan revertirse."
                        )

        st.divider()
        col_cancelar, col_guardar = st.columns([1, 2])
        with col_cancelar:
            if st.button(
                "Cancelar",
                key=f"{prefijo}_cancelar",
                type="secondary",
                icon=":material/close:",
                width="stretch",
            ):
                st.session_state[clave_inicializado] = None
                st.session_state["gestion_personal_editar_abierto"] = None
                st.rerun()
        with col_guardar:
            guardar = st.button(
                "Guardar cambios",
                key=f"{prefijo}_guardar",
                type="primary",
                icon=":material/save:",
                width="stretch",
            )
        if guardar:
            cargo = st.session_state[f"{prefijo}_cargo"].strip()
            fecha_ingreso = st.session_state[f"{prefijo}_ingreso"]
            activo = st.session_state[f"{prefijo}_activo"]
            if not nombre.strip():
                st.error("El nombre del trabajador es obligatorio.")
            elif not cargo:
                st.error("El cargo del trabajador es obligatorio.")
            elif not isinstance(fecha_ingreso, date):
                st.error("Selecciona una fecha de ingreso válida.")
            else:
                try:
                    with st.spinner("Guardando cambios del trabajador..."):
                        guardar_perfil_personal(
                            nombre,
                            cargo,
                            sueldo,
                            modalidad,
                            st.session_state[f"{prefijo}_dias_libres"],
                            fecha_ingreso,
                            activo,
                            actor,
                        )
                except (ValueError, ArithmeticError, psycopg2.Error) as error:
                    st.error(str(error))
                else:
                    st.session_state[clave_inicializado] = None
                    st.session_state["gestion_personal_editar_abierto"] = None
                    _invalidar_consultas_personal()
                    st.success("Información del trabajador actualizada.")
                    st.rerun()


def _cerrar_dialogo_personal():
    st.session_state["gestion_personal_editar_abierto"] = None
    st.session_state["gestion_personal_asistencia_abierto"] = False
    st.session_state["gestion_personal_calendario_asistencia_abierto"] = False
    st.session_state["gestion_personal_calendario_trabajador_id"] = None


@st.dialog(
    " ",
    width="large",
    on_dismiss=_cerrar_dialogo_personal,
)
def _dialogo_personal(
    tipo,
    nombre=None,
    perfil=None,
    perfiles=None,
    actor=None,
    referencia=None,
):
    if tipo == "editar":
        _contenido_dialogo_editar_personal(nombre, perfil, actor, referencia)
    elif tipo == "asistencia":
        _contenido_dialogo_asistencia_personal(perfiles, actor)
    elif tipo == "calendario":
        _contenido_dialogo_calendario_asistencia_personal(
            nombre,
            perfil.get("cargo"),
            perfil["id"],
            perfil.get("dias_libres") or [],
        )
    else:
        raise ValueError(f"Tipo de diálogo de personal desconocido: {tipo}")


def _mostrar_pago_liquidacion(nombre, liquidacion_id, actor):
    liquidaciones = _consultar_personal_cacheado(
        consultar_liquidaciones_personal, nombre
    )
    liquidacion = next(
        (fila for fila in liquidaciones if fila["id"] == liquidacion_id),
        None,
    )
    if liquidacion is None:
        return
    bruto = Decimal(liquidacion["sueldo_base"]) + Decimal(
        liquidacion["bonificacion"]
    ) - Decimal(liquidacion["descuento"])
    adelantos = st.session_state[
        "_gestion_personal_obtener_adelantos_registrados"
    ](nombre, liquidacion["fecha_inicio"], liquidacion["fecha_fin"])
    saldo = max(
        bruto
        - adelantos
        - Decimal(liquidacion["pagado"]),
        Decimal("0.00"),
    )
    fecha_actual = datetime.now(ZoneInfo("America/Lima")).date()
    fecha_limite_pago = _fecha_limite_pago(
        liquidacion["modalidad_pago"],
        liquidacion["fecha_fin"],
    )
    estado_pago, _ = _estado_pago(
        bruto,
        adelantos,
        liquidacion["pagado"],
        True,
        periodo_vencido=(
            fecha_limite_pago is not None
            and fecha_actual > fecha_limite_pago
        ),
    )
    st.caption(
        f"Saldo actual: **{_moneda(saldo)}** · "
        f"Estado: **{estado_pago}**"
    )
    if saldo <= 0:
        st.success("Esta liquidación ya está cubierta.")
        return
    with st.container(key=f"gestion_personal_payment_form_{liquidacion_id}"):
        with st.form(f"registrar_pago_personal_{liquidacion_id}"):
            col_importe, col_fecha, col_metodo = st.columns(3)
            with col_importe:
                importe = st.number_input(
                    "Importe a pagar (S/)",
                    min_value=0.01,
                    max_value=float(saldo),
                    value=min(1.0, float(saldo)),
                    step=0.01,
                    key=f"monto_pago_personal_{liquidacion_id}",
                )
            with col_fecha:
                fecha_pago = st.date_input(
                    "Fecha de pago",
                    value=_fecha_actual_personal(),
                    key=f"fecha_pago_personal_{liquidacion_id}",
                )
            with col_metodo:
                metodo = st.selectbox(
                    "Método de pago",
                    ["Efectivo", "Yape", "Plin", "Transferencia", "Tarjeta", "Otro"],
                    key=f"metodo_pago_personal_{liquidacion_id}",
                )
            observacion = st.text_input(
                "Observación (opcional)",
                key=f"observacion_pago_personal_{liquidacion_id}",
            )
            confirmar = st.form_submit_button(
                "Registrar pago",
                type="primary",
                width="stretch",
            )
    if confirmar:
        try:
            with st.spinner("Registrando pago..."):
                registrar_pago_personal(
                    liquidacion_id,
                    importe,
                    fecha_pago,
                    metodo,
                    observacion,
                    actor,
                    adelantos,
                )
        except (ValueError, ArithmeticError, psycopg2.Error) as error:
            st.error(str(error))
        else:
            _invalidar_consultas_personal()
            st.success("Pago registrado correctamente.")
            st.rerun()


def _registrar_pago_total_trabajador(
    nombre,
    modalidad,
    fecha_inicio,
    fecha_fin,
    importe,
    adelantos,
    bonificacion,
    descuento,
    actor,
):
    liquidacion_id, _ = crear_liquidacion_personal(
        nombre,
        modalidad,
        fecha_inicio,
        fecha_fin,
        bonificacion,
        actor,
        descuento=descuento,
    )
    registrar_pago_personal(
        liquidacion_id,
        importe,
        datetime.now(ZoneInfo("America/Lima")).date(),
        "Efectivo",
        "",
        actor,
        adelantos,
    )


@st.fragment
def _mostrar_detalle_trabajador(
    nombres_trabajadores,
    perfiles_por_nombre,
    filas_vista,
    referencia,
    actor,
    obtener_bonificacion_mensual,
    fecha_inicio_resumen,
    fecha_fin_resumen,
):
    nombre = st.selectbox(
        "Seleccionar trabajador para consultar o editar",
        nombres_trabajadores,
        key="gestion_personal_trabajador_seleccionado",
    )
    perfil = perfiles_por_nombre[nombre.strip().casefold()]
    if perfil.get("modalidad_pago") in {
        "Diario",
        "Quincenal",
        "Mensual",
        "Semanal",
    }:
        fecha_inicio_resumen, fecha_fin_resumen = _periodo_liquidacion(
            perfil["modalidad_pago"], referencia
        )
    bonificacion_mensual = obtener_bonificacion_mensual(nombre, referencia)
    resumen_trabajador = next(
        fila for fila in filas_vista if fila["Nombre"] == nombre
    )
    st.markdown(
        '<div class="gestion-personal-worker-heading">'
        f'<span class="gestion-personal-heading-icon">person</span>'
        f'{escape(nombre)}'
        f'<small> · {escape(perfil.get("cargo") or "Cargo sin configurar")}'
        f' · ID {perfil["id"]:03d}</small></div>',
        unsafe_allow_html=True,
    )
    clave_pestanas = f"gestion_personal_tabs_{perfil['id']}"
    opciones_pestanas = [
        "Resumen",
        "Asistencia",
        "Adelantos",
        "Bonificaciones",
        "Pagos",
    ]
    if st.session_state.get(clave_pestanas) not in opciones_pestanas:
        st.session_state[clave_pestanas] = "Resumen"
    with st.container(key=f"gestion_personal_detail_tabs_{perfil['id']}"):
        tabs = st.tabs(
            opciones_pestanas,
            key=clave_pestanas,
            on_change="rerun",
        )

    with tabs[0]:
        col_perfil, col_actividad, col_resumen = st.columns([0.82, 1.45, 1.08])
        with col_perfil:
            fecha_ingreso_texto = (
                perfil.get("fecha_ingreso").strftime("%d/%m/%Y")
                if perfil.get("fecha_ingreso")
                else "Sin configurar"
            )
            st.markdown(
                '<div class="gestion-personal-worker-card">'
                '<div class="gestion-personal-worker-avatar">person</div>'
                f'<div class="gestion-personal-worker-name">{escape(nombre)}</div>'
                f'<p class="gestion-personal-worker-role">'
                f'{escape(perfil.get("cargo") or "Cargo sin configurar")}'
                f' · ID {perfil["id"]:03d}</p>'
                '<div class="gestion-personal-worker-line">'
                '<span>Sueldo del periodo</span>'
                f'<strong>{escape(resumen_trabajador.get("_sueldo_periodo", resumen_trabajador.get("Sueldo base (S/)", "—")))}</strong></div>'
                '<div class="gestion-personal-worker-line">'
                '<span>Tipo de pago</span>'
                f'<strong>{escape(perfil.get("modalidad_pago") or "Sin configurar")}'
                '</strong></div>'
                '<div class="gestion-personal-worker-line">'
                '<span>Fecha de ingreso</span>'
                f'<strong>{escape(fecha_ingreso_texto)}</strong></div>'
                '<div class="gestion-personal-worker-line">'
                '<span>Estado</span>'
                f'<strong class="worker-status-{"active" if perfil.get("activo", True) else "inactive"}">'
                f'{"Activo" if perfil.get("activo", True) else "Inactivo"}</strong>'
                '</div></div>',
                unsafe_allow_html=True,
            )

        historial = _consultar_personal_cacheado(
            consultar_historial_personal,
            nombre,
            cargar=tabs[0].open,
            cache_version=2,
        )
        with col_actividad:
            st.markdown(
                '<div class="gestion-personal-info-card">'
                '<div class="gestion-personal-activity-title">Últimos registros</div>'
                f'{_actividad_reciente_html(historial or [])}</div>',
                unsafe_allow_html=True,
            )

        modalidad_actual = perfil.get("modalidad_pago")
        sueldo = Decimal(str(perfil.get("sueldo_base") or 0))
        bonificacion = _decimal_moneda(
            resumen_trabajador.get("Bonificación (S/)", "S/ 0.00")
        )
        descuento = _decimal_moneda(
            resumen_trabajador.get("Descuento (S/)", "S/ 0.00")
        )
        adelanto = _decimal_moneda(
            resumen_trabajador.get("Adelantos (S/)", "S/ 0.00")
        )
        sueldo_periodo = resumen_trabajador.get(
            "_sueldo_periodo",
            resumen_trabajador.get("Sueldo base (S/)", "—"),
        )
        pagos_realizados = resumen_trabajador.get(
            "_pagos_realizados", "S/ 0.00"
        )
        pendiente = resumen_trabajador.get("Total a pagar (S/)", "—")
        with col_resumen:
            st.markdown(
                '<div class="gestion-personal-info-card">'
                '<div class="gestion-personal-activity-title">'
                'Resumen del periodo</div>'
                '<div class="gestion-personal-summary-line">'
                '<span>Sueldo correspondiente</span>'
                f'<strong>{escape(sueldo_periodo)}</strong></div>'
                '<div class="gestion-personal-summary-line">'
                '<span>Adelantos</span>'
                f'<strong>− {_moneda(adelanto)}</strong></div>'
                '<div class="gestion-personal-summary-line">'
                '<span>Bonificación</span>'
                f'<strong>+ {_moneda(bonificacion)}</strong></div>'
                '<div class="gestion-personal-summary-line">'
                '<span>Descuento</span>'
                f'<strong>− {_moneda(descuento)}</strong></div>'
                '<div class="gestion-personal-summary-line">'
                '<span>Pagos realizados</span>'
                f'<strong>− {escape(pagos_realizados)}</strong></div>'
                '<div class="gestion-personal-summary-line">'
                '<span>Periodo</span>'
                f'<strong>{fecha_inicio_resumen.day:02d}/'
                f'{fecha_inicio_resumen.month:02d} – '
                f'{fecha_fin_resumen.day:02d}/{fecha_fin_resumen.month:02d}'
                '</strong></div>'
                '<div class="gestion-personal-summary-total">'
                'Total a pagar '
                f'<strong>{escape(pendiente)}</strong></div></div>',
                unsafe_allow_html=True,
            )
            importe_pago_actual = max(
                _decimal_moneda(pendiente)
                - _decimal_moneda(pagos_realizados),
                Decimal("0.00"),
            )
            modalidad_pago_actual = perfil.get("modalidad_pago")
            pago_disponible = (
                modalidad_pago_actual
                in {"Diario", "Quincenal", "Mensual", "Semanal"}
                and perfil.get("sueldo_base") is not None
                and pendiente != "—"
                and importe_pago_actual > 0
            )
            with st.container(
                key=f"gestion_personal_registrar_pago_{perfil['id']}"
            ):
                if st.button(
                    "Registrar pago",
                    key=f"registrar_pago_total_{perfil['id']}",
                    type="primary",
                    width="stretch",
                    disabled=not pago_disponible,
                    help=(
                        f"Registrar el saldo actual de {_moneda(importe_pago_actual)} "
                        "como un pago en efectivo."
                        if pago_disponible
                        else "No hay un saldo pendiente disponible para este periodo."
                    ),
                ):
                    bonificacion_pago = (
                        Decimal(
                            str(
                                obtener_bonificacion_mensual(
                                    nombre, fecha_fin_resumen
                                )
                            )
                        )
                        if modalidad_pago_actual == "Mensual"
                        else Decimal("0.00")
                    )
                    try:
                        with st.spinner("Registrando pago total..."):
                            _registrar_pago_total_trabajador(
                                nombre,
                                modalidad_pago_actual,
                                fecha_inicio_resumen,
                                fecha_fin_resumen,
                                importe_pago_actual,
                                adelanto,
                                bonificacion_pago,
                                descuento,
                                actor,
                            )
                    except (
                        ValueError,
                        ArithmeticError,
                        psycopg2.Error,
                    ) as error:
                        st.error(f"No se pudo registrar el pago: {error}")
                    else:
                        _invalidar_consultas_personal()
                        st.session_state[
                            "gestion_personal_pago_exito"
                        ] = (
                            f"Pago de {_moneda(importe_pago_actual)} "
                            f"registrado para {nombre}."
                        )
                        st.rerun()

        if st.session_state.get("gestion_personal_historial_abierto") == perfil["id"]:
            with st.expander("Historial completo del trabajador", expanded=True):
                if historial is None:
                    st.caption("El historial se cargará al abrir Resumen.")
                elif historial:
                    st.dataframe(
                        pd.DataFrame(historial),
                        width="stretch",
                        hide_index=True,
                    )
                else:
                    st.info("Aún no hay movimientos registrados.")

    with tabs[1]:
        fecha_asistencia = st.date_input(
            "Mes de asistencia",
            value=referencia,
            key=f"fecha_asistencia_{perfil['id']}",
        )
        inicio_historial = fecha_asistencia.replace(day=1)
        fin_historial = fecha_asistencia.replace(
            day=calendar.monthrange(
                fecha_asistencia.year, fecha_asistencia.month
            )[1]
        )
        precarga = _obtener_precarga_personal(
            nombres_trabajadores, referencia
        )
        periodo_precargado = (
            precarga.get("periodo_asistencias")
            if precarga
            else None
        )
        if (
            precarga
            and periodo_precargado == (inicio_historial, fin_historial)
        ):
            asistencias_df = precarga["asistencias"]
            historial_asistencia = (
                asistencias_df.loc[
                    asistencias_df["nombre"].str.casefold()
                    == nombre.strip().casefold()
                ]
                .drop(columns=["nombre"])
                .to_dict("records")
            )
        else:
            historial_asistencia = _consultar_personal_cacheado(
                consultar_asistencias_personal,
                nombre,
                inicio_historial,
                fin_historial,
                cargar=tabs[1].open,
            )
        if historial_asistencia is None:
            st.caption("Las métricas se cargarán al abrir esta pestaña.")
        else:
            conteos_asistencia = {
                estado: sum(
                    registro.get("estado") == estado
                    for registro in historial_asistencia
                )
                for estado in ("A", "F", "M")
            }
            etiquetas_estado = {
                "A": "Asistidos",
                "F": "Faltas",
                "M": "Medios días",
            }
            _mostrar_tarjetas_kpi_personal(
                [
                    (
                        "staff",
                        "how_to_reg",
                        etiquetas_estado[estado],
                        str(conteos_asistencia[estado]),
                        f"en {fecha_asistencia:%m/%Y}",
                    )
                    for estado in ("A", "F", "M")
                ]
            )
            if sum(conteos_asistencia.values()):
                datos_asistencia = pd.DataFrame(
                    [
                        {
                            "Estado": etiquetas_estado[estado],
                            "Días": conteos_asistencia[estado],
                        }
                        for estado in ("A", "M", "F")
                    ]
                )
                grafico_asistencia = (
                    alt.Chart(datos_asistencia)
                    .mark_arc(innerRadius=72, outerRadius=118, cornerRadius=3)
                    .encode(
                        theta=alt.Theta("Días:Q", stack=True),
                        color=alt.Color(
                            "Estado:N",
                            scale=alt.Scale(
                                domain=["Asistidos", "Medios días", "Faltas"],
                                range=["#00D6B2", "#FF9B43", "#FF4D67"],
                            ),
                            legend=alt.Legend(title=None),
                        ),
                        tooltip=[
                            alt.Tooltip("Estado:N", title="Estado"),
                            alt.Tooltip("Días:Q", title="Días", format="d"),
                        ],
                    )
                    .properties(height=290)
                    .configure(background="transparent")
                    .configure_legend(
                        labelColor="#DCE6F2",
                        titleColor="#DCE6F2",
                    )
                    .configure_view(strokeOpacity=0)
                )
                with st.container(
                    border=True,
                    key=f"gestion_personal_asistencia_chart_{perfil['id']}",
                ):
                    st.markdown(
                        '<div class="gestion-personal-analytics-title">'
                        "Distribución de asistencias</div>",
                        unsafe_allow_html=True,
                    )
                    st.altair_chart(
                        grafico_asistencia,
                        use_container_width=True,
                    )
            else:
                st.info("No hay asistencias registradas en este mes.")
            st.caption(
                "No se muestra puntualidad porque no hay horarios de ingreso "
                "ni registros de tardanzas."
            )

    with tabs[2]:
        detalle_adelantos = (
            st.session_state[
                "_gestion_personal_obtener_detalle_adelantos_mensuales"
            ](nombre, referencia)
            if tabs[2].open
            else None
        )
        if detalle_adelantos is None:
            st.caption("Las métricas se cargarán al abrir esta pestaña.")
        else:
            total_adelantos = sum(
                (
                    registro["importe"]
                    for registro in detalle_adelantos
                ),
                Decimal("0.00"),
            )
            cantidad_adelantos = sum(
                registro["solicitudes"]
                for registro in detalle_adelantos
            )
            promedio_adelantos = (
                (total_adelantos / cantidad_adelantos).quantize(
                    Decimal("0.01")
                )
                if cantidad_adelantos
                else Decimal("0.00")
            )
            _mostrar_tarjetas_kpi_personal(
                [
                    (
                        "pending",
                        "payments",
                        "Total adelantado",
                        _moneda(total_adelantos),
                        f"en {referencia:%m/%Y}",
                    ),
                    (
                        "salary",
                        "receipt_long",
                        "Solicitudes",
                        str(cantidad_adelantos),
                        "adelantos registrados",
                    ),
                    (
                        "bonus",
                        "functions",
                        "Promedio",
                        _moneda(promedio_adelantos),
                        "por solicitud",
                    ),
                ]
            )
            if detalle_adelantos:
                datos_adelantos = pd.DataFrame(
                    [
                        {
                            "Fecha": registro["fecha"],
                            "Importe": float(registro["importe"]),
                            "Solicitudes": registro["solicitudes"],
                        }
                        for registro in detalle_adelantos
                    ]
                )
                grafico_adelantos = (
                    alt.Chart(datos_adelantos)
                    .mark_bar(
                        cornerRadiusTopLeft=5,
                        cornerRadiusTopRight=5,
                        color="#29A8FF",
                    )
                    .encode(
                        x=alt.X(
                            "Fecha:T",
                            title="Fecha",
                            axis=alt.Axis(format="%d %b", labelColor="#A5B4C7"),
                        ),
                        y=alt.Y(
                            "sum(Importe):Q",
                            title="Importe (S/)",
                            axis=alt.Axis(labelColor="#A5B4C7"),
                        ),
                        tooltip=[
                            alt.Tooltip("Fecha:T", title="Fecha", format="%d/%m/%Y"),
                            alt.Tooltip(
                                "sum(Importe):Q",
                                title="Total adelantado",
                                format=".2f",
                            ),
                            alt.Tooltip(
                                "sum(Solicitudes):Q",
                                title="Solicitudes",
                                format="d",
                            ),
                        ],
                    )
                    .properties(height=290)
                    .interactive()
                    .configure(background="transparent")
                    .configure_view(strokeOpacity=0)
                    .configure_axis(
                        gridColor="#294158",
                        labelColor="#A5B4C7",
                        titleColor="#DCE6F2",
                    )
                )
                with st.container(
                    border=True,
                    key=f"gestion_personal_adelantos_chart_{perfil['id']}",
                ):
                    st.markdown(
                        '<div class="gestion-personal-analytics-title">'
                        "Distribución de adelantos por fecha</div>",
                        unsafe_allow_html=True,
                    )
                    st.altair_chart(
                        grafico_adelantos,
                        use_container_width=True,
                    )
            else:
                st.info("No hay adelantos registrados en este mes.")
            inicio_historial_adelantos = referencia.replace(day=1)
            fin_historial_adelantos = referencia.replace(
                day=calendar.monthrange(referencia.year, referencia.month)[1]
            )
            historial_adelantos = _consultar_personal_cacheado(
                consultar_detalle_adelantos_registros_personal,
                nombre,
                inicio_historial_adelantos,
                fin_historial_adelantos,
                cargar=tabs[2].open,
            )
            with st.expander("Historial de adelantos", expanded=False):
                if historial_adelantos is None:
                    st.caption("El historial se cargará al abrir esta pestaña.")
                elif historial_adelantos:
                    st.markdown(
                        _tabla_historial_detallado_personal(
                            historial_adelantos,
                            [
                                ("fecha", "Fecha y Hora", "fecha"),
                                ("importe", "Monto del Adelanto", "importe"),
                                (
                                    "metodo_pago",
                                    "Método de Pago / Entrega",
                                    "texto",
                                ),
                                ("registrado_por", "Registrado por", "texto"),
                            ],
                            clase_tabla=(
                                "personal-detail-history "
                                "personal-advance-history"
                            ),
                        ),
                        unsafe_allow_html=True,
                    )
                else:
                    st.info("No hay adelantos registrados en este periodo.")

    with tabs[3]:
        desglose_bonificacion = (
            st.session_state[
                "_gestion_personal_obtener_desglose_bonificacion_mensual"
            ](nombre, referencia)
            if tabs[3].open
            else None
        )
        if desglose_bonificacion is None:
            st.caption("El resumen se cargará al abrir esta pestaña.")
        else:
            col_bonificacion, col_grafico_bonificacion = st.columns(
                [0.8, 1.5],
                gap="medium",
            )
            with col_bonificacion:
                _mostrar_tarjetas_kpi_personal(
                    [
                        (
                            "bonus",
                            "workspace_premium",
                            "Bonificación acumulada",
                            _moneda(bonificacion_mensual),
                            f"en {referencia:%m/%Y}",
                        )
                    ]
                )
            with col_grafico_bonificacion:
                datos_bonificacion = pd.DataFrame(
                    [
                        {
                            "Tipo de servicio": "Solitario",
                            "Servicios": desglose_bonificacion["solitarios"],
                            "Monto producido": (
                                f'S/ {desglose_bonificacion["monto_solitario"]:.2f}'
                            ),
                        },
                        {
                            "Tipo de servicio": "Compartido",
                            "Servicios": desglose_bonificacion["compartidos"],
                            "Monto producido": (
                                f'S/ {desglose_bonificacion["monto_compartido"]:.2f}'
                            ),
                        },
                        {
                            "Tipo de servicio": "Grupal",
                            "Servicios": desglose_bonificacion["grupales"],
                            "Monto producido": (
                                f'S/ {desglose_bonificacion["monto_grupal"]:.2f}'
                            ),
                        },
                    ]
                )
                total_servicios = int(datos_bonificacion["Servicios"].sum())
                if total_servicios:
                    grafico_bonificacion = (
                        alt.Chart(datos_bonificacion)
                        .mark_arc(
                            innerRadius=68,
                            outerRadius=108,
                            cornerRadius=3,
                        )
                        .encode(
                            theta=alt.Theta("Servicios:Q", stack=True),
                            color=alt.Color(
                                "Tipo de servicio:N",
                                scale=alt.Scale(
                                    domain=["Solitario", "Compartido", "Grupal"],
                                    range=["#20D38A", "#36B8FF", "#B779F7"],
                                ),
                                legend=alt.Legend(title=None),
                            ),
                            tooltip=[
                                alt.Tooltip(
                                    "Tipo de servicio:N",
                                    title="Tipo de servicio",
                                ),
                                alt.Tooltip(
                                    "Servicios:Q",
                                    title="Servicios",
                                    format="d",
                                ),
                                alt.Tooltip(
                                    "Monto producido:N",
                                    title="Monto producido",
                                ),
                            ],
                        )
                        .properties(height=290)
                        .configure(background="transparent")
                        .configure_legend(
                            labelColor="#DCE6F2",
                            titleColor="#DCE6F2",
                        )
                        .configure_view(strokeOpacity=0)
                    )
                    with st.container(
                        border=True,
                        key=f"gestion_personal_bonificacion_chart_{perfil['id']}",
                    ):
                        st.markdown(
                            '<div class="gestion-personal-analytics-title">'
                            "Trabajo por tipo de servicio</div>",
                            unsafe_allow_html=True,
                        )
                        st.altair_chart(
                            grafico_bonificacion,
                            use_container_width=True,
                        )
                else:
                    st.info(
                        "No hay servicios registrados en este mes."
                    )
            historial_servicios = _consultar_personal_cacheado(
                consultar_detalle_servicios_personal,
                nombre,
                referencia.replace(day=1),
                referencia.replace(
                    day=calendar.monthrange(
                        referencia.year, referencia.month
                    )[1]
                ),
                cargar=tabs[3].open,
            )
            with st.expander("Historial de bonificaciones", expanded=False):
                if historial_servicios is None:
                    st.caption("El historial se cargará al abrir esta pestaña.")
                elif historial_servicios:
                    importes_sin_redondear = [
                        Decimal(str(servicio["monto"]))
                        / Decimal(str(servicio["participantes"]))
                        / Decimal("30")
                        for servicio in historial_servicios
                    ]
                    importes_redondeados = (
                        _redondear_bonificaciones_personal(
                            importes_sin_redondear,
                            bonificacion_mensual,
                        )
                    )
                    historial_bonificaciones = []
                    for servicio, importe_bonificacion in zip(
                        historial_servicios,
                        importes_redondeados,
                    ):
                        tipo_servicio = str(
                            servicio.get("tipo") or "Servicio"
                        )
                        nombre_servicio = str(
                            servicio.get("servicio") or tipo_servicio
                        )
                        historial_bonificaciones.append(
                            {
                                "fecha": servicio["fecha"],
                                "concepto": "Bono diario",
                                "motivo": (
                                    f"{nombre_servicio} ({tipo_servicio}) · "
                                    "cuota parte"
                                ),
                                "importe": importe_bonificacion,
                                "registrado_por": servicio.get(
                                    "registrado_por"
                                ),
                            }
                        )
                    st.caption(
                        "Los céntimos se distribuyen para que el total del "
                        "historial coincida con la bonificación del periodo."
                    )
                    st.markdown(
                        _tabla_historial_detallado_personal(
                            historial_bonificaciones,
                            [
                                ("fecha", "Fecha y Hora", "fecha"),
                                ("concepto", "Concepto", "texto"),
                                ("motivo", "Motivo", "texto"),
                                ("importe", "Monto Calculado (S/)", "importe"),
                                ("registrado_por", "Registrado por", "texto"),
                            ],
                            clase_monto=(
                                "personal-history-amount "
                                "personal-bonus-amount"
                            ),
                            clase_tabla=(
                                "personal-detail-history "
                                "personal-bonus-history"
                            ),
                        ),
                        unsafe_allow_html=True,
                    )
                else:
                    st.info(
                        "No hay bonificaciones registradas en este periodo."
                    )

    with tabs[4]:
        st.markdown("**Pagos registrados**")
        pagos = _consultar_personal_cacheado(
            consultar_pagos_personal,
            nombre,
            cargar=tabs[4].open,
            cache_version=2,
        )
        if pagos is None:
            st.caption("Abre esta pestaña para cargar los pagos.")
        elif pagos:
            st.markdown(
                _tabla_importe_fecha_registro(pagos),
                unsafe_allow_html=True,
            )
        else:
            st.info("No hay pagos registrados.")

@st.fragment
def render_gestion_personal(
    nombres_trabajadores,
    referencia_predeterminada,
    actor,
    obtener_bonificacion_mensual,
):
    _estilos_personal()
    st.session_state[
        "_gestion_personal_obtener_bonificacion_mensual"
    ] = obtener_bonificacion_mensual
    obtener_adelantos_registrados = st.session_state[
        "_gestion_personal_obtener_adelantos_registrados"
    ]
    mensaje_pago = st.session_state.pop(
        "gestion_personal_pago_exito", None
    )
    if mensaje_pago:
        st.success(mensaje_pago)
    if (
        not st.session_state.get("gestion_personal_asistencia_abierto")
        and st.session_state.pop(
            "gestion_personal_asistencia_limpiar", False
        )
    ):
        for clave in list(st.session_state):
            if clave == "gestion_personal_asistencia_fecha" or clave.startswith(
                "gestion_personal_asistencia_20"
            ) or clave.startswith(
                "gestion_personal_asistencia_v2_"
            ):
                st.session_state.pop(clave, None)
    if st.session_state.pop(
        "gestion_personal_asistencia_guardada", False
    ):
        st.success("Asistencia guardada correctamente.")
    col_titulo, col_periodo = st.columns([1.65, 1])
    with col_titulo:
        st.markdown(
            '<div class="gestion-personal-heading-rule"></div>'
            '<div class="gestion-personal-heading">'
            '<span class="gestion-personal-heading-icon">groups</span>'
            'Gestión de <span class="brand">Personal</span></div>'
            '<p class="gestion-personal-subtitle">'
            'Controla la asistencia, sueldos, adelantos y pagos de tu equipo de trabajo.'
            '</p>',
            unsafe_allow_html=True,
        )
    with col_periodo:
        st.markdown(
            '<div class="gestion-personal-period-label">'
            '<span><i class="gestion-personal-heading-icon">'
            'calendar_month</i> Periodos según modalidad</span></div>',
            unsafe_allow_html=True,
        )
        referencia = st.date_input(
            "Fecha de referencia",
            value=referencia_predeterminada,
            key="gestion_personal_referencia",
            format="DD/MM/YYYY",
            label_visibility="collapsed",
        )
    precarga = _obtener_precarga_personal(
        nombres_trabajadores, referencia
    )
    perfiles = (
        precarga["perfiles"].to_dict("records")
        if precarga
        else _consultar_personal_cacheado(
            consultar_perfiles_personal,
            nombres_trabajadores,
            cache_version=_PERSONAL_QUERY_SCHEMA_VERSION,
        )
    )
    perfiles_por_nombre = {
        perfil["nombre"].strip().casefold(): perfil for perfil in perfiles
    }
    periodos_por_modalidad = {
        modalidad: _periodo_liquidacion(modalidad, referencia)
        for modalidad in ("Diario", "Quincenal", "Mensual", "Semanal")
    }
    periodo_inicio, periodo_fin = periodos_por_modalidad["Mensual"]
    diario_inicio, _ = periodos_por_modalidad["Diario"]
    quincenal_inicio, quincenal_fin = periodos_por_modalidad["Quincenal"]
    st.markdown(
        '<div class="gestion-personal-period-label"><span>'
        f'Diario: {diario_inicio:%d/%m/%Y} · '
        f'Quincenal: {quincenal_inicio:%d/%m/%Y} – '
        f'{quincenal_fin:%d/%m/%Y} · '
        f'Mensual: {periodo_inicio:%d/%m/%Y} – '
        f'{periodo_fin:%d/%m/%Y} · '
        f'Semanal: {periodos_por_modalidad["Semanal"][0]:%d/%m/%Y} – '
        f'{periodos_por_modalidad["Semanal"][1]:%d/%m/%Y}'
        '</span></div>',
        unsafe_allow_html=True,
    )
    perfiles_activos = [
        perfil for perfil in perfiles if perfil["activo"]
    ]
    modalidades_activas = {
        perfil["modalidad_pago"]
        for perfil in perfiles_activos
        if perfil["modalidad_pago"] in periodos_por_modalidad
    }
    resumenes_por_modalidad = {
        modalidad: {
            fila["id"]: fila
            for fila in _consultar_personal_cacheado(
                consultar_resumen_personal_periodo,
                *periodo_modalidad,
                cache_version=_PERSONAL_QUERY_SCHEMA_VERSION,
            )
        }
        for modalidad, periodo_modalidad in periodos_por_modalidad.items()
        if modalidad in modalidades_activas
    }

    filas_vista = []
    sueldo_total = Decimal("0.00")
    bonificacion_total = Decimal("0.00")
    pendiente_total = Decimal("0.00")
    fecha_actual = datetime.now(ZoneInfo("America/Lima")).date()
    for perfil in perfiles_activos:
        sueldo_configurado = perfil.get("sueldo_base") is not None
        bonificacion_acumulada = Decimal(
            str(obtener_bonificacion_mensual(perfil["nombre"], referencia))
        )
        fecha_limite_pago = _fecha_limite_pago(
            perfil.get("modalidad_pago"), referencia
        )
        modalidad_perfil = perfil.get("modalidad_pago")
        periodo_perfil = periodos_por_modalidad.get(modalidad_perfil)
        if periodo_perfil is None:
            periodo_perfil = (periodo_inicio, periodo_fin)
        inicio_periodo_perfil, fin_periodo_perfil = periodo_perfil
        fila_periodo = resumenes_por_modalidad.get(
            modalidad_perfil, {}
        ).get(perfil["id"], {})
        existe_liquidacion = fila_periodo.get("liquidacion_id") is not None
        adelantos_registrados = Decimal(
            str(
                obtener_adelantos_registrados(
                    perfil["nombre"],
                    inicio_periodo_perfil,
                    fin_periodo_perfil,
                )
            )
        )
        sueldo_correspondiente = (
            _sueldo_correspondiente(
                modalidad_perfil,
                perfil["sueldo_base"],
                fila_periodo,
            )
            if sueldo_configurado
            else None
        )
        descuento_calculado = (
            _descuento_por_faltas(
                modalidad_perfil,
                perfil["sueldo_base"],
                fila_periodo,
            )
            if sueldo_configurado
            else Decimal("0.00")
        )
        bonificacion_mostrar = bonificacion_acumulada
        bonificacion_total += bonificacion_mostrar
        if sueldo_correspondiente is not None:
            bonificacion = (
                Decimal(fila_periodo.get("bonificacion_liquidada") or 0)
                if modalidad_perfil == "Mensual" and existe_liquidacion
                else (
                    bonificacion_mostrar
                    if modalidad_perfil == "Mensual"
                    else Decimal("0.00")
                )
            )
            descuento = (
                Decimal(fila_periodo.get("descuento_liquidado") or 0)
                if existe_liquidacion
                else descuento_calculado
            )
            pagado = Decimal(fila_periodo.get("pagado") or 0)
            estado, saldo = _estado_pago(
                sueldo_correspondiente + bonificacion - descuento,
                adelantos_registrados,
                pagado,
                existe_liquidacion,
                periodo_vencido=(
                    fecha_limite_pago is not None
                    and fecha_actual > fecha_limite_pago
                ),
            )
            sueldo_total += sueldo_correspondiente
            pendiente_total += saldo
            total_mostrar = _moneda(
                max(
                    sueldo_correspondiente
                    - adelantos_registrados
                    + bonificacion
                    - descuento,
                    Decimal("0.00"),
                )
            )
            sueldo_periodo_mostrar = _moneda(sueldo_correspondiente)
            base_mostrar = (
                _moneda(perfil["sueldo_base"])
                if modalidad_perfil == "Diario"
                else sueldo_periodo_mostrar
            )
            adelanto_mostrar = _moneda(adelantos_registrados)
            bonificacion_mostrar = (
                f"S/ {float(bonificacion_mostrar):.2f}"
            )
            descuento_mostrar = _moneda(descuento)
            pagos_mostrar = _moneda(pagado)
            asistencia_mostrar = (
                "A: {asistio} · F: {falto} · M: {medio_dia}".format(
                    asistio=fila_periodo.get("asistio", 0),
                    falto=fila_periodo.get("falto", 0),
                    medio_dia=fila_periodo.get("medio_dia", 0),
                )
            )
        else:
            estado = "⚪ Configurar"
            base_mostrar = (
                _moneda(perfil.get("sueldo_base"))
                if sueldo_configurado else "Configurar"
            )
            bonificacion_mostrar = (
                f"S/ {float(bonificacion_mostrar):.2f}"
            )
            adelanto_mostrar = _moneda(adelantos_registrados)
            descuento_mostrar = _moneda(0)
            total_mostrar = "—"
            pagos_mostrar = _moneda(fila_periodo.get("pagado") or 0)
            asistencia_mostrar = (
                "A: {asistio} · F: {falto} · M: {medio_dia}".format(
                    asistio=fila_periodo.get("asistio", 0),
                    falto=fila_periodo.get("falto", 0),
                    medio_dia=fila_periodo.get("medio_dia", 0),
                )
                if any(
                    fila_periodo.get(clave, 0)
                    for clave in ("asistio", "falto", "medio_dia")
                )
                else "Pendiente"
            )
            sueldo_periodo_mostrar = base_mostrar
        filas_vista.append(
            {
                "Nombre": perfil["nombre"],
                "Cargo": perfil.get("cargo") or "Sin configurar",
                "Asistencia": asistencia_mostrar,
                "Sueldo base (S/)": base_mostrar,
                "Tipo de pago": (
                    perfil.get("modalidad_pago") or "Sin configurar"
                ),
                "Adelantos (S/)": adelanto_mostrar,
                "Bonificación (S/)": bonificacion_mostrar,
                "Descuento (S/)": descuento_mostrar,
                "_pagos_realizados": pagos_mostrar,
                "_sueldo_periodo": sueldo_periodo_mostrar,
                "Fecha a pagar": (
                    fecha_limite_pago.strftime("%d/%m/%Y")
                    if fecha_limite_pago
                    else "—"
                ),
                "Total a pagar (S/)": total_mostrar,
                "Estado de pago": estado,
            }
        )
    _estilos_tarjetas = [
        (
            "staff", "groups", "Total de colaboradores",
            str(len(perfiles_activos)), "registrados",
        ),
        (
            "salary", "payments", "Sueldo total",
            _moneda(sueldo_total), "periodo correspondiente a cada modalidad",
        ),
        (
            "bonus", "workspace_premium", "Bonificación acumulada",
            _moneda(bonificacion_total), "mes de referencia · se suma al periodo mensual",
        ),
        (
            "pending", "pending_actions", "Total pendiente de pago",
            _moneda(pendiente_total), "menos adelantos y pagos registrados",
        ),
    ]
    tarjetas = "".join(
        '<div class="gestion-personal-kpi '
        f'{clase}"><div class="gestion-personal-kpi-icon">{icono}</div>'
        '<div class="gestion-personal-kpi-copy">'
        f'<div class="gestion-personal-kpi-label">{escape(titulo)}</div>'
        f'<div class="gestion-personal-kpi-value">{escape(valor)}</div>'
        '</div></div>'
        for clase, icono, titulo, valor in (
            tarjeta[:4] for tarjeta in _estilos_tarjetas
        )
    )
    st.markdown(
        f'<div class="gestion-personal-kpis">{tarjetas}</div>',
        unsafe_allow_html=True,
    )

    with st.container(border=True, key="gestion_personal_table_panel"):
        col_tabla_titulo, col_busqueda, col_asistencia = st.columns(
            [1.3, 1.5, .8], vertical_alignment="center"
        )
        with col_tabla_titulo:
            st.markdown(
                '<div class="gestion-personal-table-title">'
                '<span class="gestion-personal-heading-icon">groups</span>'
                'Registro de Personal</div>',
                unsafe_allow_html=True,
            )
        with col_busqueda:
            busqueda = st.text_input(
                "Buscar trabajador",
                placeholder="Buscar colaborador...",
                key="gestion_personal_busqueda",
                label_visibility="collapsed",
            ).strip().casefold()
        with col_asistencia:
            if st.button(
                "Asistencia",
                key="gestion_personal_asistencia_button",
                type="primary",
                icon=":material/calendar_month:",
                help="Registrar o actualizar la asistencia del equipo",
                width="stretch",
            ):
                st.session_state["gestion_personal_asistencia_abierto"] = True
                st.session_state["gestion_personal_editar_abierto"] = None
                st.session_state[
                    "gestion_personal_calendario_asistencia_abierto"
                ] = False
                st.session_state[
                    "gestion_personal_calendario_trabajador_id"
                ] = None
                st.session_state[
                    "gestion_personal_asistencia_fecha"
                ] = _fecha_actual_personal()
        filas_filtradas = [
            fila for fila in filas_vista
            if (
                not busqueda
                or busqueda in fila["Nombre"].casefold()
                or busqueda in fila["Cargo"].casefold()
            )
        ]
        if filas_filtradas:
            pesos_columnas = [
                0.34, 1.2, 0.88, 0.9, 1, 0.72, 0.84, 0.84, 0.84, 1, 0.9,
                0.82, 0.34,
            ]
            columnas_fila = [
                clave
                for clave in filas_filtradas[0]
                if clave not in {"_pagos_realizados", "_sueldo_periodo"}
            ]
            encabezados = [
                "N.º",
                *columnas_fila,
                "Acciones",
            ]
            with st.container(key="gestion_personal_table_grid"):
                with st.container(key="gestion_personal_grid_header"):
                    columnas = st.columns(pesos_columnas, gap="small")
                    for columna, encabezado in zip(columnas, encabezados):
                        with columna:
                            st.markdown(
                                '<div class="gestion-personal-grid-header-cell">'
                                f'{escape(encabezado)}</div>',
                                unsafe_allow_html=True,
                            )

                for indice, fila in enumerate(filas_filtradas, start=1):
                    perfil_fila = perfiles_por_nombre[
                        fila["Nombre"].strip().casefold()
                    ]
                    key_fila = (
                        "gestion_personal_fila_par"
                        if indice % 2 == 0
                        else "gestion_personal_fila_impar"
                    )
                    with st.container(
                        key=f"{key_fila}_{perfil_fila['id']}"
                    ):
                        columnas = st.columns(pesos_columnas, gap="small")
                        valores = {
                            "N.º": str(indice),
                            **{
                                clave: fila[clave]
                                for clave in columnas_fila
                            },
                        }
                        for columna, (clave, valor) in zip(
                            columnas[:-1], valores.items()
                        ):
                            with columna:
                                if clave == "Asistencia":
                                    with st.container(
                                        key=(
                                            "gestion_personal_asistencia_celda_"
                                            f"{perfil_fila['id']}"
                                        )
                                    ):
                                        if st.button(
                                            " ",
                                            key=(
                                                "gestion_personal_calendario_"
                                                f"{perfil_fila['id']}"
                                            ),
                                            icon=":material/visibility:",
                                            help=(
                                                "Consultar calendario mensual "
                                                f"de {fila['Nombre']}"
                                            ),
                                            type="tertiary",
                                        ):
                                            st.session_state[
                                                "gestion_personal_calendario_asistencia_abierto"
                                            ] = True
                                            st.session_state[
                                                "gestion_personal_editar_abierto"
                                            ] = None
                                            st.session_state[
                                                "gestion_personal_asistencia_abierto"
                                            ] = False
                                            st.session_state[
                                                "gestion_personal_calendario_trabajador_id"
                                            ] = perfil_fila["id"]
                                            st.session_state[
                                                "gestion_personal_calendario_mes_"
                                                f"{perfil_fila['id']}"
                                            ] = referencia.replace(day=1)
                                            st.rerun(scope="app")
                                elif (
                                    clave == "Tipo de pago"
                                    and str(valor) not in {
                                        "Mensual",
                                        "Quincenal",
                                        "Diario",
                                        "Semanal",
                                    }
                                ):
                                    clave_configurar = (
                                        "gestion_personal_configurar_"
                                        f"{perfil_fila['id']}"
                                    )
                                    if st.button(
                                        "Configurar",
                                        key=f"{clave_configurar}_button",
                                        type="tertiary",
                                        help=(
                                            "Configurar modalidad de pago "
                                            f"para {fila['Nombre']}"
                                        ),
                                        wrap=False,
                                    ):
                                        _seleccionar_accion_personal(
                                            perfil_fila["id"],
                                            fila["Nombre"],
                                            "Editar",
                                        )
                                else:
                                    contenido = _celda_tabla_personal(
                                        clave,
                                        valor,
                                        fila.get("Estado de pago", ""),
                                    )
                                    clase = (
                                        "gestion-personal-grid-cell money-cell"
                                        if clave in {
                                            "Sueldo base (S/)",
                                            "Adelantos (S/)",
                                            "Bonificación (S/)",
                                            "Descuento (S/)",
                                            "Total a pagar (S/)",
                                        }
                                        else "gestion-personal-grid-cell"
                                    )
                                    st.markdown(
                                        f'<div class="{clase}">{contenido}</div>',
                                        unsafe_allow_html=True,
                                    )
                        clave_editar = (
                            f"gestion_personal_action_editar_"
                            f"{perfil_fila['id']}"
                        )
                        with columnas[-1]:
                            with st.container(key=clave_editar):
                                if st.button(
                                    " ",
                                    key=f"{clave_editar}_button",
                                    icon=":material/edit:",
                                    help=f"Editar {fila['Nombre']}",
                                    type="tertiary",
                                ):
                                    _seleccionar_accion_personal(
                                        perfil_fila["id"],
                                        fila["Nombre"],
                                        "Editar",
                                    )
        else:
            st.info("No hay trabajadores que coincidan con la búsqueda.")
        col_tabla_ayuda, col_exportar = st.columns([3, 1])
        with col_tabla_ayuda:
            st.caption(
                "Para pagos diarios: A cuenta como 1 día, M como medio día "
                "y F como 0. Mensual, quincenal y semanal descuentan el día "
                "faltado y un día libre por cada semana con faltas."
            )
        with col_exportar:
            df_exportar = pd.DataFrame(
                [
                    {
                        clave: valor
                        for clave, valor in fila.items()
                        if clave not in {
                            "_pagos_realizados",
                            "_sueldo_periodo",
                        }
                    }
                    for fila in filas_filtradas
                ]
            )
            st.download_button(
                "Exportar",
                data=df_exportar.to_csv(index=False).encode("utf-8-sig"),
                file_name="registro_personal.csv",
                mime="text/csv",
                key="exportar_gestion_personal",
                icon=":material/download:",
                width="stretch",
            )

    nombres_para_detalle = [perfil["nombre"] for perfil in perfiles]
    if not nombres_para_detalle:
        st.info(
            "Agrega trabajadores desde Gestión para configurar sus sueldos."
        )
        return
    _mostrar_detalle_trabajador(
        nombres_para_detalle,
        perfiles_por_nombre,
        filas_vista,
        referencia,
        actor,
        obtener_bonificacion_mensual,
        periodo_inicio,
        periodo_fin,
    )
    nombre = st.session_state["gestion_personal_trabajador_seleccionado"]
    perfil = perfiles_por_nombre[nombre.strip().casefold()]
    if st.session_state.get("gestion_personal_editar_abierto") == perfil["id"]:
        _dialogo_personal(
            "editar",
            nombre=nombre,
            perfil=perfil,
            actor=actor,
            referencia=referencia,
        )
    elif st.session_state.get("gestion_personal_asistencia_abierto"):
        _dialogo_personal("asistencia", perfiles=perfiles, actor=actor)
    elif st.session_state.get(
        "gestion_personal_calendario_asistencia_abierto"
    ):
        perfil_calendario_id = st.session_state.get(
            "gestion_personal_calendario_trabajador_id"
        )
        perfil_calendario = next(
            (
                perfil
                for perfil in perfiles
                if perfil["id"] == perfil_calendario_id
            ),
            None,
        )
        if perfil_calendario is None:
            st.session_state[
                "gestion_personal_calendario_asistencia_abierto"
            ] = False
            st.error(
                "No se encontró el trabajador del calendario de asistencia."
            )
        else:
            _dialogo_personal(
                "calendario",
                nombre=perfil_calendario["nombre"],
                perfil=perfil_calendario,
            )
