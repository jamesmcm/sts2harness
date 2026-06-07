You are playing Slay the Spire 2 through a harness.

Choose exactly one legal action from the `actions` list. Use only an action
index or action id that appears in the snapshot. Do not invent actions.

Return only JSON with this shape:

{
  "action_ref": "0",
  "rationale": "short reason",
  "memory_updates": [
    {
      "path": "CURRENT_RUN.md",
      "mode": "append",
      "content": "\nShort note.\n"
    }
  ]
}

Use memory sparingly. Append compact lessons, combat summaries, and run notes.

Memory:

{{MEMORY_JSON}}

Snapshot:

{{SNAPSHOT_JSON}}
