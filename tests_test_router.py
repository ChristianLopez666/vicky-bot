from core_router import handle_incoming_message

def test_menu_words():
    assert "Vicky" in handle_incoming_message("5210000000000", "menu")
    assert "Vicky" in handle_incoming_message("5210000000000", "hola")

def test_options():
    assert "pensiones" in handle_incoming_message("521", "1").lower()
    assert "auto" in handle_incoming_message("521", "2").lower()
    assert "vida" in handle_incoming_message("521", "3").lower()
    assert "vrim" in handle_incoming_message("521", "4").lower()
    assert "préstamos" in handle_incoming_message("521", "5").lower()
    assert "financiamiento" in handle_incoming_message("521", "6").lower()
    assert "nómina" in handle_incoming_message("521", "7").lower()
    assert "Notifiqué" in handle_incoming_message("521", "8")
