# crm_client.py
"""
Клиент для отправки лидов в CRM.

Поддерживается:
- AmoCRM (api/v4, Bearer token)
- Bitrix24 (incoming webhook)
- HubSpot (Private App token, Bearer)

Все функции async, используют aiohttp. Возвращают (ok: bool, message: str).

ВАЖНО: access_token / api_key / webhook_url хранятся в settings (JSON) в
открытом виде. Для MVP это ок (локальная SQLite, не git). TODO: шифровать
через secure_config.encrypt перед сохранением.
"""

import logging
from typing import Tuple

import aiohttp

logger = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=15)


# ========================================================================
# ПРОВЕРКА ПОДКЛЮЧЕНИЯ
# ========================================================================

async def test_connection(crm: dict) -> Tuple[bool, str]:
    """Проверяет, что CRM принимает запросы с текущими ключами."""
    crm_type = (crm or {}).get("type")

    if crm_type == "amocrm":
        return await _test_amocrm(crm)
    if crm_type == "bitrix24":
        return await _test_bitrix24(crm)
    if crm_type == "hubspot":
        return await _test_hubspot(crm)
    return False, f"Неизвестная CRM: {crm_type or '—'}"


async def _test_amocrm(crm: dict) -> Tuple[bool, str]:
    subdomain = (crm.get("subdomain") or "").strip()
    token = (crm.get("access_token") or "").strip()
    if not subdomain or not token:
        return False, "Не указан subdomain или access_token"

    url = f"https://{subdomain}.amocrm.ru/api/v4/account"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(url, headers=headers) as r:
                if r.status == 200:
                    try:
                        data = await r.json()
                        name = data.get("name") or subdomain
                        return True, f"✅ AmoCRM: {name}"
                    except Exception:
                        return True, "✅ AmoCRM: подключено"
                if r.status == 401:
                    return False, "❌ 401: токен невалиден или истёк"
                if r.status == 403:
                    return False, "❌ 403: нет доступа (проверьте права интеграции)"
                if r.status == 404:
                    return False, "❌ 404: неверный subdomain"
                text = await r.text()
                return False, f"❌ HTTP {r.status}: {text[:150]}"
    except aiohttp.ClientError as e:
        return False, f"❌ Сеть: {e}"
    except Exception as e:
        return False, f"❌ {e}"


async def _test_bitrix24(crm: dict) -> Tuple[bool, str]:
    webhook = (crm.get("webhook_url") or "").strip()
    if not webhook:
        return False, "Не указан webhook_url"
    if not webhook.endswith("/"):
        webhook += "/"

    url = f"{webhook}profile.json"

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(url) as r:
                if r.status == 200:
                    try:
                        data = await r.json()
                    except Exception:
                        return False, "❌ Bitrix вернул не-JSON"
                    if data.get("error"):
                        desc = data.get("error_description") or data["error"]
                        return False, f"❌ Bitrix: {desc}"
                    name = data.get("result", {}).get("NAME") or "Bitrix24"
                    return True, f"✅ Bitrix24: {name}"
                if r.status == 404:
                    return False, "❌ 404: неверный webhook URL"
                text = await r.text()
                return False, f"❌ HTTP {r.status}: {text[:150]}"
    except aiohttp.ClientError as e:
        return False, f"❌ Сеть: {e}"
    except Exception as e:
        return False, f"❌ {e}"


async def _test_hubspot(crm: dict) -> Tuple[bool, str]:
    token = (crm.get("api_key") or "").strip()
    if not token:
        return False, "Не указан api_key"

    url = "https://api.hubapi.com/crm/v3/objects/contacts?limit=1"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(url, headers=headers) as r:
                if r.status == 200:
                    return True, "✅ HubSpot: подключено"
                if r.status == 401:
                    return False, "❌ 401: токен невалиден"
                if r.status == 403:
                    return False, "❌ 403: нет прав у Private App"
                text = await r.text()
                return False, f"❌ HTTP {r.status}: {text[:150]}"
    except aiohttp.ClientError as e:
        return False, f"❌ Сеть: {e}"
    except Exception as e:
        return False, f"❌ {e}"


# ========================================================================
# ОТПРАВКА ЛИДА
# ========================================================================

async def push_lead(crm: dict, lead: dict) -> Tuple[bool, str]:
    """
    Отправляет лид в CRM.

    lead = {
        "name": str | None,
        "problem": str | None,
        "budget": int | None,
        "deadline": int | None,
        "telegram_id": int,
        "username": str | None,
        "tags": list[str],
        "score": int,
    }
    """
    crm_type = (crm or {}).get("type")

    if crm_type == "amocrm":
        return await _push_amocrm(crm, lead)
    if crm_type == "bitrix24":
        return await _push_bitrix24(crm, lead)
    if crm_type == "hubspot":
        return await _push_hubspot(crm, lead)
    return False, f"Неизвестная CRM: {crm_type or '—'}"


def _lead_summary_text(lead: dict) -> str:
    return "\n".join([
        f"Проблема: {lead.get('problem') or 'не указана'}",
        f"Бюджет: {lead.get('budget') or 'не указан'}",
        f"Сроки: {lead.get('deadline') or 'не указаны'} дней",
        f"Telegram: @{lead.get('username') or '—'} (ID {lead.get('telegram_id')})",
        f"Теги: {', '.join(lead.get('tags') or []) or '—'}",
        f"Score: {lead.get('score', 0)}/100",
    ])


async def _push_amocrm(crm: dict, lead: dict) -> Tuple[bool, str]:
    subdomain = (crm.get("subdomain") or "").strip()
    token = (crm.get("access_token") or "").strip()
    if not subdomain or not token:
        return False, "AmoCRM: не указан subdomain/token"

    url = f"https://{subdomain}.amocrm.ru/api/v4/leads/complex"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    title = (
        f"AI-лид: {lead.get('name') or 'без имени'}"
        f" — {(lead.get('problem') or '')[:60]}"
    )

    body = [{
        "name": title,
        "price": int(lead.get("budget") or 0),
        "_embedded": {
            "contacts": [{
                "first_name": lead.get("name") or "Лид из Telegram",
            }],
        },
    }]

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.post(url, headers=headers, json=body) as r:
                if r.status in (200, 201):
                    try:
                        data = await r.json()
                        leads = data.get("_embedded", {}).get("leads", [])
                        if leads:
                            lead_id = leads[0].get("id")
                            return True, f"AmoCRM: сделка #{lead_id}"
                    except Exception:
                        pass
                    return True, "AmoCRM: сделка создана"
                text = await r.text()
                return False, f"AmoCRM HTTP {r.status}: {text[:200]}"
    except Exception as e:
        return False, f"AmoCRM: {e}"


async def _push_bitrix24(crm: dict, lead: dict) -> Tuple[bool, str]:
    webhook = (crm.get("webhook_url") or "").strip()
    if not webhook:
        return False, "Bitrix24: не указан webhook_url"
    if not webhook.endswith("/"):
        webhook += "/"

    url = f"{webhook}crm.lead.add.json"
    title = f"AI-лид: {lead.get('name') or 'без имени'}"

    fields = {
        "TITLE": title,
        "NAME": lead.get("name") or "Лид",
        "COMMENTS": _lead_summary_text(lead),
        "SOURCE_ID": "TELEGRAM",
    }

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.post(url, json={"fields": fields}) as r:
                if r.status == 200:
                    try:
                        data = await r.json()
                    except Exception:
                        return False, "Bitrix24: не-JSON ответ"
                    if data.get("error"):
                        desc = data.get("error_description") or data["error"]
                        return False, f"Bitrix24: {desc}"
                    result = data.get("result")
                    return True, f"Bitrix24: лид #{result}"
                text = await r.text()
                return False, f"Bitrix24 HTTP {r.status}: {text[:200]}"
    except Exception as e:
        return False, f"Bitrix24: {e}"


async def _push_hubspot(crm: dict, lead: dict) -> Tuple[bool, str]:
    token = (crm.get("api_key") or "").strip()
    if not token:
        return False, "HubSpot: не указан api_key"

    url = "https://api.hubapi.com/crm/v3/objects/contacts"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    properties = {
        "firstname": lead.get("name") or "Лид",
        "hs_lead_status": "NEW",
        "lifecyclestage": "lead",
        "hs_content_membership_notes": _lead_summary_text(lead),
    }

    body = {"properties": properties}

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.post(url, headers=headers, json=body) as r:
                if r.status in (200, 201):
                    try:
                        data = await r.json()
                        cid = data.get("id")
                        return True, f"HubSpot: контакт #{cid}"
                    except Exception:
                        return True, "HubSpot: контакт создан"
                if r.status == 409:
                    return False, "HubSpot: контакт уже существует (дубль)"
                text = await r.text()
                return False, f"HubSpot HTTP {r.status}: {text[:200]}"
    except Exception as e:
        return False, f"HubSpot: {e}"