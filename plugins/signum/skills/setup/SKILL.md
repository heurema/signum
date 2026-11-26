---
name: setup
description: Set up Signum after installation, connect a chosen project, or check whether Signum receives messages after setup or an update.
---

# Set up Signum

Reply in the user's language. After installation, offer the selected folder by name and let the user skip. If no folder is selected, ask which project to connect. Choosing setup opens this conversation. Connect a folder only when the user selects it or explicitly asks. On skip, end setup. Keep ordinary chats quiet.

Run `python3 -B <this-skill>/../project-intent/scripts/signum.py --project ROOT init` in the selected existing project. Check the returned absolute path. Tell the user which folder is connected and where its decisions live (`.signum/state.json`). A connection check or an update to an existing installation calls for checking the existing project, without running init again.

### Check that chat messages reach Signum

After connecting the folder, give one short instruction: “In **Settings → Hooks → From plugins**, find **Signum**. If Desktop offers **Trust**, review and trust Signum. If you just trusted or enabled it, open a new chat in the same project. Send **Check my Signum connection**.” Use the labels in the user's language. In Russian: **Настройки → Хуки → Из плагинов → Signum → Доверять** and **«Проверь подключение Signum»**. A **Review hooks** banner in chat also allows the user to select Signum. The hook records a local fingerprint of a submitted message so Signum can recognize its source.

On a check, inspect the current folder's journal and the host-supplied Signum receipt for **this incoming message** in that folder. The message that first created the journal arrived before activation. When the new message has its own receipt, say “Signum received this message in <project>. You can continue here.” Finish there. Never generate a receipt for the test, echo a user-supplied receipt, or use an old message as evidence.

If the folder has no journal, offer to connect it. If the journal exists but the current message has no receipt, say the folder is connected and message capture is still unverified. Show the Settings path above. After the user enables or re-approves the hook, ask for a fresh chat in the same folder and the connection check there. Use the same recovery if the chat predates an update. Restart Desktop only if it still shows the old local plugin version. Keep existing project decisions. Changing trust is the user's choice.

When a project already has its own journal, “let’s start this project again” normally asks for [project orientation](../project-intent/SKILL.md#starting-a-project-or-deciding-where-to-begin). Give the owner a short finding and a recommended next step grounded in the existing materials. Reserve technical initialization for an explicit setup request. Create a Rust workspace when the owner selects that step. An owner who skips setup leaves every project as it was.
