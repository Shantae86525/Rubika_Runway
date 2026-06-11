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


async def _try_call(fn, attempts):
    """Call fn trying several arg shapes; return first non-TypeError result."""
    last = None
    for make in attempts:
        a, k = make()
        try:
            return await fn(*a, **k)
        except TypeError as e:
            last = e
            continue
    raise RuntimeError(f"signature mismatch: {last}")


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

        # ---- optional: create throwaway channel + group, probe make-admin --
        print("\n" + "=" * 60)
        print("CREATE throwaway channel + group and probe set_group_admin (--create)")
        print("=" * 60)

        # our OWN guid is used as the member to (try to) promote — harmless,
        # we're already the owner; it just proves the method accepts a channel
        # guid vs a group guid without error.
        try:
            self_guid = await rb.get_self_guid(client)
            print(f"   self guid: {self_guid}")
        except Exception as e:  # noqa: BLE001
            print(f"   ❌ could not get self guid: {e!r}")
            return

        # full admin access list (rubpy accepts a list of access strings)
        access = ["SetAdmin", "ChangeInfo", "PinMessages", "DeleteGlobalAllMessages",
                  "BanMember", "SetJoinLink", "SetMemberAccess", "AddMember"]

        async def _try_set_admin(obj_guid, label):
            fn = getattr(client, "set_group_admin", None)
            if fn is None:
                print(f"   [{label}] ❌ set_group_admin missing")
                return
            attempts = [
                lambda: ((), {"group_guid": obj_guid, "member_guid": self_guid,
                              "action": "SetAdmin", "access_list": access}),
                lambda: ((obj_guid, self_guid, "SetAdmin", access), {}),
                lambda: ((), {"object_guid": obj_guid, "member_guid": self_guid,
                              "action": "SetAdmin", "access_list": access}),
            ]
            for mk in attempts:
                a, k = mk()
                try:
                    res = await fn(*a, **k)
                    print(f"   [{label}] ✅ set_group_admin worked -> "
                          f"{rb._data_of(res) or 'OK'}")
                    return
                except TypeError:
                    continue
                except Exception as e:  # noqa: BLE001
                    print(f"   [{label}] ⚠️ set_group_admin error: {e!r}")
                    return
            print(f"   [{label}] ❌ no argument shape matched")

        # 1) channel
        try:
            ch_guid = await rb.create_channel(client, "تست کانال موتور (پاک کن)")
            print(f"   ✅ channel created: {ch_guid}")
            await _try_set_admin(ch_guid, "CHANNEL")
        except Exception as e:  # noqa: BLE001
            print(f"   ❌ create channel failed: {e!r}")

        # 2) group — find the create-group method
        grp_fn = (getattr(client, "add_group", None)
                  or getattr(client, "create_group", None))
        if grp_fn is None:
            print("   ❌ no add_group()/create_group() on this build")
        else:
            try:
                res = await _try_call(grp_fn, [
                    lambda: ((), {"title": "تست گروه موتور (پاک کن)",
                                  "member_guids": [self_guid]}),
                    lambda: (("تست گروه موتور (پاک کن)", [self_guid]), {}),
                ])
                g_guid = rb._guid_of(res) or rb._channel_guid_of(res)
                print(f"   ✅ group created: {g_guid}")
                if g_guid:
                    await _try_set_admin(g_guid, "GROUP")
            except Exception as e:  # noqa: BLE001
                print(f"   ❌ create group failed: {e!r}")

        print("\n   ℹ️ کانال و گروهِ تستی رو خودت دستی پاک کن.")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
