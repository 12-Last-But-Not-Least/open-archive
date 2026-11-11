# Transcript maintenance

User instruction: update the conversation transcript at the end of each turn and before every push.

- Export the active Open Archive conversation into `LLM Transcripts/conversation.txt` using `scripts/export_transcript.py --thread-id <active-thread-id>`.
- Run the export immediately before preparing a commit for a push, and include the updated transcript in that commit.
- Refresh the local transcript before the final response of each turn. The final response itself will be included by the next export once it is saved in chat history.
- A transcript update alone does not require an otherwise unrequested push.
- Include only user messages and user-facing assistant replies/progress updates. Exclude internal instructions, reasoning, and raw tool calls/results. Preserve wording; mark omitted images and redact credentials and local user-directory prefixes.
- If the chat history cannot be accessed, report that limitation; do not invent missing messages.
