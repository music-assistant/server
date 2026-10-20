# Reviewing a new provider

The matching demo provider is the ground truth — it's the annotated template that encodes the required structure, lifecycle and config schema:

| Provider type | Reference |
|---|---|
| Music source | `music_assistant/providers/_demo_music_provider` |
| Player | `music_assistant/providers/_demo_player_provider` |
| Plugin | `music_assistant/providers/_demo_plugin_provider` |
| Audio analysis | `music_assistant/providers/_demo_audio_analysis_provider` |

Read the demo provider alongside the new one, then flag deviations from its requirements and patterns as `[PROBLEM]` or `[CRITICAL]` depending on what breaks.

Provider icons (`icon.svg`) are capped at 5KB — anything larger is `[CRITICAL]`.
