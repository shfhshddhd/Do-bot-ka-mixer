# Do-bot-ka-mixer master bot

This repository is the writable master build. The original multi-user
Telethon bot remains the base system: its hosted-account login, plugins,
database, message bridge, AI mode, Mini App, self-update controls, and existing
commands are kept in `telegram_userbot/`.

## Private VC relay

After using `/host`, create a private Telegram group, add the control bot as an
administrator, and grant it **Manage Voice Chats**. Register it from the
control bot:

```text
/privategroupvcsetup <control_group_chat_id>
```

The registered group is also the default source/personal VC. A separate source
VC can be supplied when needed:

```text
/privategroupvcsetup <control_group_chat_id> <source_vc_group_chat_id>
```

The hosted Telegram account must be a member of the source and target groups,
and a voice chat must already be active in both groups. Control commands are
accepted only in the registered group:

```text
/join <target_group_chat_id>       # or .vcjoin <target_group_chat_id>
/leave [target_group_chat_id]
/leaveall
/leaveplay [target_group_chat_id]
/level <1-25> [target_group_chat_id]
/bass <0-15> [target_group_chat_id]
/mute [target_group_chat_id]
/unmute [target_group_chat_id]
/startrecord [target_group_chat_id]
/stoprecord
/speedtest
```

The owner who registered the group and current Telegram administrators of that
group may issue these commands. They all execute through the original owner's
hosted session; an administrator does not need to host a separate account.

## Audio architecture

The relay uses the pinned `py-tgcalls==2.3.3` native frame API:

1. One `PyTgCalls` instance attaches to the existing hosted Telethon client.
2. The hosted account joins the source and target VCs with
   `MediaStream(ExternalMedia.AUDIO)`.
3. `StreamFrames` updates for incoming `Device.SPEAKER` frames are received
   from the source call.
4. Frames are optionally processed as PCM16 for level, bass, and mute controls,
   then sent to the target with `send_frame(..., Device.MICROPHONE, ...)`.

No duplicate Telegram session or competing PyTgCalls client is created. The
manager owns watchdog reconnects, recordings, playback restoration, and
unhost cleanup. The relay sends no Telegram messages from the hosted account;
control replies and recording uploads come from the Bot API control bot.

`tests/` covers the deterministic PCM frame controls. The finite verification
commands are:

```text
python -m compileall -q telegram_userbot tests
PYTHONPATH=.:telegram_userbot python -m unittest discover -s tests -v
```

Live Telegram voice audio still requires a deployed manual test because this
sandbox cannot join real voice chats. The connected GitHub write proxy accepted
the source commits but rejected `.github/workflows/*` paths, so GitHub Actions
must be enabled by adding the two workflow files from the checkout through
GitHub's normal UI or CLI.