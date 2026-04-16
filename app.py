from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request

from bootstrap import load_app_config
from config_models import AppConfig
from funnels import FunnelEngine, MAIN_MENU
from services import (
    ConversationStateStore,
    DecisionLayerClient,
    GoogleWorkspaceGateway,
    InternalAuthGuard,
    LeadMatch,
    PromoQueueItem,
    SendResult,
    WhatsAppClient,
    normalize_phone_last10,
    normalize_to_e164_mx,
    now_iso,
)

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - depende de entorno
    OpenAI = None

log = logging.getLogger("vicky-secom.app")


@dataclass(slots=True)
class AppContext:
    config: AppConfig
    state_store: ConversationStateStore
    google: GoogleWorkspaceGateway
    whatsapp: WhatsAppClient
    decision_layer: DecisionLayerClient
    funnels: FunnelEngine
    auth_guard: InternalAuthGuard
    openai_client: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.config, AppConfig):
            raise ValueError("config must be an AppConfig")
        if not isinstance(self.state_store, ConversationStateStore):
            raise ValueError("state_store must be a ConversationStateStore")
        if not isinstance(self.google, GoogleWorkspaceGateway):
            raise ValueError("google must be a GoogleWorkspaceGateway")
        if not isinstance(self.whatsapp, WhatsAppClient):
            raise ValueError("whatsapp must be a WhatsAppClient")
        if not isinstance(self.decision_layer, DecisionLayerClient):
            raise ValueError("decision_layer must be a DecisionLayerClient")
        if not isinstance(self.funnels, FunnelEngine):
            raise ValueError("funnels must be a FunnelEngine")
        if not isinstance(self.auth_guard, InternalAuthGuard):
            raise ValueError("auth_guard must be an InternalAuthGuard")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "env": self.config.env,
            "port": self.config.port,
            "debug": self.config.debug,
            "google_ready": self.google.ready,
            "state_backend": self.config.state_store.backend,
            "decision_layer_active": self.config.decision_layer.active,
            "openai_active": bool(self.openai_client),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)


def _extract_auth_token() -> str:
    auth_header = (request.headers.get("Authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return (request.headers.get("X-AUTO-TOKEN") or request.args.get("token") or "").strip()


def _mask_payload(payload: Dict[str, Any], *, max_chars: int) -> str:
    raw = json.dumps(payload, ensure_ascii=False)
    return raw[:max_chars]


def _build_openai_client(config: AppConfig) -> Any:
    if not config.openai.active or OpenAI is None:
        return None
    try:
        return OpenAI(api_key=config.openai.api_key)
    except Exception:
        log.exception("No fue posible inicializar OpenAI")
        return None


def create_app(
    *,
    config: Optional[AppConfig] = None,
    config_overrides: Optional[List[str]] = None,
) -> Flask:
    app = Flask(__name__)
    app_config = config or load_app_config(
        config_dir=Path(__file__).resolve().parent / "conf",
        overrides=config_overrides,
    )

    context = AppContext(
        config=app_config,
        state_store=ConversationStateStore(app_config.state_store),
        google=GoogleWorkspaceGateway(app_config.google),
        whatsapp=WhatsAppClient(app_config.whatsapp),
        decision_layer=DecisionLayerClient(app_config.decision_layer),
        funnels=FunnelEngine(app_config.funnels),
        auth_guard=InternalAuthGuard(app_config.security),
        openai_client=_build_openai_client(app_config),
    )
    app.extensions["vicky_context"] = context

    def ctx() -> AppContext:
        value = app.extensions.get("vicky_context")
        if not isinstance(value, AppContext):
            raise RuntimeError("AppContext no inicializado")
        return value

    def send_message(to: str, text: str) -> SendResult:
        result = ctx().whatsapp.send_text(to, text)
        if not result.ok:
            log.warning("Fallo enviando mensaje a %s: %s", to, result.to_dict())
        return result

    def send_template_message(to: str, template_name: str, params: Dict[str, Any] | List[Any]) -> SendResult:
        result = ctx().whatsapp.send_template(to, template_name, params)
        ctx().google.append_envio_status(to, result.message_id, "sent" if result.ok else "failed", template_name, now_iso())
        if not result.ok:
            log.warning("Fallo enviando plantilla %s a %s: %s", template_name, to, result.to_dict())
        return result

    def notify_advisor(text: str) -> None:
        advisor = ctx().config.whatsapp.advisor_number
        if not advisor:
            return
        send_message(advisor, text)

    def apply_outcome(phone: str, outcome: Any, match: Optional[LeadMatch]) -> None:
        if outcome.next_state is not None:
            ctx().state_store.set_state(phone, outcome.next_state)
        if outcome.data_updates:
            ctx().state_store.patch_data(phone, outcome.data_updates)
        for text in outcome.messages:
            send_message(phone, text)
        for note in outcome.advisor_notifications:
            notify_advisor(note)
        if outcome.followup_note and outcome.followup_date_iso:
            ctx().google.write_followup("auto_recordatorio", outcome.followup_note, outcome.followup_date_iso)
        if outcome.lead_updates and match:
            try:
                ctx().google.update_row_cells(match.row, outcome.lead_updates)
            except Exception:
                log.exception("No fue posible actualizar lead row=%s", match.row)

    def get_lead_match(phone: str) -> Optional[LeadMatch]:
        return ctx().google.match_client(normalize_phone_last10(phone))

    def process_text_message(phone: str, text: str, match: Optional[LeadMatch]) -> None:
        state = ctx().state_store.get_state(phone)
        data = ctx().state_store.get_data(phone)
        nombre = match.nombre.strip() if match else ""
        ctx().google.append_respuesta_cliente(phone, nombre, text, now_iso())

        # Decision Layer no bloqueante
        decision = ctx().decision_layer.process_inbound(phone, text, nombre=nombre)
        boardroom_notified = bool(data.get("boardroom_notified"))
        vicky_hint = str(data.get("vicky_hint") or "")
        if isinstance(decision, dict):
            vicky_hint = str(decision.get("vicky_context_hint") or "")
            patch = {
                "vicky_hint": vicky_hint,
                "campaign_bonus_eligible": bool(decision.get("campaign_bonus_eligible")),
            }
            if decision.get("route_to") == "VICKY_CAMPANAS" and str(decision.get("existing_client", "")) != "true":
                notify_advisor(
                    "🧠 BOARDROOM — Lead enrutable a VICKY_CAMPANAS\n"
                    f"Teléfono: {phone}\n"
                    f"Interés: {decision.get('interest') or '-'}\n"
                    f"Campaña activa: {decision.get('active_campaign') or '-'}\n"
                    f"Hint: {vicky_hint or '-'}"
                )
                patch["boardroom_notified"] = True
                boardroom_notified = True
            ctx().state_store.patch_data(phone, patch)

        # Interceptor seguro para comando interno sgpt:
        text_lower = text.lower().strip()
        if text_lower.startswith(ctx().config.openai.command_prefix.lower()) and ctx().openai_client:
            allowed = ctx().config.openai.allow_public_command or normalize_phone_last10(phone) in {
                normalize_phone_last10(number) for number in ctx().config.security.allowed_debug_numbers
            }
            if not allowed:
                send_message(phone, "Ese comando no está habilitado para este número.")
                return
            prompt = text.split(":", 1)[1].strip()
            if not prompt:
                send_message(phone, "Falta el contenido después del prefijo sgpt:.")
                return
            try:
                completion = ctx().openai_client.chat.completions.create(
                    model=ctx().config.openai.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=ctx().config.openai.temperature,
                )
                answer = completion.choices[0].message.content.strip()
                send_message(phone, answer)
                return
            except Exception:
                log.exception("Error llamando a OpenAI")
                send_message(phone, "Hubo un detalle al procesar tu solicitud. Intentemos de nuevo.")
                return

        template_outcome = ctx().funnels.handle_template_info(
            phone=phone,
            text=text,
            state=state,
            last_template_name=ctx().google.get_last_envio_template(normalize_phone_last10(phone)),
            match=match,
        )
        if template_outcome.handled:
            apply_outcome(phone, template_outcome, match)
            return

        outcome = ctx().funnels.handle_text(
            phone=phone,
            text=text,
            state=state,
            data=data,
            match=match,
            nombre_hint=vicky_hint,
        )
        apply_outcome(phone, outcome, match)

        normalized = text_lower
        valid_commands = {
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
            "7",
            "menu",
            "menú",
            "inicio",
            "hola",
            "imss",
            "ley 73",
            "prestamo",
            "préstamo",
            "pension",
            "pensión",
            "auto",
            "seguro auto",
            "seguros de auto",
            "vida",
            "salud",
            "seguro de vida",
            "seguro de salud",
            "vrim",
            "tarjeta medica",
            "tarjeta médica",
            "empresarial",
            "pyme",
            "credito",
            "crédito",
            "credito empresarial",
            "crédito empresarial",
            "financiamiento",
            "financiamiento practico",
            "financiamiento práctico",
            "contactar",
            "asesor",
            "contactar con christian",
        }
        if not normalized.isdigit() and normalized not in valid_commands and not boardroom_notified:
            notify_advisor(f"📩 Cliente INTERESADO / DUDA detectada\nWhatsApp: {phone}\nMensaje: {text}")

    def process_media_message(phone: str, message: Dict[str, Any], match: Optional[LeadMatch]) -> None:
        media_type = message.get("type")
        media_id = None
        if media_type in {"image", "document", "audio", "video"}:
            media_id = (message.get(media_type) or {}).get("id")
        if not media_id:
            send_message(phone, "Recibí tu archivo, gracias. (No se pudo identificar el contenido).")
            return
        ctx().whatsapp.forward_media_to_advisor(media_type, media_id)
        file_bytes, mime_type, file_name = ctx().whatsapp.download_media(media_id)
        if not file_bytes:
            send_message(phone, "Recibí tu archivo, pero hubo un problema procesándolo.")
            return
        last4 = normalize_phone_last10(phone)[-4:]
        folder_name = (
            f"{match.nombre.replace(' ', '_')}_{last4}" if match and match.nombre else f"Cliente_{last4}"
        )
        link = ctx().google.upload_to_drive(
            file_name or f"media_{media_id}",
            file_bytes,
            mime_type or "application/octet-stream",
            folder_name,
        )
        notify_advisor(
            "🔔 Multimedia recibida\n"
            f"Desde: {phone}\n"
            f"Archivo: {file_name or media_id}\n"
            f"Drive: {link or '(sin link Drive)'}"
        )
        send_message(phone, "✅ *Recibido y en proceso*. En breve te doy seguimiento.")

    @app.get("/health")
    def health() -> Any:
        return jsonify({"status": "ok", "service": "Vicky Bot Inbursa Refactor", "timestamp": now_iso()}), 200

    @app.get("/ext/health")
    def ext_health() -> Any:
        detail = {
            "status": "ok",
            "env": ctx().config.env,
            "queue_size": ctx().state_store.queue_size(),
            "whatsapp_configured": True,
            "google": ctx().google.health_summary(),
            "decision_layer_active": ctx().config.decision_layer.active,
            "openai_active": bool(ctx().openai_client),
        }
        if not ctx().config.security.expose_detailed_health:
            detail["google"] = {"google_ready": ctx().google.ready}
        return jsonify(detail), 200

    @app.get("/webhook")
    def webhook_verify() -> Any:
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge", "")
        if mode == "subscribe" and token == ctx().config.whatsapp.verify_token:
            log.info("Webhook verificado exitosamente")
            return challenge, 200
        return "Error", 403

    @app.post("/webhook")
    def webhook_receive() -> Any:
        payload = request.get_json(silent=True) or {}
        if ctx().config.logging.redact_payloads:
            log.info("Webhook recibido (%s chars)", len(json.dumps(payload, ensure_ascii=False)))
        else:
            log.info("Webhook recibido: %s", _mask_payload(payload, max_chars=ctx().config.logging.max_payload_log_chars))

        all_messages: List[Dict[str, Any]] = []
        all_statuses: List[Dict[str, Any]] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                all_messages.extend(value.get("messages", []))
                all_statuses.extend(value.get("statuses", []))

        for status in all_statuses:
            try:
                if (status.get("status") or "").lower() == "failed":
                    log.warning("Status failed recibido: %s", json.dumps(status, ensure_ascii=False)[:500])
            except Exception:
                log.exception("Error procesando status update")

        processed_messages = 0
        for message in all_messages:
            phone = message.get("from")
            if not phone:
                continue
            try:
                match = get_lead_match(phone)
                message_type = message.get("type")
                if message_type == "text" and "text" in message:
                    process_text_message(phone, message["text"].get("body", "").strip(), match)
                    processed_messages += 1
                    continue
                if message_type in {"image", "document", "audio", "video"}:
                    process_media_message(phone, message, match)
                    processed_messages += 1
                    continue
                log.info("Tipo de mensaje no manejado: %s", message_type)
            except Exception:
                log.exception("Error procesando mensaje individual")

        return jsonify(
            {
                "ok": True,
                "processed_messages": processed_messages,
                "statuses_received": len(all_statuses),
            }
        ), 200

    @app.post("/ext/test-send")
    def ext_test_send() -> Any:
        token = _extract_auth_token()
        if ctx().config.security.require_auth_on_test_send and not ctx().auth_guard.is_authorized(token):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        body = request.get_json(silent=True) or {}
        to = str(body.get("to", "")).strip()
        text = str(body.get("text", "")).strip()
        if not to or not text:
            return jsonify({"ok": False, "error": "Faltan parámetros 'to' o 'text'"}), 400
        result = send_message(to, text)
        return jsonify({"ok": result.ok, "message_id": result.message_id, "status_code": result.status_code}), 200

    @app.post("/ext/send-promo")
    def ext_send_promo() -> Any:
        token = _extract_auth_token()
        if not ctx().auth_guard.is_authorized(token):
            return jsonify({"queued": False, "error": "unauthorized"}), 401
        body = request.get_json(silent=True) or {}
        items = body.get("items", [])
        if not isinstance(items, list) or not items:
            return jsonify({"queued": False, "error": "Lista 'items' inválida o vacía"}), 400
        valid_items: List[PromoQueueItem] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                valid_items.append(
                    PromoQueueItem(
                        to=str(item.get("to", "")),
                        text=str(item.get("text", "")),
                        template=str(item.get("template", "")),
                        params=item.get("params", {}),
                    )
                )
            except Exception:
                log.warning("Item inválido en send-promo: %s", item)
        if not valid_items:
            return jsonify({"queued": False, "error": "No hay items válidos para enviar"}), 400
        ctx().state_store.enqueue_many(valid_items)
        return jsonify(
            {
                "queued": True,
                "queue_size": ctx().state_store.queue_size(),
                "valid_items": len(valid_items),
                "timestamp": now_iso(),
            }
        ), 202

    @app.get("/ext/ping-advisor")
    def ext_ping_advisor() -> Any:
        token = _extract_auth_token()
        if not ctx().auth_guard.is_authorized(token):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        result = send_message(
            ctx().config.whatsapp.advisor_number,
            "🤖 Vicky SECOM activa. Este mensaje mantiene la ventana de notificaciones abierta.",
        )
        return jsonify({"ok": result.ok, "to": ctx().config.whatsapp.advisor_number}), 200

    @app.post("/ext/auto-send-one")
    def ext_auto_send_one() -> Any:
        token = _extract_auth_token()
        if not ctx().auth_guard.is_authorized(token):
            return jsonify({"ok": False, "error": "unauthorized"}), 401

        queue_item = ctx().state_store.dequeue()
        if queue_item is not None:
            if queue_item.template:
                result = send_template_message(queue_item.to, queue_item.template, queue_item.params)
                if result.ok:
                    ctx().state_store.set_state(queue_item.to, f"awaiting_info:{queue_item.template}")
                return jsonify(
                    {
                        "ok": True,
                        "sent": result.ok,
                        "source": "durable_queue",
                        "to": queue_item.to,
                        "message_id": result.message_id,
                        "queue_size": ctx().state_store.queue_size(),
                    }
                ), 200
            result = send_message(queue_item.to, queue_item.text)
            return jsonify(
                {
                    "ok": True,
                    "sent": result.ok,
                    "source": "durable_queue",
                    "to": queue_item.to,
                    "message_id": result.message_id,
                    "queue_size": ctx().state_store.queue_size(),
                }
            ), 200

        body = request.get_json(silent=True) or {}
        template_name = str(body.get("template", "")).strip()
        if not template_name:
            return jsonify({"ok": False, "error": "Falta 'template'"}), 400
        next_pending = ctx().google.pick_next_pending()
        if not next_pending:
            return jsonify({"ok": True, "sent": False, "reason": "no_pending"}), 200

        to = normalize_to_e164_mx(str(next_pending["whatsapp"]))
        nombre = str(next_pending["nombre"] or "").strip() or "Cliente"
        params = {} if template_name == "vrim_ideal" else {"nombre": nombre}
        result = send_template_message(to, template_name, params)
        if result.ok:
            ctx().state_store.set_state(to, f"awaiting_info:{template_name}")

        estatus = "FALLO_ENVIO"
        if result.ok and template_name == ctx().config.funnels.tpv_template_name:
            estatus = "ENVIADO_TPV"
        elif result.ok and template_name in set(ctx().config.funnels.alliance_templates):
            estatus = "ENVIADO_ALIANZA"
        elif result.ok:
            estatus = "ENVIADO_INICIAL"

        ctx().google.update_row_cells(
            int(next_pending["row_number"]),
            {"ESTATUS": estatus, "LAST_MESSAGE_AT": now_iso()},
        )
        return jsonify(
            {
                "ok": True,
                "sent": result.ok,
                "source": "sheet_pending",
                "to": to,
                "row": int(next_pending["row_number"]),
                "nombre": nombre,
                "message_id": result.message_id,
                "timestamp": now_iso(),
            }
        ), 200

    return app


if __name__ == "__main__":
    application = create_app()
    context = application.extensions["vicky_context"]
    if isinstance(context, AppContext):
        log.info("Contexto cargado: %s", context.to_json())
    application.run(host="0.0.0.0", port=context.config.port if isinstance(context, AppContext) else 5000, debug=False)
