from typing import Any, Dict, List

def extract_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    msgs: List[Dict[str, Any]] = []
    for entry in (payload or {}).get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for m in value.get("messages", []) or []:
                msgs.append(m)
    return msgs

def safe_text(msg: Dict[str, Any]) -> str:
    return ((msg.get("text") or {}).get("body") or "").strip()

def safe_from(msg: Dict[str, Any]) -> str:
    return (msg.get("from") or "").strip()
