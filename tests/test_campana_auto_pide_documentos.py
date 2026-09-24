"""Campana de auto: un "si" a la plantilla entra al embudo del menu (opcion 2)
y pide INE y tarjeta de circulacion. Las demas plantillas no cambian."""

from unittest.mock import patch

import pytest

import app as vicky

PHONE = "5216681234567"
MATCH = {"row": 5, "nombre": "Cliente Prueba", "estatus": "ENVIADO_SEGURO_AUTO"}


@pytest.fixture(autouse=True)
def entorno():
    vicky.user_state.clear()
    vicky.user_data.clear()
    with patch.object(vicky, "send_message", return_value=True) as send, \
         patch.object(vicky, "_notify_advisor") as notify, \
         patch.object(vicky, "_safe_update_row_cells") as update, \
         patch.object(vicky, "_is_recent_awaiting_template_context", return_value=True), \
         patch.object(vicky, "_cierre_registrar"):
        yield send, notify, update
    vicky.user_state.clear()
    vicky.user_data.clear()


def _textos(send):
    return [c.args[1] for c in send.call_args_list]


@pytest.mark.parametrize("respuesta", ["Sí", "si", "me interesa"])
def test_si_a_plantilla_auto_pide_ine_y_tarjeta(entorno, respuesta):
    send, notify, update = entorno
    vicky.user_state[PHONE] = "awaiting_info:seguro_auto_70"

    assert vicky._handle_awaiting_template_response(PHONE, respuesta, MATCH) is True

    assert vicky.user_state[PHONE] == "auto_intro"
    assert any("INE" in t and "Tarjeta de circulación" in t for t in _textos(send))
    assert not any("Ya registré tu interés" in t for t in _textos(send))
    notify.assert_called_once()
    assert update.call_args.args[1]["ESTATUS"] == "INTERESADO_TEMPLATE"


def test_si_a_otra_plantilla_no_cambia(entorno):
    send, notify, _ = entorno
    vicky.user_state[PHONE] = "awaiting_info:vida_temporal_v2"

    assert vicky._handle_awaiting_template_response(PHONE, "Sí", MATCH) is True

    assert vicky.user_state[PHONE] == "__greeted__"
    assert any("Ya registré tu interés" in t for t in _textos(send))
    notify.assert_called_once()


def test_no_a_plantilla_auto_no_entra_al_embudo(entorno):
    send, _, update = entorno
    vicky.user_state[PHONE] = "awaiting_info:seguro_auto_70"

    assert vicky._handle_awaiting_template_response(PHONE, "no", MATCH) is True

    assert vicky.user_state[PHONE] == "__greeted__"
    assert update.call_args.args[1]["ESTATUS"] == "NO_INTERESADO_TEMPLATE"


def test_documentos_despues_del_si_cierran_con_gracias(entorno):
    send, _, _ = entorno
    vicky.user_state[PHONE] = "awaiting_info:seguro_auto_70"
    vicky._handle_awaiting_template_response(PHONE, "Sí", MATCH)
    vicky._ensure_user(PHONE)["auto_docs_recibidos"] = True

    vicky._auto_next(PHONE, "Gracias")

    assert any("Ya tengo tus documentos" in t for t in _textos(send))
    assert vicky.user_state[PHONE] == "__greeted__"
