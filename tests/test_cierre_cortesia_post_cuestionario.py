"""
Cierre de cortesia post-cuestionario (Vicky SECOM).

Al terminar un embudo, SECOM mandaba el resumen y el menu y ahi se acababa:
no agradecia el tiempo del cliente, y un "gracias" posterior caia en el
fallback generico de _route_command ("En breve, su asesor Christian Lopez se
pondra en contacto...") como si fuera una consulta nueva.

Esta bateria cubre las cuatro piezas del cierre:
  1. acuse automatico en cuanto la solicitud queda registrada,
  2. respuesta de cortesia sin genero, con invitacion al menu, una sola vez,
  3. despedida cuando el cliente responde que no,
  4. recordatorio a la hora si no responde, entregado una sola vez y cancelado
     por cualquier mensaje entrante.
"""

import time
from unittest.mock import patch

import app as vicky
import cierre_cortesia as cc

PHONE = "5216681110000"


def _limpiar():
    vicky.user_state.clear()
    vicky.user_data.clear()
    vicky._cierre_ctx.clear()


def _cerrar_imss(send):
    """Deja al cliente en el ultimo paso del embudo IMSS y lo cierra."""
    _limpiar()
    vicky.user_state[PHONE] = "imss_nomina"
    vicky.user_data[PHONE] = {
        "imss_nombre": "Juan Perez", "imss_ciudad": "Los Mochis",
        "imss_pension": 12000, "imss_monto": 100000,
    }
    vicky._imss_next(PHONE, "no")
    return [c.args[1] for c in send.call_args_list]


# ── 1. Acuse automatico ───────────────────────────────────────────────────────

def test_el_cierre_del_embudo_agradece_sin_esperar_otro_mensaje():
    with patch.object(vicky, "send_message") as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        textos = _cerrar_imss(send)

    assert cc.ACUSE in textos, textos
    # Va despues del resumen preautorizado, no antes.
    assert textos.index(cc.ACUSE) == len(textos) - 1
    assert PHONE in vicky._cierre_ctx


def test_el_cierre_deja_armado_el_recordatorio():
    with patch.object(vicky, "send_message") as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)

    ctx = vicky._cierre_ctx[PHONE]
    assert ctx["producto"] == "imss"
    assert ctx["nudge_due"] - time.time() > vicky.CIERRE_NUDGE_SECONDS - 60


# ── 2. Cortesia sin genero ────────────────────────────────────────────────────

def test_gracias_recibe_cortesia_sin_genero_y_no_el_fallback_generico():
    with patch.object(vicky, "send_message") as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)
        send.reset_mock()
        manejado = vicky._cierre_manejar_cortesia(PHONE, "muchas gracias")

    assert manejado is True
    esperado = cc.cortesia_final("imss")
    assert [c.args[1] for c in send.call_args_list] == [esperado]
    assert "atenderle" in esperado
    assert "menú" in esperado
    assert "por ser pensionado" in esperado


def test_la_cortesia_no_se_repite_si_el_cliente_agradece_dos_veces():
    with patch.object(vicky, "send_message") as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)
        vicky._cierre_manejar_cortesia(PHONE, "gracias")
        send.reset_mock()
        manejado = vicky._cierre_manejar_cortesia(PHONE, "ok gracias")

    assert manejado is True
    send.assert_not_called()


# ── 3. Respuesta negativa ─────────────────────────────────────────────────────

def test_una_negativa_agradece_el_tiempo_y_cancela_el_recordatorio():
    with patch.object(vicky, "send_message") as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)
        vicky._cierre_manejar_cortesia(PHONE, "gracias")
        send.reset_mock()
        manejado = vicky._cierre_manejar_cortesia(PHONE, "no gracias")

    assert manejado is True
    assert [c.args[1] for c in send.call_args_list] == [cc.DESPEDIDA_NEGATIVA]
    assert vicky._cierre_ctx[PHONE]["nudge_due"] is None


def test_tras_la_despedida_una_cortesia_mas_no_vuelve_a_ofrecer_nada():
    with patch.object(vicky, "send_message") as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)
        vicky._cierre_manejar_cortesia(PHONE, "no gracias")
        send.reset_mock()
        manejado = vicky._cierre_manejar_cortesia(PHONE, "gracias")

    assert manejado is True
    send.assert_not_called()


def test_un_mensaje_con_contenido_libera_el_contexto_y_se_rutea():
    with patch.object(vicky, "send_message") as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)
        send.reset_mock()
        manejado = vicky._cierre_manejar_cortesia(PHONE, "gracias, tambien quiero cotizar mi auto")

    assert manejado is False
    send.assert_not_called()
    assert PHONE not in vicky._cierre_ctx


# ── 4. Recordatorio a la hora ─────────────────────────────────────────────────

def test_el_recordatorio_se_entrega_una_sola_vez_cuando_vence():
    with patch.object(vicky, "send_message") as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)
        vicky._cierre_ctx[PHONE]["nudge_due"] = time.time() - 1
        send.reset_mock()
        send.return_value = True

        assert vicky.nudge_sweep_once() == 1
        assert [c.args[1] for c in send.call_args_list] == [cc.NUDGE]

        send.reset_mock()
        assert vicky.nudge_sweep_once() == 0
        send.assert_not_called()


def _webhook(text: str):
    """Entra por la misma puerta que produccion: /webhook. Es lo que hace la
    diferencia entre probar la funcion y probar el requisito."""
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": PHONE, "id": "wamid.cierre", "type": "text", "text": {"body": text}}
    ]}}]}]}
    return vicky.app.test_client().post("/webhook", json=payload)


def test_la_oferta_del_menu_tambien_lleva_recordatorio_a_la_hora():
    """Recorrido real: cierre -> "gracias" por /webhook -> cortesia. Esa
    cortesia termina con "escriba menu si requiere algun otro servicio": la
    oferta queda abierta, asi que le corresponde la misma hora de espera. El
    webhook cancela el recordatorio al entrar el mensaje; lo que se comprueba
    es que la respuesta de Vicky lo vuelve a dejar armado."""
    with patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "match_client_in_sheets", return_value=None), \
         patch.object(vicky, "append_respuesta_cliente"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)
        send.reset_mock()

        rv = _webhook("muchas gracias")

        assert rv.status_code == 200
        assert [c.args[1] for c in send.call_args_list] == [cc.cortesia_final("imss")]
        assert vicky._cierre_ctx[PHONE]["nudge_due"] - time.time() > vicky.CIERRE_NUDGE_SECONDS - 60

        # Y al vencer, se entrega.
        send.reset_mock()
        vicky._cierre_ctx[PHONE]["nudge_due"] = time.time() - 1
        assert vicky.nudge_sweep_once() == 1
        assert [c.args[1] for c in send.call_args_list] == [cc.NUDGE]


def test_el_recordatorio_no_se_entrega_dos_veces_en_el_mismo_ciclo():
    """Si el cliente se quedo callado, ya recibio "Quedo atenta..." y despues
    escribe "gracias", la cortesia NO le vuelve a programar la misma frase."""
    with patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "match_client_in_sheets", return_value=None), \
         patch.object(vicky, "append_respuesta_cliente"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)
        vicky._cierre_ctx[PHONE]["nudge_due"] = time.time() - 1
        assert vicky.nudge_sweep_once() == 1

        send.reset_mock()
        _webhook("gracias")

        assert [c.args[1] for c in send.call_args_list] == [cc.cortesia_final("imss")]
        assert vicky._cierre_ctx[PHONE]["nudge_due"] is None
        assert vicky.nudge_sweep_once() == 0


def test_si_el_cliente_responde_que_no_el_recordatorio_ya_no_existe():
    with patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "match_client_in_sheets", return_value=None), \
         patch.object(vicky, "append_respuesta_cliente"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)
        send.reset_mock()

        rv = _webhook("no gracias")

        assert rv.status_code == 200
        assert [c.args[1] for c in send.call_args_list] == [cc.DESPEDIDA_NEGATIVA]
        assert vicky._cierre_ctx[PHONE]["nudge_due"] is None
        assert vicky.nudge_sweep_once() == 0


def test_tras_la_cortesia_una_segunda_negativa_tampoco_arma_nada():
    """La cortesia deja una oferta abierta ("escriba menu..."). Ni esa oferta
    ni la despedida posterior vuelven a armar el recordatorio."""
    with patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "match_client_in_sheets", return_value=None), \
         patch.object(vicky, "append_respuesta_cliente"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)
        _webhook("gracias")
        _webhook("no, gracias")

        assert vicky._cierre_ctx[PHONE]["nudge_due"] is None
        assert vicky.nudge_sweep_once() == 0


def test_un_recordatorio_muy_atrasado_ya_no_se_entrega():
    with patch.object(vicky, "send_message") as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)
        vicky._cierre_ctx[PHONE]["nudge_due"] = time.time() - vicky.CIERRE_NUDGE_MAX_ATRASO - 60
        send.reset_mock()

        assert vicky.nudge_sweep_once() == 0
        send.assert_not_called()


def test_un_contexto_expirado_ya_no_atiende_la_cortesia():
    with patch.object(vicky, "send_message") as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "send_main_menu"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_imss(send)
        vicky._cierre_ctx[PHONE]["ts"] = time.time() - vicky.CIERRE_VENTANA_SECONDS - 60
        send.reset_mock()
        manejado = vicky._cierre_manejar_cortesia(PHONE, "gracias")

    assert manejado is False
    send.assert_not_called()


# ── 4b. Cuestionario Vida Temporal ────────────────────────────────────────────
# Era el unico cuestionario sin cierre de cortesia: la ruta de interes a la
# plantilla Vida si estaba cubierta, pero esa es otra puerta (respuesta a
# campana), no el cuestionario completo. Es ademas el embudo que SECOM esta
# operando hoy.

def _cerrar_vida(send):
    _limpiar()
    vicky.user_state[PHONE] = "vida_objetivo"
    vicky.user_data[PHONE] = {"edad": 45, "fuma": "no", "estado": "Sinaloa",
                              "suma": "1 millón"}
    vicky._vida_next(PHONE, "1")
    return [c.args[1] for c in send.call_args_list]


def test_el_cierre_del_cuestionario_vida_abre_el_contexto_de_cortesia():
    with patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        textos = _cerrar_vida(send)

    ctx = vicky._cierre_ctx[PHONE]
    assert ctx["producto"] == "vida"
    assert ctx["nudge_due"] - time.time() > vicky.CIERRE_NUDGE_SECONDS - 60
    # Su mensaje de cierre ya agradece y ya anuncia el seguimiento de
    # Christian: no se le encima el acuse.
    assert cc.ACUSE not in textos
    assert any("Seguro de Vida Temporal" in t for t in textos), textos


def test_gracias_tras_el_cuestionario_vida_no_ofrece_tarifa_de_pensionado():
    with patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "match_client_in_sheets", return_value=None), \
         patch.object(vicky, "append_respuesta_cliente"), \
         patch.object(vicky, "CIERRE_NUDGE_SWEEPER", False):
        _cerrar_vida(send)
        send.reset_mock()
        _webhook("gracias")

    enviados = [c.args[1] for c in send.call_args_list]
    assert enviados == [cc.cortesia_final("vida")]
    assert "por ser pensionado" not in enviados[0]
    # La oferta del menu queda abierta: el recordatorio se reprograma.
    assert vicky._cierre_ctx[PHONE]["nudge_due"] is not None


# ── 5. Textos y clasificadores ────────────────────────────────────────────────

def test_la_tarifa_por_ser_pensionado_solo_se_ofrece_tras_el_embudo_imss():
    assert "por ser pensionado" in cc.cortesia_final("imss")
    # A quien no viene del embudo IMSS no se le promete una tarifa que
    # depende de estar pensionado.
    assert "por ser pensionado" not in cc.cortesia_final("empresarial")
    assert "tarifa preferencial" in cc.cortesia_final("empresarial")
    # A quien acaba de cerrar el embudo de auto no se le vuelve a ofrecer auto.
    assert "seguro para su auto" not in cc.cortesia_final("auto")


def test_clasificadores_de_cortesia_y_negativa():
    for texto in ("gracias", "Muchas gracias!", "ok", "perfecto", "de acuerdo"):
        assert cc.es_cortesia_pura(texto), texto
    for texto in ("", "gracias, quiero cotizar auto", "cuanto me prestan"):
        assert not cc.es_cortesia_pura(texto), texto

    for texto in ("no", "no gracias", "por ahora no", "así está bien, gracias"):
        assert cc.es_respuesta_negativa(texto), texto
    for texto in ("", "gracias", "no entiendo", "no, mejor el seguro de auto"):
        assert not cc.es_respuesta_negativa(texto), texto
