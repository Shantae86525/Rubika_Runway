"""
Standalone TEST for Feature #2 (channel report) — run this on the server where
the account sessions live (data/sessions/acc_<phone>).

Goal: confirm which fields rubpy 7.3.5 actually returns for
  * channel member count  -> from get_channel_info(channel_guid)
  * last post view count   -> from get_messages(channel_guid, "0", limit)

This does NOT touch the bot/db. It only reads. It prints the RAW response
dicts so we can lock the exact field names before wiring the feature in.

Usage:
    python scripts/test_channel_report.py <phone> <channel>

    <phone>   : the account phone whose session will be used (e.g. 09123456789)
    <channel> : a channel GUID (starts with 'c0...'), OR a public @username,
                OR a full channel link (https://rubika.ir/<username>)

Examples:
    python scripts/test_channel_report.py 09123456789 c0ABCDEF...
    python scripts/test_channel_report.py 09123456789 @my_channel
    python scripts/test_channel_report.py 09123456789 https://rubika.ir/my_channel
"""
import asyncio
import json
import os
import sys

# allow running from repo root OR from scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rubika_client as rb  # noqa: E402


def dump(label, obj):
    """Pretty-print whatever rubpy returned, plus its raw dict form."""
    print("\n" + "=" * 60)
    print(label)
    print("=" * 60)
    data = rb._data_of(obj)
    if data:
        try:
            print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        except Exception:
            print(repr(data))
    else:
        print("(no dict form) repr:", repr(obj)[:2000])
    return data


async def _try_call(fn, attempts):
    """Call fn trying several arg shapes (mirrors rubika_client style)."""
    last = None
    for make in attempts:
        a, k = make()
        try:
            return await fn(*a, **k)
        except TypeError as e:
            last = e
            continue
    raise RuntimeError(f"signature mismatch: {last}")


async def resolve_channel_guid(client, channel: str) -> str:
    """Turn a guid / @username / link into a channel guid (best-effort)."""
    channel = channel.strip()

    # 1) already a guid?
    if channel.startswith("c0") or channel.startswith("c"):
        # crude: real channel guids start with 'c0'
        if channel.startswith("c0"):
            return channel

    # 2) extract a username from @name or a link
    username = None
    if channel.startswith("@"):
        username = channel[1:]
    elif channel.startswith("http"):
        username = channel.rstrip("/").split("/")[-1].lstrip("@")
    else:
        username = channel.lstrip("@")

    # try the common rubpy resolver names, tolerant of version diffs
    for name in ("get_object_by_username", "get_info_by_username",
                 "get_channel_info_by_username"):
        fn = getattr(client, name, None)
        if fn is None:
            continue
        try:
            res = await _try_call(fn, [
                lambda u=username: ((u,), {}),
                lambda u=username: ((), {"username": u}),
            ])
        except Exception as e:
            print(f"[resolve] {name} failed: {e!r}")
            continue
        dump(f"resolve via {name}('{username}')", res)
        guid = rb._guid_of(res) or rb._channel_guid_of(res)
        if guid:
            print(f"[resolve] -> guid = {guid}")
            return guid

    raise RuntimeError(
        "could not resolve channel to a guid. Re-run passing the channel "
        "GUID directly (starts with 'c0...').")


def extract_members(info_data: dict):
    """Look for a member-count field under any of the common names."""
    candidates = ("count_members", "member_count", "members_count",
                  "count_member", "subscriber_count")
    # member count is sometimes nested under "channel"
    nests = [info_data, info_data.get("channel") or {},
             info_data.get("abs_object") or {}]
    for d in nests:
        if isinstance(d, dict):
            for key in candidates:
                if d.get(key) is not None:
                    return key, d.get(key)
    return None, None


def extract_views(msg: dict):
    """Look for a view-count field on a message under common names."""
    candidates = ("views", "view_count", "count_views", "seen_count")
    for key in candidates:
        if msg.get(key) is not None:
            return key, msg.get(key)
    return None, None


async def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    phone = sys.argv[1]
    channel = sys.argv[2]

    client = rb.open_client(phone)
    try:
        await rb.connect_ready(client)
        print(f"[ok] connected with session for {phone}")

        guid = await resolve_channel_guid(client, channel)

        # ---- get_channel_info(channel_guid) -------------------------------
        info_fn = getattr(client, "get_channel_info", None)
        if info_fn is None:
            print("!! this rubpy build has NO get_channel_info()")
        else:
            info = await _try_call(info_fn, [
                lambda: ((guid,), {}),
                lambda: ((), {"channel_guid": guid}),
                lambda: ((), {"object_guid": guid}),
            ])
            info_data = dump("get_channel_info(channel_guid)", info)
            key, val = extract_members(info_data)
            if key:
                print(f"\n>>> MEMBER COUNT found: {key} = {val}")
            else:
                print("\n>>> MEMBER COUNT field NOT recognized — "
                      "look at the raw dump above and tell me the field name.")

        # ---- get_messages(channel_guid, "0", limit) -> last post views ----
        msgs = await client.get_messages(guid, "0", "5")
        messages = getattr(msgs, "messages", None)
        if messages is None and isinstance(msgs, dict):
            messages = msgs.get("messages", [])
        if not messages:
            print("\n>>> no messages returned for this channel.")
        else:
            print(f"\n[ok] got {len(messages)} recent messages; inspecting them:")
            for i, m in enumerate(messages):
                md = rb._data_of(m)
                mid = rb._get(m, "message_id", "id")
                key, val = extract_views(md)
                txt = (rb._msg_text_of(m) or "")[:40].replace("\n", " ")
                line = f"  [{i}] id={mid} views={val if key else '??'} ({key or 'unknown'}) text='{txt}'"
                print(line)
            # full dump of the newest message so we can see the view field
            dump("NEWEST message (raw)", messages[0])
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
