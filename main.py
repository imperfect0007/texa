import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from sessions import SessionManager
from webhook_auth import verify_webhook_get, verify_webhook_signature

load_dotenv(".env.local")


app = FastAPI(title="Texa Apparel WhatsApp Bot")

# Session store (in-memory, 24h TTL).
session_manager = SessionManager(ttl_seconds=24 * 60 * 60)


def _get_env(name: str) -> str:
    """Fetch a required environment variable or raise a clear error."""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "").strip()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()
DESK_NOTIFY_WHATSAPP = os.getenv("DESK_NOTIFY_WHATSAPP", "").strip()
APP_SECRET = os.getenv("APP_SECRET", "").strip() or None

if VERIFY_TOKEN and ACCESS_TOKEN and PHONE_NUMBER_ID and DESK_NOTIFY_WHATSAPP:
    # Env is likely loaded and ready.
    pass
else:
    # Avoid crashing the server import for local linting; fail fast on use.
    pass


def _now_utc_iso() -> str:
    """Return current timestamp in ISO format (UTC)."""
    return datetime.now(timezone.utc).isoformat()


def _clean_user_text(text: str) -> str:
    """Normalize user text for matching keywords."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _is_cancel_command(text: str) -> bool:
    """Return True if user wants to cancel/stop the current flow."""
    t = _clean_user_text(text)
    return t in {"cancel", "stop", "exit", "quit"}


def _normalize_phone(user_input: str) -> Optional[str]:
    """
    Normalize a user-provided phone number to a canonical WhatsApp-style string.

    Accepts:
    - 10-digit Indian numbers (e.g., 9876543210)
    - 91 prefixed numbers (e.g., 919876543210)
    Returns a string like "+91XXXXXXXXXX" or None if invalid.
    """
    raw = (user_input or "").strip()
    raw = raw.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if raw.startswith("+"):
        raw = raw[1:]

    if not raw.isdigit():
        return None

    if len(raw) == 10 and raw[0] in {"6", "7", "8", "9"}:
        return f"+91{raw}"
    if len(raw) == 12 and raw.startswith("91") and raw[2] in {"6", "7", "8", "9"}:
        return f"+{raw}"

    return None


def _parse_int(text: str) -> Optional[int]:
    """Parse an integer quantity from user text."""
    if not text:
        return None
    t = text.strip().replace(",", "")
    if not re.fullmatch(r"\d+", t):
        return None
    return int(t)


# Button IDs (Meta "button_reply.id" payload)
BTN_ABOUT = "ABOUT"
BTN_ORDER = "ORDER"
BTN_MORE = "MORE"

BTN_ORDER_BULK = "ORDER_BULK"
BTN_ORDER_CUSTOM = "ORDER_CUSTOM"
BTN_ORDER_STOCK = "ORDER_STOCK"

BTN_MORE_CATALOGUE = "MORE_CATALOGUE"
BTN_MORE_CONTACT = "MORE_CONTACT"
BTN_MORE_CALLBACK = "MORE_CALLBACK"

BTN_CANCEL = "CANCEL"


MAX_BUTTONS = 3

SIZE_OPTIONS = ["XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL"]


async def _send_whatsapp_payload(payload: Dict[str, Any]) -> None:
    """Send a WhatsApp API request using the shared async httpx client."""
    if not ACCESS_TOKEN or not PHONE_NUMBER_ID:
        raise RuntimeError("Missing WhatsApp API env vars (ACCESS_TOKEN/PHONE_NUMBER_ID).")

    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}

    client = getattr(app.state, "http_client", None)
    if client is None:
        async with httpx.AsyncClient(timeout=20) as local_client:
            resp = await local_client.post(url, headers=headers, json=payload)
    else:
        resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code >= 400:
        raise RuntimeError(f"WhatsApp send failed: {resp.status_code} {resp.text}")


@app.on_event("startup")
async def on_startup() -> None:
    """Create a shared httpx client for outbound WhatsApp API calls."""
    app.state.http_client = httpx.AsyncClient(timeout=20)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Close the shared httpx client on app shutdown."""
    client = getattr(app.state, "http_client", None)
    if client:
        await client.aclose()


async def send_text(to_phone: str, text: str) -> None:
    """Send a plain text WhatsApp message to a user/admin phone."""
    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }
    await _send_whatsapp_payload(payload)


async def send_button_menu(to_phone: str, body_text: str, buttons: List[Tuple[str, str]]) -> None:
    """
    Send an interactive button message (max 3 buttons).

    buttons: list of (button_id, title).
    """
    if len(buttons) > MAX_BUTTONS:
        raise ValueError("Meta WhatsApp button message supports max 3 buttons.")

    meta_buttons = [
        {"type": "reply", "reply": {"id": button_id, "title": title}}
        for button_id, title in buttons
    ]

    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": meta_buttons},
        },
    }
    await _send_whatsapp_payload(payload)


async def send_home_menu(to_phone: str) -> None:
    """Send the HOME menu (About / Order / More)."""
    body = "Welcome to Texa Apparel 👕\nCrafting Corporate Apparel with Precision.\nChoose an option:"
    buttons = [(BTN_ABOUT, "About 👕"), (BTN_ORDER, "Order 📦"), (BTN_MORE, "More ➕")]
    await send_button_menu(to_phone, body, buttons)


async def send_about(to_phone: str) -> None:
    """Send business overview and stock/delivery capabilities."""
    text = (
        "Texa Apparel (Mysore, Karnataka) — Corporate Apparel, done fast.\n\n"
        "Production Capacity:\n"
        "- 62 Apparel Models\n"
        "- 8 Sizes\n"
        "- 30 Colours\n"
        "- 5,00,000+ Products Ready Stock\n"
        "- No Waiting Time (Instant Dispatch)\n\n"
        "Services:\n"
        "- Corporate Uniforms\n"
        "- Bulk T-shirt Manufacturing\n"
        "- Custom Branding\n"
        "- Event Apparel\n"
        "- Ready Stock Orders\n\n"
        "Delivery: Pan India Shipping."
    )
    await send_text(to_phone, text)


async def send_order_menu(to_phone: str) -> None:
    """Send ORDER menu (Bulk / Custom / Ready Stock)."""
    body = "Order options for Texa Apparel:"
    buttons = [
        (BTN_ORDER_BULK, "Bulk Order"),
        (BTN_ORDER_CUSTOM, "Custom Design"),
        (BTN_ORDER_STOCK, "Ready Stock"),
    ]
    await send_button_menu(to_phone, body, buttons)


async def send_more_menu(to_phone: str) -> None:
    """Send MORE menu (Catalogue / Contact / Callback)."""
    body = "More options:"
    buttons = [
        (BTN_MORE_CATALOGUE, "Catalogue"),
        (BTN_MORE_CONTACT, "Contact 📞"),
        (BTN_MORE_CALLBACK, "Callback"),
    ]
    await send_button_menu(to_phone, body, buttons)


async def send_pricing_info(to_phone: str) -> None:
    """Send general pricing guidance for quantity-based pricing."""
    text = (
        "Pricing depends on quantity.\n"
        "For the best rate, place a Bulk/Custom order and share your expected quantity.\n"
        "Reply: 'Order' or tap 'Order 📦' to continue."
    )
    await send_text(to_phone, text)


async def send_delivery_info(to_phone: str) -> None:
    """Send pan-India delivery information."""
    text = "Pan India shipping is available. Once you confirm quantity, size, colour, and location, we arrange instant dispatch."
    await send_text(to_phone, text)


async def send_contact_info(to_phone: str) -> None:
    """Send how users can contact the business (admin desk WhatsApp)."""
    text = (
        f"Contact Texa Apparel Desk (WhatsApp): {DESK_NOTIFY_WHATSAPP}\n"
        "Location: Mysore, Karnataka.\n"
        "For bulk/ready stock, tap 'Order 📦' to place quickly."
    )
    await send_text(to_phone, text)


async def send_help(to_phone: str) -> None:
    """Send keyword help / commands list."""
    text = (
        "Commands you can use:\n"
        "- hi / hello : show menu\n"
        "- order : show order options\n"
        "- bulk : bulk order info\n"
        "- custom : custom design info\n"
        "- stock : ready stock info\n"
        "- price : quantity-based pricing info\n"
        "- delivery : pan India delivery info\n"
        "- contact : desk contact\n"
        "- callback : request a callback\n"
        "- cancel / stop : interrupt the flow"
    )
    await send_text(to_phone, text)


async def persist_order(order: Dict[str, Any]) -> None:
    """Persist an order as a JSON line in data/orders.jsonl."""

    def _write_sync() -> None:
        path = os.path.join(os.path.dirname(__file__), "data", "orders.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(order, ensure_ascii=False) + "\n")

    await asyncio.to_thread(_write_sync)


def format_admin_order_notification(order: Dict[str, Any]) -> str:
    """Format the admin notification message text."""
    return (
        "New Texa Apparel Order\n\n"
        f"Name: {order.get('name','')}\n"
        f"Phone: {order.get('phone','')}\n"
        f"Order Type: {order.get('order_type','')}\n"
        f"Quantity: {order.get('quantity','')}\n"
        f"Size: {order.get('size','')}\n"
        f"Color: {order.get('color','')}\n"
        f"Location: {order.get('location','')}\n\n"
        f"Created At: {order.get('created_at','')}"
    )


def format_admin_callback_notification(payload: Dict[str, Any]) -> str:
    """Format the admin notification message text for callback requests."""
    return (
        "New Texa Apparel Callback Request\n\n"
        f"Name: {payload.get('name','')}\n"
        f"Phone: {payload.get('phone','')}\n"
        f"Created At: {payload.get('created_at','')}"
    )


async def notify_admin(text: str) -> None:
    """Send a message to the admin desk WhatsApp."""
    if not DESK_NOTIFY_WHATSAPP:
        raise RuntimeError("Missing DESK_NOTIFY_WHATSAPP env var.")
    await send_text(DESK_NOTIFY_WHATSAPP, text)


async def safe_background(coro: Any) -> None:
    """Run a coroutine in the background and log errors without crashing the webhook."""
    try:
        await coro
    except Exception as e:
        # Keep it simple: webhook must respond quickly.
        print(f"[background task error] {type(e).__name__}: {e}")


async def finalize_order(to_phone: str, session_phone: str, session_data: Dict[str, Any]) -> None:
    """Finalize an order: persist JSONL, notify admin (async), and confirm user."""
    order: Dict[str, Any] = {
        "created_at": _now_utc_iso(),
        "name": session_data.get("name"),
        "phone": session_data.get("phone"),
        "order_type": session_data.get("order_type"),
        "quantity": session_data.get("quantity"),
        "size": session_data.get("size"),
        "color": session_data.get("color"),
        "location": session_data.get("location"),
        "user_phone": session_phone,
    }

    # Persist in background to keep webhook responsive.
    asyncio.create_task(safe_background(persist_order(order)))
    # Notify admin in background as well.
    asyncio.create_task(safe_background(notify_admin(format_admin_order_notification(order))))

    await send_text(
        to_phone,
        "Thanks! Your Texa Apparel order request is received ✅\n"
        "Our desk will contact you shortly to confirm details and dispatch."
    )


async def finalize_callback(to_phone: str, session_phone: str, session_data: Dict[str, Any]) -> None:
    """Finalize callback request: notify admin (async) and confirm user."""
    payload: Dict[str, Any] = {
        "created_at": _now_utc_iso(),
        "name": session_data.get("name"),
        "phone": session_data.get("phone"),
        "user_phone": session_phone,
    }
    asyncio.create_task(safe_background(notify_admin(format_admin_callback_notification(payload))))
    await send_text(to_phone, "Thanks! Callback request received ✅\nOur desk will reach out soon.")


async def start_order_flow(from_phone: str, order_type: str) -> None:
    """Start/restart the order flow for a specific order type."""
    await session_manager.start_order_flow(from_phone, order_type=order_type)
    # Small intent-specific info (keeps the UX aligned with keyword expectations).
    blurb = {
        "Bulk Order": "Bulk orders (corporate uniforms / bulk t-shirts) with instant dispatch. Minimum quantity is greater than 10.",
        "Custom Design": "Custom branding is available (logos/prints/designs). We’ll confirm your details and dispatch quickly based on ready stock + production capacity.",
        "Ready Stock": "Ready stock orders: 5,00,000+ products are available for instant dispatch (no waiting time).",
    }.get(order_type, "")
    await send_text(
        from_phone,
        f"Great choice: {order_type}.\n{blurb}\n\nStep 1/6: What is your name?"
    )


async def start_callback_flow(from_phone: str) -> None:
    """Start/restart the callback flow."""
    await session_manager.start_callback_flow(from_phone)
    await send_text(from_phone, "Callback flow ✅\nStep 1/2: What is your name?")


async def handle_order_step(from_phone: str, text: str) -> None:
    """Handle a user's text input during an active order flow."""
    session = await session_manager.get(from_phone)
    if not session or session.flow != "order":
        return

    stage = session.stage
    t = (text or "").strip()

    if stage == "name":
        if not t:
            await send_text(from_phone, "Please share your name to continue.")
            return
        await session_manager.update_data(from_phone, name=t)
        await session_manager.advance_stage(from_phone, "phone")
        await send_text(from_phone, "Step 2/6: Share your phone number (10 digits).")
        return

    if stage == "phone":
        normalized = _normalize_phone(t)
        if not normalized:
            await send_text(from_phone, "That phone number doesn't look valid. Please enter a valid 10-digit number.")
            return
        await session_manager.update_data(from_phone, phone=normalized)
        await session_manager.advance_stage(from_phone, "quantity")
        await send_text(from_phone, "Step 3/6: Quantity needed? (Minimum 10)")
        return

    if stage == "quantity":
        qty = _parse_int(t)
        if qty is None:
            await send_text(from_phone, "Please enter quantity as numbers only (e.g., 50).")
            return
        if qty <= 10:
            await send_text(from_phone, "Quantity must be greater than 10. Please enter a higher quantity.")
            return
        await session_manager.update_data(from_phone, quantity=qty)
        await session_manager.advance_stage(from_phone, "size")
        sizes = ", ".join(SIZE_OPTIONS)
        await send_text(from_phone, f"Step 4/6: Preferred size? Options: {sizes}")
        return

    if stage == "size":
        if not t:
            await send_text(from_phone, "Please share a size (e.g., M / XL / 2XL).")
            return
        await session_manager.update_data(from_phone, size=t.upper())
        await session_manager.advance_stage(from_phone, "color")
        await send_text(from_phone, "Step 5/6: Preferred colour(s)? (e.g., Navy Blue / Red / Corporate mix)")
        return

    if stage == "color":
        if not t:
            await send_text(from_phone, "Please share your colour preference.")
            return
        await session_manager.update_data(from_phone, color=t)
        await session_manager.advance_stage(from_phone, "location")
        await send_text(from_phone, "Step 6/6: Delivery location (City, State, Pincode).")
        return

    if stage == "location":
        if not t:
            await send_text(from_phone, "Please share your delivery location.")
            return

        await session_manager.update_data(from_phone, location=t)
        # Read updated data before clearing session.
        session_latest = await session_manager.get(from_phone)
        data = session_latest.data if session_latest else session.data

        await session_manager.clear(from_phone)
        await finalize_order(from_phone, from_phone, data)
        return

    # Unknown stage: reset safely.
    await session_manager.clear(from_phone)
    await send_home_menu(from_phone)


async def handle_callback_step(from_phone: str, text: str) -> None:
    """Handle a user's text input during an active callback flow."""
    session = await session_manager.get(from_phone)
    if not session or session.flow != "callback":
        return

    stage = session.stage
    t = (text or "").strip()

    if stage == "name":
        if not t:
            await send_text(from_phone, "Please share your name to continue.")
            return
        await session_manager.update_data(from_phone, name=t)
        await session_manager.advance_stage(from_phone, "phone")
        await send_text(from_phone, "Step 2/2: Share your phone number (10 digits).")
        return

    if stage == "phone":
        normalized = _normalize_phone(t)
        if not normalized:
            await send_text(from_phone, "Please enter a valid 10-digit phone number for the callback.")
            return
        await session_manager.update_data(from_phone, phone=normalized)
        session_latest = await session_manager.get(from_phone)
        data = session_latest.data if session_latest else session.data
        await session_manager.clear(from_phone)
        await finalize_callback(from_phone, from_phone, data)
        return

    await session_manager.clear(from_phone)
    await send_home_menu(from_phone)


async def route_keyword(from_phone: str, text: str) -> None:
    """Route free-text commands when no active flow is set."""
    t = _clean_user_text(text)

    if t in {"hi", "hello"}:
        await session_manager.clear(from_phone)
        await send_home_menu(from_phone)
        return

    if t == "order":
        await send_order_menu(from_phone)
        return

    if t == "about":
        await send_about(from_phone)
        return

    if t == "more":
        await send_more_menu(from_phone)
        return

    if t == "bulk":
        await start_order_flow(from_phone, "Bulk Order")
        return

    if t == "custom":
        await start_order_flow(from_phone, "Custom Design")
        return

    if t in {"stock", "ready", "ready stock"}:
        await start_order_flow(from_phone, "Ready Stock")
        return

    if t == "price":
        await send_pricing_info(from_phone)
        return

    if t == "delivery":
        await send_delivery_info(from_phone)
        return

    if t == "contact":
        await send_contact_info(from_phone)
        return

    if t == "callback":
        await start_callback_flow(from_phone)
        return

    if t == "help":
        await send_help(from_phone)
        return

    await send_text(
        from_phone,
        "I can help with Texa Apparel orders and stock.\n"
        "Try: `hi` (menu) or `order` (start order). Type `help` to see commands."
    )
    # Keep the user oriented.
    await send_home_menu(from_phone)


async def handle_incoming_text(from_phone: str, text: str) -> None:
    """Handle incoming free-text messages from WhatsApp."""
    if _is_cancel_command(text):
        await session_manager.clear(from_phone)
        await send_home_menu(from_phone)
        return

    session = await session_manager.get(from_phone)
    if session:
        if session.flow == "order":
            await handle_order_step(from_phone, text)
            return
        if session.flow == "callback":
            await handle_callback_step(from_phone, text)
            return

    # No active session -> route by keyword.
    await route_keyword(from_phone, text)


async def handle_button(from_phone: str, button_id: str) -> None:
    """Handle Meta interactive button clicks (interrupts any flow)."""
    # Button click interrupts flow by design.
    await session_manager.clear(from_phone)

    if button_id == BTN_CANCEL:
        await send_home_menu(from_phone)
        return

    if button_id == BTN_ABOUT:
        await send_about(from_phone)
        return

    if button_id == BTN_ORDER:
        await send_order_menu(from_phone)
        return

    if button_id == BTN_MORE:
        await send_more_menu(from_phone)
        return

    if button_id == BTN_ORDER_BULK:
        await start_order_flow(from_phone, "Bulk Order")
        return

    if button_id == BTN_ORDER_CUSTOM:
        await start_order_flow(from_phone, "Custom Design")
        return

    if button_id == BTN_ORDER_STOCK:
        await start_order_flow(from_phone, "Ready Stock")
        return

    if button_id == BTN_MORE_CATALOGUE:
        await send_text(from_phone, "Catalogue on request ✅\nTell us your requirement (T-shirts / Uniforms / Event), and we will share the latest catalogue details.")
        return

    if button_id == BTN_MORE_CONTACT:
        await send_contact_info(from_phone)
        return

    if button_id == BTN_MORE_CALLBACK:
        await start_callback_flow(from_phone)
        return

    # Unknown button id: recover to menu.
    await send_home_menu(from_phone)


@app.get("/")
async def root() -> PlainTextResponse:
    """Simple service health response."""
    return PlainTextResponse("Texa Apparel WhatsApp Bot is running.")


@app.get("/ping")
async def ping() -> PlainTextResponse:
    """Keep-alive endpoint for Render health checks."""
    return PlainTextResponse("pong")


@app.get("/webhook")
async def verify_webhook(request: Request) -> PlainTextResponse:
    """Handle Meta webhook verification (GET challenge-response)."""
    if not VERIFY_TOKEN:
        raise HTTPException(status_code=500, detail="VERIFY_TOKEN not configured.")

    q = dict(request.query_params)
    if verify_webhook_get(q, expected_verify_token=VERIFY_TOKEN):
        # Meta expects the raw hub.challenge string in the response body.
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Verification token mismatch.")


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    """Handle Meta WhatsApp webhook events (messages from users)."""
    raw = await request.body()
    sig = request.headers.get("X-Hub-Signature-256")
    if not verify_webhook_signature(raw, sig, app_secret=APP_SECRET):
        raise HTTPException(status_code=403, detail="Invalid webhook signature.")

    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    tasks: List[asyncio.Task] = []

    # Meta webhook payload structure: entry -> changes -> value -> messages
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", []) or []
            for msg in messages:
                from_phone = msg.get("from")
                if not from_phone:
                    continue

                msg_type = msg.get("type")

                if msg_type == "text":
                    text = (msg.get("text") or {}).get("body") or ""
                    tasks.append(asyncio.create_task(handle_incoming_text(from_phone, text)))

                elif msg_type == "interactive":
                    interactive = msg.get("interactive") or {}
                    button_reply = interactive.get("button_reply") or {}
                    button_id = button_reply.get("id")
                    if button_id:
                        tasks.append(asyncio.create_task(handle_button(from_phone, button_id)))

                else:
                    # Ignore unsupported message types.
                    pass

    if tasks:
        # Await tasks so exceptions surface in logs (but still quickly).
        await asyncio.gather(*tasks, return_exceptions=True)

    return JSONResponse({"status": "ok"})

