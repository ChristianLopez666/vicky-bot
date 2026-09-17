"""Ampliacion urgente del 17-sep: entrega real del aviso por plantilla, rechazo
tardio de Meta, payload valido de advisor_notification y un transporte de Google
por hilo (el worker que murio con SIGSEGV)."""
import threading
from unittest.mock import patch

import app as vicky

ADVISOR = "5216682478005"


def _limpiar():
    vicky._advisor_sent.clear()
    vicky._avisos_fallidos_vistos.clear()
    vicky._advisor_window_expire()


# ── Entrega real: ventana y plantilla ────────────────────────────────────────

def test_si_consta_que_la_ventana_esta_cerrada_se_va_directo_a_la_plantilla():
    _limpiar()
    with patch.object(vicky, "ADVISOR_NUMBER", ADVISOR), \
         patch.object(vicky, "ADVISOR_ALERT_TEMPLATE", "asesor_lead_v1"), \
         patch.object(vicky, "_advisor_window_state", return_value="cerrada"), \
         patch.object(vicky, "send_message") as libre, \
         patch.object(vicky, "send_template_message", return_value={"ok": True, "wamid": "wamid.tpl"}), \
         patch.object(vicky, "_log_conversacion"):
        resultado = vicky._notify_advisor("🧠 Lead de Vida\npara ti")

    libre.assert_not_called()
    assert resultado["ok"] is True and resultado["nivel"] == "template"


def test_si_la_ventana_esta_abierta_se_usa_texto_libre():
    _limpiar()
    vicky._advisor_window_open()
    with patch.object(vicky, "ADVISOR_NUMBER", ADVISOR), \
         patch.object(vicky, "ADVISOR_ALERT_TEMPLATE", "asesor_lead_v1"), \
         patch.object(vicky, "send_message", return_value={"ok": True, "wamid": "wamid.1"}), \
         patch.object(vicky, "send_template_message") as tpl, \
         patch.object(vicky, "_log_conversacion"):
        resultado = vicky._notify_advisor("aviso")

    tpl.assert_not_called()
    assert resultado["nivel"] == "texto_libre" and resultado["ok"] is True


def test_cuando_christian_escribe_su_ventana_se_reabre():
    _limpiar()
    assert vicky._advisor_window_state() == "desconocida"
    vicky._advisor_window_open()
    assert vicky._advisor_window_state() == "abierta"


def test_el_parametro_de_plantilla_va_en_una_sola_linea():
    # Meta rechaza 132000/132012 con saltos de linea o espacios repetidos.
    limpio = vicky._sanitize_template_param("🧠 Lead de Vida\n\nNombre:  Yomero\tTel: 123")
    assert "\n" not in limpio and "\t" not in limpio and "  " not in limpio
    assert vicky._sanitize_template_param("   ") == vicky.TPL_PARAM_FALLBACK


def test_sin_plantilla_configurada_el_aviso_queda_pendiente_pero_no_se_pierde():
    _limpiar()
    with patch.object(vicky, "ADVISOR_NUMBER", ADVISOR), \
         patch.object(vicky, "ADVISOR_ALERT_TEMPLATE", ""), \
         patch.object(vicky, "send_message", return_value={"ok": False, "motivo": "131047"}), \
         patch.object(vicky, "_log_conversacion"), \
         patch.object(vicky, "_registrar_alerta_pendiente") as alerta:
        resultado = vicky._notify_advisor("aviso")

    assert resultado["ok"] is False
    alerta.assert_called_once()


# ── Rechazo tardio de Meta (el caso real de las 13:05) ───────────────────────

def _status_fallido(wamid="wamid.libre", codigo=131047):
    return {"wamid": wamid, "status": "failed", "recipient": ADVISOR,
            "error_code": codigo, "error_title": "Re-engagement message",
            "occurred_at": "2026-09-17T20:04:59.000Z"}


def test_el_rechazo_tardio_reenvia_por_plantilla_y_deja_la_alerta():
    _limpiar()
    vicky._advisor_window_open()
    vicky._advisor_remember("wamid.libre", "texto_libre", "🧠 Lead de Vida para ti", "r-1")

    with patch.object(vicky, "ADVISOR_NUMBER", ADVISOR), \
         patch.object(vicky, "ADVISOR_ALERT_TEMPLATE", "asesor_lead_v1"), \
         patch.object(vicky, "send_template_message", return_value={"ok": True, "wamid": "wamid.tpl"}) as tpl, \
         patch.object(vicky, "_registrar_alerta_pendiente") as alerta:
        resultado = vicky._atender_aviso_no_entregado(_status_fallido())

    assert resultado == "entregado_por_plantilla"
    tpl.assert_called_once()
    assert alerta.call_args.args[1]["ok"] is True
    # Y la contabilidad de la ventana se corrige: Meta dijo que no.
    assert vicky._advisor_window_state() == "desconocida"


def test_el_mismo_rechazo_reentregado_no_reenvia_dos_veces():
    _limpiar()
    vicky._advisor_remember("wamid.libre", "texto_libre", "aviso", "r-1")
    with patch.object(vicky, "ADVISOR_ALERT_TEMPLATE", "asesor_lead_v1"), \
         patch.object(vicky, "send_template_message", return_value={"ok": True, "wamid": "wamid.tpl"}) as tpl, \
         patch.object(vicky, "_registrar_alerta_pendiente"):
        vicky._atender_aviso_no_entregado(_status_fallido())
        vicky._atender_aviso_no_entregado(_status_fallido())

    assert tpl.call_count == 1


def test_si_la_plantilla_tambien_falla_no_hay_nivel_superior():
    _limpiar()
    vicky._advisor_remember("wamid.tpl", "template", "aviso", "r-2")
    with patch.object(vicky, "send_template_message") as tpl, \
         patch.object(vicky, "_registrar_alerta_pendiente") as alerta:
        resultado = vicky._atender_aviso_no_entregado(_status_fallido(wamid="wamid.tpl"))

    tpl.assert_not_called()
    assert resultado == "pendiente"
    alerta.assert_called_once()


def test_un_fallo_que_no_es_del_asesor_no_dispara_nada():
    _limpiar()
    with patch.object(vicky, "_registrar_alerta_pendiente") as alerta:
        assert vicky._atender_aviso_no_entregado(_status_fallido(wamid="wamid.de_un_prospecto")) is None
    alerta.assert_not_called()


# ── Payload de advisor_notification ──────────────────────────────────────────

def test_el_payload_del_aviso_lleva_telefono_y_resultado_real():
    with patch.object(vicky, "ADVISOR_NUMBER", ADVISOR):
        payload = vicky._advisor_notification_payload(
            {"ok": False, "nivel": "texto_libre", "motivo": "131047"})

    assert payload["to"].startswith("521")
    assert payload["to"].isdigit()
    assert payload["channel"] == "whatsapp"
    assert payload["delivered"] is False
    assert payload["level"] == "texto_libre"
    assert payload["reason"] == "131047"


# ── Un transporte de Google por hilo ─────────────────────────────────────────

def test_cada_hilo_tiene_su_propia_cache_de_recursos():
    servicio = object()
    vistos = {}

    def construir(_svc):
        return object()

    principal = vicky._cached_resource("prueba", servicio, construir)
    assert vicky._cached_resource("prueba", servicio, construir) is principal

    def en_otro_hilo():
        vistos["otro"] = vicky._cached_resource("prueba", servicio, construir)

    hilo = threading.Thread(target=en_otro_hilo)
    hilo.start()
    hilo.join()

    # Distinto hilo, distinto recurso: es lo que evita el SIGSEGV.
    assert vistos["otro"] is not principal


def test_el_hilo_principal_sigue_usando_el_cliente_compartido():
    assert vicky._sheets_service() is vicky.sheets_svc
