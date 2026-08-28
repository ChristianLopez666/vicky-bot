# Auditoria forense SECOM (2026-08-28): estas pruebas fijan el invariante del
# que depende la Fase 6 del monitor de correo (Apps Script, fuera de este
# repo) para distinguir un evento real automatizado de una edicion
# administrativa hecha a mano en Sheets: TODO escritura de ESTATUS que nazca
# de una interaccion real del bot debe estampar LAST_MESSAGE_AT en la MISMA
# llamada. Antes de este fix, vida_start/vida_objetivo/TPV escribian ESTATUS
# sin LAST_MESSAGE_AT, lo que habria apagado sus alertas por error si se
# aplicaba esa heuristica sin corregir estos 4 sitios primero.
from unittest.mock import patch

import pytest

import app as vicky


PHONE = "5216681234567"


@pytest.fixture(autouse=True)
def clean_state():
    vicky.user_state.clear()
    vicky.user_data.clear()
    yield
    vicky.user_state.clear()
    vicky.user_data.clear()


@pytest.fixture
def no_boardroom():
    with patch.object(vicky, "BOARDROOM_ENABLED", False):
        yield


@pytest.fixture
def no_external_io(no_boardroom):
    with patch.object(vicky, "send_message", return_value=True), \
         patch.object(vicky, "_notify_advisor"), \
         patch.object(vicky, "match_client_in_sheets", return_value=None), \
         patch.object(vicky, "append_respuesta_cliente"):
        yield


def test_vida_start_stamps_last_message_at(no_external_io):
    with patch.object(vicky, "_safe_update_row_cells") as update:
        vicky.vida_start(PHONE, {"row": 5, "nombre": "Ana"})

    updates = update.call_args.args[1]
    assert updates["ESTATUS"] == "interesado"
    assert updates.get("LAST_MESSAGE_AT")


def test_vida_objetivo_close_stamps_last_message_at(no_external_io):
    match = {"row": 2, "nombre": "Ana"}
    vicky.vida_start(PHONE, match)
    vicky._vida_next(PHONE, "45", match)
    vicky._vida_next(PHONE, "no", match)
    vicky._vida_next(PHONE, "Sinaloa", match)
    vicky._vida_next(PHONE, "1 millón", match)

    with patch.object(vicky, "_safe_update_row_cells") as update:
        vicky._vida_next(PHONE, "1", match)

    updates = update.call_args.args[1]
    assert updates["ESTATUS"] == "perfil_inicial_capturado"
    assert updates.get("LAST_MESSAGE_AT")


def test_tpv_interesado_stamps_last_message_at(no_external_io):
    vicky.user_state[PHONE] = "tpv_giro"
    vicky._tpv_next(PHONE, "restaurante", {"row": 3, "nombre": "Luis"})
    vicky._tpv_next(PHONE, "hoy 4pm", {"row": 3, "nombre": "Luis"})

    with patch.object(vicky, "_sheet_get_rows", return_value=(["ESTATUS", "LAST_MESSAGE_AT"], [])), \
         patch.object(vicky, "_update_row_cells") as update:
        vicky.user_state[PHONE] = "tpv_giro"
        vicky._tpv_next(PHONE, "restaurante", {"row": 3, "nombre": "Luis"})
        vicky._tpv_next(PHONE, "hoy 4pm", {"row": 3, "nombre": "Luis"})

    updates = update.call_args.args[1]
    assert updates["ESTATUS"] == "TPV_INTERESADO"
    assert updates.get("LAST_MESSAGE_AT")


def test_tpv_no_interesado_stamps_last_message_at(no_external_io):
    with patch.object(vicky, "_sheet_get_rows", return_value=(["ESTATUS", "LAST_MESSAGE_AT"], [])), \
         patch.object(vicky, "_update_row_cells") as update:
        vicky.user_state[PHONE] = "tpv_motivo"
        vicky._tpv_next(PHONE, "omitir", {"row": 3, "nombre": "Luis"})

    updates = update.call_args.args[1]
    assert updates["ESTATUS"] == "TPV_NO_INTERESADO"
    assert updates.get("LAST_MESSAGE_AT")
