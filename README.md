# Multichannel Audio Out for Music Assistant

A [Music Assistant](https://music-assistant.io) community provider that plays discrete 5.1 and
7.1 audio through a surround sound card or HDMI output, with every channel pair going to its own
speakers.

Music Assistant mixes every source down to stereo before it reaches a player. For multichannel
files from your local library, this provider reads the file itself and sends each channel pair
(front, center/LFE, surround, side) to its own PulseAudio stereo sink. Everything else (stereo
files, radio, streaming services) plays through Music Assistant's normal stream on the front pair.

## What you need

- A Music Assistant server with access to PulseAudio, such as the Music Assistant app on Home
  Assistant OS.
- A multichannel output: a surround sound card (for example a Creative X-Fi) or HDMI to an AV
  receiver, set to a 5.1 or 7.1 profile.
- One PulseAudio remap sink per channel pair on that output, for example created by an app that
  sets up stereo pairs, or by `module-remap-sink` lines in your PulseAudio configuration.
- Multichannel music as local files (FLAC, WAV and similar) in a Music Assistant filesystem
  music source.

## Setup

Add **Multichannel Audio Out** in Music Assistant under Settings > Players, then set:

| Setting | What to choose |
|---|---|
| PulseAudio sink | The 5.1 or 7.1 sink of the card or HDMI output. |
| Channel layout | 5.1 or 7.1, matching the profile of that output. |
| Channel map | FLAC / PCM standard for almost all files; DVD / AC3 if center and LFE come out swapped. |
| Front, Center / LFE, Rear, Side sinks | The remap sink that plays each channel pair. Leave them all on Auto-detect if your remap sinks are named `<prefix>_front_stereo`, `<prefix>_center_sub`, `<prefix>_rear_stereo` and `<prefix>_side_stereo`. |

The provider adds one player, **Multichannel Audio (5.1)** or **(7.1)**.

## Good to know

- Seek, pause and the progress bar work for files read directly.
- There is a short gap between two tracks that are both read directly.
- The player does not join sync groups with other players.
- Audio is resampled to the rate of the output sink.

## Support

Report problems and ideas in the [issues of this repository](https://github.com/iVolt1/ma-multichannel-audio/issues).
This provider is maintained by its author, not by the Music Assistant team.

## Development

This repository follows the
[community provider template](https://github.com/music-assistant/community-provider-template):
the provider is in `multichannel_audio/`, and `uv run pytest`, `uv run ruff check .`,
`uv run ruff format .`, `uv run mypy` and `uv run codespell` must pass. The tests stand in for
PulseAudio, so they run without a sound card; two of them need ffmpeg. The
[community catalog rules](https://github.com/music-assistant/.github/blob/main/COMMUNITY_CATALOG_RULES.md)
say what a community provider may do.
