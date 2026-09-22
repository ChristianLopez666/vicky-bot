"""
Menu con emojis, cierre del embudo de auto y lecturas de Sheets (22-sep).

En la prueba real del 22-sep, tras mandar sus documentos del seguro de auto,
el cliente escribio "gracias" y "ya te los envie" y Vicky le volvio a pedir los
documentos. Ademas SECOM leia la hoja completa en cada webhook (incluidos los
acuses de Meta) y Google la corto con 429 a los pocos mensajes.
"""

from types import SimpleNamespace
from unittest.mock import patch

import app as vicky

PHONE = "5216681110022"


def _limpiar():
    vicky.user_state.clear()
    vicky.user_data.clear()
    vicky._cierre_ctx.clear()


def test_el_menu_conserva_las_siete_opciones_y_los_atajos():
    menu = vicky.MAIN_MENU
    for n, palabra in enumerate(
        ["IMSS", "Auto", "Vida", "VRIM", "Empresarial", "Financiamiento", "Christian"], start=1
    ):
        assert f"{n}️⃣" in menu
        assert palabra in menu


def test_gracias_despues_de_los_documentos_cierra_el_embudo_de_auto():
    _limpiar()
    enviados = []
    with patch.object(vicky, "send_message", side_effect=lambda p, t: enviados.append(t) or True), \
         patch.object(vicky, "_nudge_ensure_sweeper"):
        vicky.auto_start(PHONE, None)
        vicky._ensure_user(PHONE)["auto_docs_recibidos"] = True
        vicky._auto_next(PHONE, "gracias")

    assert "Ya tengo tus documentos" in enviados[-1]
    assert vicky.user_state[PHONE] == "__greeted__"
    assert PHONE in vicky._cierre_ctx

    # El siguiente "gracias" ya es cortesia, no otra peticion de documentos.
    with patch.object(vicky, "send_message", side_effect=lambda p, t: enviados.append(t) or True), \
         patch.object(vicky, "_nudge_ensure_sweeper"):
        assert vicky._cierre_manejar_cortesia(PHONE, "gracias") is True
    assert "gusto atenderle" in enviados[-1]


def test_ya_te_los_envie_con_documentos_no_los_vuelve_a_pedir():
    _limpiar()
    enviados = []
    with patch.object(vicky, "send_message", side_effect=lambda p, t: enviados.append(t) or True), \
         patch.object(vicky, "_nudge_ensure_sweeper"):
        vicky.auto_start(PHONE, None)
        vicky._ensure_user(PHONE)["auto_docs_recibidos"] = True
        vicky._auto_next(PHONE, "ya te los envie")
    assert "Ya tengo tus documentos" in enviados[-1]
    assert "enviando los *documentos*" not in enviados[-1]


def test_gracias_sin_documentos_responde_con_cortesia_y_sigue_esperando():
    _limpiar()
    enviados = []
    with patch.object(vicky, "send_message", side_effect=lambda p, t: enviados.append(t) or True):
        vicky.auto_start(PHONE, None)
        vicky._auto_next(PHONE, "gracias")
    assert enviados[-1].startswith("Con gusto")
    assert vicky.user_state[PHONE] == "auto_intro"


def test_un_nuevo_embudo_de_auto_olvida_los_documentos_anteriores():
    _limpiar()
    with patch.object(vicky, "send_message", return_value=True):
        vicky._ensure_user(PHONE)["auto_docs_recibidos"] = True
        vicky.auto_start(PHONE, None)
    assert "auto_docs_recibidos" not in vicky._ensure_user(PHONE)


class _Hoja:
    def __init__(self):
        self.lecturas = 0
        self.escrituras = 0

    def get(self, **_):
        def execute():
            self.lecturas += 1
            return {"values": [["Nombre", "WhatsApp"], ["Yomero", "6681620521"]]}
        return SimpleNamespace(execute=execute)

    def batchUpdate(self, **_):
        def execute():
            self.escrituras += 1
            return {}
        return SimpleNamespace(execute=execute)


def _entorno(hoja, segundos=15):
    return (
        patch.object(vicky, "google_ready", True),
        patch.object(vicky, "sheets_svc", object()),
        patch.object(vicky, "SHEETS_ID_LEADS", "hoja-prueba"),
        patch.object(vicky, "_sheets_values", return_value=hoja),
        patch.object(vicky, "SHEETS_READ_CACHE_SECONDS", segundos),
    )


def test_varios_webhooks_seguidos_leen_la_hoja_una_sola_vez():
    vicky._sheet_rows_invalidate()
    hoja = _Hoja()
    a, b, c, d, e = _entorno(hoja)
    with a, b, c, d, e:
        for _ in range(10):
            assert vicky.match_client_in_sheets("6681620521")["nombre"] == "Yomero"
    assert hoja.lecturas == 1


def test_una_escritura_propia_obliga_a_leer_de_nuevo():
    vicky._sheet_rows_invalidate()
    hoja = _Hoja()
    a, b, c, d, e = _entorno(hoja)
    with a, b, c, d, e:
        headers, _ = vicky._sheet_get_rows()
        vicky._update_row_cells(2, {"Nombre": "Otro"}, headers)
        vicky._sheet_get_rows()
    assert hoja.lecturas == 2


def test_con_cache_en_cero_siempre_lee():
    vicky._sheet_rows_invalidate()
    hoja = _Hoja()
    a, b, c, d, e = _entorno(hoja, segundos=0)
    with a, b, c, d, e:
        vicky._sheet_get_rows()
        vicky._sheet_get_rows()
    assert hoja.lecturas == 2


def test_quien_modifica_las_filas_no_altera_la_copia_guardada():
    vicky._sheet_rows_invalidate()
    hoja = _Hoja()
    a, b, c, d, e = _entorno(hoja)
    with a, b, c, d, e:
        _, rows = vicky._sheet_get_rows()
        rows[0][0] = "Cambiado"
        _, otra = vicky._sheet_get_rows()
    assert otra[0][0] == "Yomero"
