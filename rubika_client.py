"""
Rubika integration layer (wraps the `rubpy` library, v7.x).
===========================================================

Scope of THIS project (on purpose):
  * logs into the USER'S OWN account (phone + code + optional 2FA),
  * reads the account's own contacts (paginated),
  * finds a message the user marked in their OWN Saved Messages,
  * FORWARDS that message to a list of the user's own contacts.

There is intentionally NO proxy support, NO multi-account orchestration and
NO batching/anti-rate-limit machinery here. This is a small personal tool.

All rubpy-specific calls live in this file. rubpy is unofficial, so method
names / response shapes can differ between versions; the helpers below are
written defensively for that reason.
"""
import asyncio
import inspect
import os

from rubpy import Client
from rubpy.crypto import Crypto

SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "data", "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)


def session_path(phone: str) -> str:
    safe = phone.replace("+", "").replace(" ", "")
    return os.path.join(SESSIONS_DIR, f"acc_{safe}")


def normalize_phone(phone: str) -> str:
    """Rubika expects digits with country code, no '+' and no leading 0.
    '+989121234567' -> '989121234567', '09121234567' -> '989121234567'
    """
    p = "".join(ch for ch in phone if ch.isdigit())
    if p.startswith("0"):
        p = "98" + p[1:]
    return p


def _make_client(name: str) -> Client:
    return Client(name=name)


def open_client(phone: str) -> Client:
    """Return a rubpy client bound to the account's SAVED session."""
    return _make_client(session_path(phone))


# --------------------------------------------------------------------------- #
# Connect + rebuild the signing material that rubpy's connect() can omit.
# --------------------------------------------------------------------------- #
async def connect_ready(client: Client):
    await client.connect()
    auth = getattr(client, "auth", None)
    private_key = getattr(client, "private_key", None)
    try:
        if auth is not None and getattr(client, "key", None) in (None, ""):
            client.key = Crypto.passphrase(auth)
    except Exception:
        pass
    try:
        if auth is not None:
            client.decode_auth = Crypto.decode_auth(auth)
    except Exception:
        pass
    try:
        if private_key is not None and getattr(client, "import_key", None) is None:
            ik = _import_key_from_private(private_key)
            if ik is not None:
                client.import_key = ik
    except Exception:
        pass
    return client


# --------------------------------------------------------------------------- #
# Programmatic login (mirrors rubpy's own start.py flow)
# --------------------------------------------------------------------------- #
def _get(obj, *names):
    for n in names:
        v = getattr(obj, n, None)
        if v not in (None, ""):
            return v
        if isinstance(obj, dict) and obj.get(n) not in (None, ""):
            return obj.get(n)
    return None


def _import_key_from_private(private_key):
    """Build the signing key exactly like rubpy start.py does."""
    try:
        from Crypto.PublicKey import RSA
        from Crypto.Signature import pkcs1_15
        if private_key is not None:
            return pkcs1_15.new(RSA.import_key(private_key.encode()))
    except Exception:
        pass
    return None


async def start_login(phone: str, pass_key: str = None) -> dict:
    """Phase 1: connect + request the login code (handles 2FA pass_key)."""
    phone = normalize_phone(phone)
    client = _make_client(session_path(phone))
    await client.connect()

    public_key, private_key = Crypto.create_keys()

    if pass_key:
        result = await client.send_code(phone_number=phone, pass_key=pass_key)
    else:
        result = await client.send_code(phone_number=phone)

    return {
        "client": client,
        "phone": phone,
        "status": _get(result, "status") or "",
        "phone_code_hash": _get(result, "phone_code_hash"),
        "hint": _get(result, "hint_pass_key"),
        "public_key": public_key,
        "private_key": private_key,
    }


async def finish_login(ctx: dict, code: str):
    """Phase 2: sign in with the code, then replicate rubpy start.py steps."""
    client: Client = ctx["client"]
    phone = ctx["phone"]
    private_key = ctx["private_key"]

    result = await client.sign_in(
        phone_code=code,
        phone_number=phone,
        phone_code_hash=ctx["phone_code_hash"],
        public_key=ctx["public_key"],
    )

    status = _get(result, "status") or ""
    if str(status).upper() not in ("OK", ""):
        raise RuntimeError(f"sign_in status: {status}")

    enc_auth = _get(result, "auth")
    decrypted = Crypto.decrypt_RSA_OAEP(private_key, enc_auth)

    client.private_key = private_key
    client.key = Crypto.passphrase(decrypted)
    client.auth = decrypted
    try:
        client.decode_auth = Crypto.decode_auth(client.auth)
    except Exception:
        pass
    ik = _import_key_from_private(private_key)
    if ik is not None:
        client.import_key = ik

    try:
        user = _get(result, "user")
        guid = _guid_of(user) or _guid_of(result)
        phone_number = _get(user, "phone") or phone
        user_agent = getattr(client, "user_agent", None)
        client.session.insert(
            auth=client.auth,
            guid=guid,
            user_agent=user_agent,
            phone_number=phone_number,
            private_key=private_key,
        )
    except Exception:
        pass

    try:
        await client.register_device(device_model=getattr(client, "name", "RubikaBot"))
    except Exception:
        try:
            await client.register_device()
        except Exception:
            pass

    return result


# --------------------------------------------------------------------------- #
# Tolerant field extractors (shapes vary across rubpy versions)
# --------------------------------------------------------------------------- #
def _data_of(obj):
    for attr in ("original_update", "to_dict"):
        v = getattr(obj, attr, None)
        if isinstance(v, dict):
            return v
    if isinstance(obj, dict):
        return obj
    return {}


def _guid_of(obj):
    if obj is None:
        return None
    d = _data_of(obj)
    for key in ("object_guid", "user_guid", "guid"):
        if d.get(key):
            return d[key]
    for attr in ("object_guid", "user_guid", "guid"):
        v = getattr(obj, attr, None)
        if v:
            return v
    user = getattr(obj, "user", None)
    if user is not None and user is not obj:
        return _guid_of(user)
    if isinstance(d.get("user"), dict):
        u = d["user"]
        for key in ("object_guid", "user_guid", "guid"):
            if u.get(key):
                return u[key]
    return None


def _name_of(obj, default="-"):
    d = _data_of(obj)
    first = d.get("first_name") or ""
    last = d.get("last_name") or ""
    name = (str(first) + " " + str(last)).strip()
    if name:
        return name
    for key in ("name", "title", "first_name"):
        if d.get(key):
            return d[key]
    for attr in ("first_name", "name", "title"):
        v = getattr(obj, attr, None)
        if v:
            return v
    return default


def _type_of(obj):
    d = _data_of(obj)
    t = d.get("type")
    if not t and isinstance(d.get("abs_object"), dict):
        t = d["abs_object"].get("type")
    if not t:
        abs_obj = getattr(obj, "abs_object", None) or obj
        t = getattr(abs_obj, "type", None)
        if t is None and isinstance(abs_obj, dict):
            t = abs_obj.get("type")
    return (t or "").lower()


def _last_online_of(u):
    d = _data_of(u)
    v = d.get("last_online")
    if v is None:
        ot = d.get("online_time")
        if isinstance(ot, dict):
            v = ot.get("exact_time")
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _is_online(u):
    d = _data_of(u)
    status = (d.get("status") or "").lower()
    return status == "online"


# --------------------------------------------------------------------------- #
# Contacts (paginated; Rubika returns ~100 per page)
# --------------------------------------------------------------------------- #
def _next_start_id(result):
    return _get(result, "next_start_id") or _get(result, "next_start_index")


async def get_contacts_full(client: Client) -> list:
    """Return ALL contacts as dicts {guid, name, last_online, online}, paginated."""
    out = []
    seen = set()
    start_id = None
    for _ in range(200):  # safety cap (200 * ~100 = 20k)
        result = await client.get_contacts(start_id) if start_id else await client.get_contacts()
        users = getattr(result, "users", None)
        if users is None and isinstance(result, dict):
            users = result.get("users", [])
        for u in users or []:
            guid = _guid_of(u)
            if guid and guid not in seen:
                seen.add(guid)
                out.append({
                    "guid": guid,
                    "name": _name_of(u),
                    "last_online": _last_online_of(u),
                    "online": _is_online(u),
                })
        start_id = _next_start_id(result)
        if not start_id or not users:
            break
    return out


async def get_chats_user_guids(client: Client):
    """Return an ORDERED list of guids of USER chats (most recent activity first)
    and the total number of groups the account is in.
    """
    user_chats = []
    seen_u = set()
    n_groups = 0
    seen_g = set()
    start_id = None
    for _ in range(200):
        result = await client.get_chats(start_id) if start_id else await client.get_chats()
        chats = getattr(result, "chats", None)
        if chats is None and isinstance(result, dict):
            chats = result.get("chats", [])
        for chat in chats or []:
            ctype = _type_of(chat)
            guid = _guid_of(chat)
            if not guid:
                continue
            if ctype == "user" and guid not in seen_u:
                seen_u.add(guid)
                user_chats.append(guid)
            elif ctype == "group" and guid not in seen_g:
                seen_g.add(guid)
                n_groups += 1
        start_id = _next_start_id(result)
        if not start_id or not chats:
            break
    return user_chats, n_groups


async def get_ordered_recipients(client: Client):
    """Build the recipient list for the account's OWN CONTACTS only.

    Order requested by the user:
      1) contacts we already have a chat with (most recent first)
      2) then contacts that are currently online
      3) then the rest, by last-seen (most recent first)

    Returns (ordered: list of {guid, name}, stats: dict).
    """
    contacts = await get_contacts_full(client)
    user_chats, n_groups = await get_chats_user_guids(client)

    by_guid = {c["guid"]: c for c in contacts if c["guid"]}

    # 1) contacts with a chat, in recent-activity order
    with_chat = [g for g in user_chats if g in by_guid]
    with_chat_set = set(with_chat)

    rest = [g for g in by_guid if g not in with_chat_set]
    # 2) online first, 3) then by last_online desc
    rest.sort(key=lambda g: (1 if by_guid[g]["online"] else 0, by_guid[g]["last_online"]),
              reverse=True)

    ordered_guids = with_chat + rest
    ordered = [{"guid": g, "name": by_guid[g]["name"]} for g in ordered_guids]

    stats = {
        "contacts": len(contacts),
        "groups": n_groups,
        "with_chat": len(with_chat),
    }
    return ordered, stats


# --------------------------------------------------------------------------- #
# Find a marked message in the account's OWN Saved Messages.
# --------------------------------------------------------------------------- #
def _msg_id_of(msg):
    return _get(msg, "message_id", "id")


def _msg_text_of(msg):
    return _get(msg, "text", "caption") or ""


async def get_self_guid(client: Client) -> str:
    me = await client.get_me()
    guid = _guid_of(me)
    if not guid:
        raise RuntimeError("could not resolve self guid")
    return guid


async def find_marked_message(client: Client, marker: str):
    """Search Saved Messages for a message whose text/caption contains `marker`.
    Returns (saved_guid, message_id) or (saved_guid, None).
    """
    saved_guid = await get_self_guid(client)
    max_id = None
    for _ in range(50):  # up to ~50 pages of recent saved messages
        try:
            if max_id:
                result = await client.get_messages(saved_guid, max_id, "20")
            else:
                result = await client.get_messages(saved_guid, "0", "20")
        except Exception:
            break
        messages = getattr(result, "messages", None)
        if messages is None and isinstance(result, dict):
            messages = result.get("messages", [])
        if not messages:
            break
        for msg in messages:
            if marker in _msg_text_of(msg):
                return saved_guid, _msg_id_of(msg)
        last = messages[-1]
        max_id = _msg_id_of(last)
        if not max_id:
            break
    return saved_guid, None


# --------------------------------------------------------------------------- #
# Forwarding (version-tolerant): forward the marked message to one recipient.
# Forwarding reuses media already uploaded from the user's phone, so the bot
# never needs to upload anything itself.
# --------------------------------------------------------------------------- #
async def forward_message(client: Client, from_guid: str, to_guid: str, message_id):
    """Forward one message, adapting to whatever signature this rubpy build uses."""
    fn = getattr(client, "forward_messages", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no forward_messages()")

    mids = [message_id]
    try:
        params = [p for p in inspect.signature(fn).parameters.keys() if p != "self"]
    except (TypeError, ValueError):
        params = []

    if params:
        kwargs = {}
        for p in params:
            lp = p.lower()
            if "from" in lp and "guid" in lp:
                kwargs[p] = from_guid
            elif "to" in lp and "guid" in lp:
                kwargs[p] = to_guid
            elif lp in ("object_guid", "from_object_guid"):
                kwargs[p] = from_guid
            elif "message_ids" in lp or lp in ("messages", "message_ids"):
                kwargs[p] = mids
            elif "message_id" in lp:
                kwargs[p] = message_id
        # Only use kwargs if we matched the from/to/message params sensibly.
        if kwargs.get(_first_match(params, "from"), None) is not None:
            try:
                return await fn(**kwargs)
            except TypeError:
                pass

    # Fallbacks: try the two most common positional orders.
    try:
        return await fn(from_guid, to_guid, mids)
    except TypeError:
        return await fn(from_guid, mids, to_guid)


def _first_match(params, needle):
    for p in params:
        if needle in p.lower():
            return p
    return None


# --------------------------------------------------------------------------- #
# Channels (version-tolerant, like forward_message above).
# rubpy is unofficial, so method names / signatures differ between versions;
# we try the most common shapes and fail with a clear message otherwise.
# --------------------------------------------------------------------------- #
def _channel_guid_of(obj):
    """Pull a channel guid out of whatever shape create_channel returned."""
    d = _data_of(obj)
    for key in ("channel_guid", "object_guid", "guid"):
        if d.get(key):
            return d[key]
    # sometimes nested under "channel"
    ch = d.get("channel")
    if isinstance(ch, dict):
        for key in ("channel_guid", "object_guid", "guid"):
            if ch.get(key):
                return ch[key]
    for attr in ("channel_guid", "object_guid", "guid"):
        v = getattr(obj, attr, None)
        if v:
            return v
    ch = getattr(obj, "channel", None)
    if ch is not None and ch is not obj:
        return _channel_guid_of(ch)
    return None


async def _try_call(fn, attempts):
    """Call `fn` trying several arg shapes; return first non-TypeError result."""
    last_err = None
    for make_args in attempts:
        args, kwargs = make_args()
        try:
            return await fn(*args, **kwargs)
        except TypeError as e:  # signature mismatch -> try the next shape
            last_err = e
            continue
    raise RuntimeError(f"signature mismatch: {last_err}")


async def create_channel(client: Client, title: str, description: str = None) -> str:
    """Create a channel and return its guid. Tolerant of rubpy version diffs.

    IMPORTANT: never pass an empty-string description — Rubika's addChannel
    rejects it with INVALID_INPUT. Omit it (None) when there is no description.
    Verified against rubpy 7.3.5 where the method is `add_channel(title, ...)`.
    """
    fn = getattr(client, "add_channel", None) or getattr(client, "create_channel", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no add_channel()/create_channel()")
    desc = description or None  # turn "" into None
    if desc:
        attempts = [
            lambda: ((), {"title": title, "description": desc}),
            lambda: ((title, desc), {}),
            lambda: ((), {"title": title}),
        ]
    else:
        attempts = [
            lambda: ((), {"title": title}),
            lambda: ((title,), {}),
        ]
    result = await _try_call(fn, attempts)
    guid = _channel_guid_of(result)
    if not guid:
        raise RuntimeError("channel created but its guid was not found in the response")
    return guid


async def add_channel_members(client: Client, channel_guid: str, member_guids: list):
    """Add a batch of member guids to a channel. Tolerant of rubpy version diffs."""
    if not member_guids:
        return None
    fn = (getattr(client, "add_channel_members", None)
          or getattr(client, "add_channel_member", None))
    if fn is None:
        raise RuntimeError("this rubpy build has no add_channel_members()")
    return await _try_call(fn, [
        lambda: ((channel_guid, member_guids), {}),
        lambda: ((), {"channel_guid": channel_guid, "member_guids": member_guids}),
        lambda: ((), {"object_guid": channel_guid, "member_guids": member_guids}),
        lambda: ((), {"channel_guid": channel_guid, "user_ids": member_guids}),
    ])


async def seed_channel_with_contacts(client: Client, channel_guid: str,
                                     target: int = 300, batch: int = 80,
                                     delay: float = 2.0) -> int:
    """Add the account's OWN contacts to `channel_guid`, in chunks of `batch`,
    until `target` is reached. Returns how many members were added.

    Contacts are read with get_contacts_full() which already paginates Rubika's
    ~100-per-page contact list, so we transparently walk past the 100 limit.
    """
    contacts = await get_contacts_full(client)            # paginated read
    guids = [c["guid"] for c in contacts if c.get("guid")][:max(0, int(target))]
    added = 0
    for i in range(0, len(guids), max(1, int(batch))):
        chunk = guids[i:i + batch]
        try:
            await add_channel_members(client, channel_guid, chunk)
            added += len(chunk)
        except Exception:
            # best-effort: skip a failed batch and keep going to the next one
            pass
        if i + batch < len(guids):
            await asyncio.sleep(delay)
    return added


# --------------------------------------------------------------------------- #
# Plain text send + group listing (for the Automation feature).
# Verified against rubpy 7.3.5: send_message(object_guid, text=...).
# --------------------------------------------------------------------------- #
async def send_text(client: Client, object_guid: str, text: str):
    """Send a plain text message to a chat/group. Tolerant of rubpy diffs."""
    fn = getattr(client, "send_message", None)
    if fn is None:
        raise RuntimeError("this rubpy build has no send_message()")
    return await _try_call(fn, [
        lambda: ((object_guid, text), {}),
        lambda: ((), {"object_guid": object_guid, "text": text}),
    ])


async def get_group_guids(client: Client) -> list:
    """Return ALL groups the account is in as {guid, name}, paginated."""
    out = []
    seen = set()
    start_id = None
    for _ in range(200):  # safety cap
        result = await client.get_chats(start_id) if start_id else await client.get_chats()
        chats = getattr(result, "chats", None)
        if chats is None and isinstance(result, dict):
            chats = result.get("chats", [])
        for ch in chats or []:
            if _type_of(ch) == "group":
                g = _guid_of(ch)
                if g and g not in seen:
                    seen.add(g)
                    out.append({"guid": g, "name": _name_of(ch)})
        start_id = _next_start_id(result)
        if not start_id or not chats:
            break
    return out
