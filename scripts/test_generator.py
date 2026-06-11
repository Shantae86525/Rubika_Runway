"""
Standalone TEST for the future "generator engine" (موتور مولد).
================================================================

Run this on the server where the account sessions live. It does NOT build the
engine and it does NOT spam anything. Its only job is to DISCOVER which rubpy
7.3.5 methods exist (and their argument shapes) for the steps the generator
will need, so we lock the exact names BEFORE writing the engine:

  1. create a channel              (already used elsewhere: add_channel)
  2. another account joins it      (join by guid? by link?)
  3. make a member an ADMIN        (the risky unknown one)
  4. add members from contacts     (already used: add_channel_members)

By default it is READ-ONLY: it just lists the candidate method names that this
rubpy build actually exposes, so we can see what's available. Creating a real
test channel only happens if you pass --create.

Usage:
    # safe, read-only: just show which methods exist
    python scripts/test_generator.py <phone>

    # also create a throwaway channel to test admin/join wiring (optional)
    python scripts/test_generator.py <phone> --create
"""
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rubika_client as rb  # noqa: E402


# method-name candidates we care about, grouped by the engine step
CANDIDATES = {
    "create_channel": ["add_channel", "create_channel"],
    "join_channel": ["join_channel", "join_channel_action", "join_group",
                     "join_chat", "join_channel_by_link", "add_channel_member_self"],
    "make_admin": ["set_channel_admin", "add_channel_admin", "set_member_access",
                   "set_channel_member_access", "set_group_admin",
                   "set_chat_admin", "update_channel_admin", "channel_set_admin"],
    "add_members": ["add_channel_members", "add_channel_member", "add_member"],
    "get_members": ["get_channel_all_members", "get_channel_members",
                    "get_channel_member"],
    "get_admin_access_list": ["get_channel_admin_access_list",
                              "get_channel_admin_members", "get_admin_access_list"],
}


def show_methods(client):
    print("\n" + "=" * 60)
    print("METHOD DISCOVERY (which candidates this rubpy build exposes)")
    print("=" * 60)
    for step, names in CANDIDATES.items():
        print(f"\n[{step}]")
        found_any = False
        for name in names:
            fn = getattr(client, name, None)
            if fn is None:
                continue
            found_any = True
            try:
                params = [p for p in inspect.signature(fn).parameters
                          if p != "self"]
                sig = "(" + ", ".join(params) + ")"
            except (TypeError, ValueError):
                sig = "(signature unavailable)"
            print(f"   ✅ {name}{sig}")
        if not found_any:
            print("   ❌ NONE of the candidates exist — need to find the real name.")


def dump_all_admin_like(client):
    """List every client method whose name hints at admin/access, so if our
    candidate list missed the real name, we still spot it."""
    print("\n" + "=" * 60)
    print("ALL methods containing 'admin' / 'access' / 'member' / 'join'")
    print("=" * 60)
    hits = []
    for name in dir(client):
        if name.startswith("_"):
            continue
        low = name.lower()
        if any(k in low for k in ("admin", "access", "member", "join")):
            hits.append(name)
    for name in sorted(hits):
        fn = getattr(client, name, None)
        try:
            params = [p for p in inspect.signature(fn).parameters if p != "self"]
            sig = "(" + ", ".join(params) + ")"
        except (TypeError, ValueError):
            sig = ""
        print(f"   • {name}{sig}")
    if not hits:
        print("   (none found)")


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    phone = sys.argv[1]
    do_create = "--create" in sys.argv[2:]

    client = rb.open_client(phone)
    try:
        await rb.connect_ready(client)
        print(f"[ok] connected with session for {phone}")

        show_methods(client)
        dump_all_admin_like(client)

        if not do_create:
            print("\n[done] read-only discovery finished. Re-run with --create to "
                  "make a throwaway channel and test the admin/join wiring.")
            return

        # ---- optional: create a throwaway channel and probe admin/join -----
        print("\n" + "=" * 60)
        print("CREATE a throwaway channel to probe wiring (--create)")
        print("=" * 60)
        title = "تست موتور (پاک کن)"
        try:
            guid = await rb.create_channel(client, title)
            print(f"   ✅ channel created: {guid}")
        except Exception as e:  # noqa: BLE001
            print(f"   ❌ create_channel failed: {e!r}")
            return
        print("   ℹ️ این کانالِ تستی رو خودت دستی پاک کن.")
        # we do NOT auto-make-admin or auto-add here, because that needs a
        # second account's guid; just report the created guid so we can wire
        # the real engine against confirmed method names.
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
