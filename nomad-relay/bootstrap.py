"""One-time Zulip setup for the relay (ADR-008). Idempotent; safe to re-run.

Uses the operator's own API key (zulip-admin.zuliprc) because only the operator can change their
own notification settings and the org's settings. The relay itself runs on the bot's key.

  - creates #gate #alerts #runs #projects #digest, subscribes operator + bot
  - push/desktop notifications ON for #gate and #alerts, OFF for the rest
  - org: invite-only; only admins invite, create bots or add emoji; no message editing; no link or
    image previews (untrusted URLs in proposals would otherwise be fetched by the server)
  - writes ~/.config/nomad/relay.env (operator email + pinned user id) for the systemd unit
  - deletes zulip-admin.zuliprc: the relay never needs the operator's key, so it doesn't stay on disk
"""
import os
import sys

import zulip

import render as R

CFG = os.path.expanduser("~/.config/nomad")


def ok(res, what):
    if res.get("result") != "success":
        sys.exit(f"✗ {what}: {res.get('msg')}")
    return res


def main():
    admin = zulip.Client(config_file=os.path.join(CFG, "zulip-admin.zuliprc"))
    bot = zulip.Client(config_file=os.path.join(CFG, "zulip-bot.zuliprc"))
    me = ok(admin.get_profile(), "operator profile")
    b = ok(bot.get_profile(), "bot profile")
    if not b.get("is_bot"):
        sys.exit("✗ zulip-bot.zuliprc is not a bot account")
    if not me.get("is_admin"):
        sys.exit("✗ zulip-admin.zuliprc must be the organization owner/admin")

    ok(admin.add_subscriptions(
        streams=[{"name": n, "description": desc} for n, (desc, _) in R.CHANNELS.items()],
        principals=[me["user_id"], b["user_id"]], announce=False), "create channels")
    print("✓ channels:", ", ".join("#" + n for n in R.CHANNELS))

    settings = []
    for name, (_, loud) in R.CHANNELS.items():
        sid = ok(admin.get_stream_id(name), f"stream id {name}")["stream_id"]
        for prop in ("push_notifications", "desktop_notifications", "audible_notifications"):
            settings.append({"stream_id": sid, "property": prop, "value": loud})
    ok(admin.update_subscription_settings(settings), "notification settings")
    print("✓ push on for #gate #alerts, off for the rest")

    groups = ok(admin.call_endpoint("user_groups", method="GET"), "user groups")["user_groups"]
    admins = next(g["id"] for g in groups if g.get("name") == "role:administrators")
    admins_only = {"new": admins}
    ok(admin.call_endpoint("realm", method="PATCH", request={
        "invite_required": True,
        "want_advertise_in_communities_directory": False,
        "can_invite_users_group": admins_only,
        "can_create_multiuse_invite_group": admins_only,
        "can_create_bots_group": admins_only,
        "can_create_write_only_bots_group": admins_only,
        "can_add_custom_emoji_group": admins_only,
        "allow_message_editing": False,
        "inline_url_embed_preview": False,
        "inline_image_preview": False,
    }), "org hardening")
    print("✓ org hardened: invite-only, admin-only invites/bots/emoji, no edits, no previews")

    members = ok(admin.get_members(), "members")["members"]
    others = [u["email"] for u in members
              if u.get("is_active") and not u.get("is_bot") and u["user_id"] != me["user_id"]]
    if others:
        print(f"⚠ other active accounts: {others}. The relay acts on nothing until they're deactivated.")

    os.makedirs(CFG, exist_ok=True)
    path = os.path.join(CFG, "relay.env")
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(f"NOMAD_ZULIP_OPERATOR={me['email']}\nNOMAD_ZULIP_OPERATOR_ID={me['user_id']}\n")
    print(f"✓ {path}")

    ok(bot.send_message({"type": "direct", "to": [me["user_id"]],
                         "content": "NOMAD is connected. " + R.HELP}), "welcome DM")
    print("✓ welcome DM sent")

    os.remove(os.path.join(CFG, "zulip-admin.zuliprc"))
    print("✓ deleted zulip-admin.zuliprc")
    print("→ Now regenerate your API key in Zulip (Settings → Account & privacy → API key → Generate"
          " new API key) so the copy that was on disk is dead. Re-running bootstrap needs a fresh download.")


if __name__ == "__main__":
    main()
