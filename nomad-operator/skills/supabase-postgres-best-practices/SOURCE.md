Source: https://github.com/supabase-community/supabase-plugin @ f3f332e0164c34a8392772811737fda0cb972d06.
License: MIT, as declared in that commit's `.claude-plugin/plugin.json`. The repo ships no LICENSE
file, so there is no license text to copy here.

Vendored unchanged into the nomad-operator plugin (#1049). Before that it was installed as plain
skills in ~/.claude/skills on 2026-10-03 (#1007), instead of as the upstream plugin, so that no
write-capable Supabase MCP gets configured. The read-only MCP is the user-scope server "supabase-ro".
To update: copy `skills/<name>` from a newer commit and change the hash above.
