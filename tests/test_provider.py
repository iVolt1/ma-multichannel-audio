"""Tests of the provider, loaded by a real server."""

import asyncio
import importlib
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from music_assistant_models.config_entries import ConfigValueOption
from music_assistant_models.enums import ProviderTier, StreamType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.translations import TRANSLATION_RESOLVER

from music_assistant.mass import MusicAssistant
from music_assistant.models.player import PlayerMedia
from tests import DOMAIN

# the server imports a community provider under its own providers package
MODULE = f"music_assistant.providers.{DOMAIN}"
HDMI = "alsa_output.pci-0000_08_00.1.hdmi-surround71-extra1"
SINKS = [HDMI, "Front", "Center", "Surround"]
PAIRS = {
    "sink_front_stereo": "Front",
    "sink_center_sub": "Center",
    "sink_rear_stereo": "Surround",
}
# one tone per channel of a 5.1 file: FL, FR, FC, LFE, SL, SR
TONES = {"Front": (200, 300), "Center": (500, 700), "Surround": (1100, 1300)}
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


class FakeSink:
    """Stand-in for PASimpleStream that records what each sink receives."""

    received: ClassVar[dict[str, bytearray]] = {}
    events: ClassVar[list[tuple[str, str]]] = []
    realtime = False

    def __init__(self, sink_name: str, **kwargs: Any) -> None:
        """Open the fake stream."""
        self.sink = sink_name
        self.rate = kwargs["rate"]
        FakeSink.received[sink_name] = bytearray()

    def write(self, data: bytes) -> None:
        """Record a chunk; real sinks drain in parallel, so pace on one of them."""
        FakeSink.received[self.sink] += data
        if FakeSink.realtime and self.sink == "Front":
            import time  # noqa: PLC0415

            time.sleep(len(data) / (self.rate * 2 * 4))

    def drain(self) -> None:
        """Record a drain."""
        FakeSink.events.append((self.sink, "drain"))

    def close(self) -> None:
        """Record a close."""
        FakeSink.events.append((self.sink, "close"))

    def get_latency(self) -> float:
        """Report a fixed latency."""
        return 0.3


@pytest.fixture(name="surround_file", scope="session")
def surround_file_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write a 2 second 5.1 FLAC with a different tone on each channel."""
    path = tmp_path_factory.mktemp("audio") / "tones51.flac"
    inputs = [
        arg
        for f in (200, 300, 500, 700, 1100, 1300)
        for arg in ("-f", "lavfi", "-i", f"sine=f={f}:d=2")
    ]
    mapping = "0.0-FL|1.0-FR|2.0-FC|3.0-LFE|4.0-SL|5.0-SR"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            f"[0][1][2][3][4][5]join=inputs=6:channel_layout=5.1(side):map={mapping}",
            "-ar",
            "48000",
            "-sample_fmt",
            "s16",
            str(path),
        ],
        check=True,
    )
    return path


@contextmanager
def _system(*, pulse: bool = True) -> Iterator[None]:
    """Stand in for the PulseAudio parts of a host this test runner does not have."""
    init_mod = importlib.import_module(MODULE)
    prov_mod = importlib.import_module(f"{MODULE}.provider")

    options = [ConfigValueOption(title=name, value=name) for name in SINKS]
    cdll: Any = MagicMock() if pulse else MagicMock(side_effect=OSError("not found"))
    with (
        patch.object(init_mod, "_get_pa_sink_options", return_value=options),
        patch.object(prov_mod.ctypes, "CDLL", cdll),
        patch.object(prov_mod, "_query_sink_format", return_value=(192000, 32, 8)),
        patch.object(prov_mod, "_derive_card_name", return_value=None),
    ):
        yield


async def _add_provider(mass: MusicAssistant, values: dict[str, Any]) -> Any:
    """Add the provider through its setup flow, configure it and return its player protocol."""
    step = await mass.config.setup_provider(DOMAIN)
    assert step.result is not None
    instance_id = step.result["instance_id"]
    await mass.config.save_provider_config(DOMAIN, values, instance_id=instance_id)
    for _ in range(100):
        provider = cast("Any", mass.get_provider(instance_id))
        if provider and provider._player and provider._player.player:
            return provider._player
        await asyncio.sleep(0.05)
    raise AssertionError("the player was not registered")


def _queue_item(stream_type: StreamType, path: str | None, channels: int, seek: float = 0) -> Any:
    details = MagicMock(stream_type=stream_type, path=path, seek_position=seek)
    details.audio_format.channels = channels
    details.audio_format.sample_rate = 48000
    return MagicMock(streamdetails=details)


async def _play(mass: MusicAssistant, player: Any, item: Any, stop_after: float = 0) -> None:
    """Play one queue item on the player and wait for its playback to finish."""
    FakeSink.received.clear()
    FakeSink.events.clear()
    with (
        patch(f"{MODULE}.pa_simple.PASimpleStream", FakeSink),
        patch.object(mass.player_queues, "get_active_queue", return_value=MagicMock(queue_id="q")),
        patch.object(mass.player_queues, "get_item", return_value=item),
    ):
        await player.play_media(PlayerMedia(uri="x", queue_item_id="qi"))
        if stop_after:
            await asyncio.sleep(stop_after)
            await player.stop()
        for _ in range(200):
            await asyncio.sleep(0.05)
            if player._playback_task is None or player._playback_task.done():
                return


def _peak_hz(samples: np.ndarray, rate: int) -> int:
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return int(np.fft.rfftfreq(len(samples), 1 / rate)[int(np.argmax(spectrum))].round())


def _assert_pairs(rate: int, seconds: float) -> None:
    for sink, expected in TONES.items():
        pcm = np.frombuffer(bytes(FakeSink.received[sink]), dtype=np.int32).reshape(-1, 2)
        middle = pcm[len(pcm) // 4 : len(pcm) // 4 + rate // 4].astype(float)
        got = (_peak_hz(middle[:, 0], rate), _peak_hz(middle[:, 1], rate))
        assert all(abs(g - e) < 5 for g, e in zip(got, expected, strict=True)), (sink, got)
        assert abs(len(pcm) / rate - seconds) < 0.05, (sink, len(pcm) / rate)


async def test_provider_loads(mass: MusicAssistant) -> None:
    """The server loads the provider from this repository as a community provider."""
    assert mass.get_provider_manifest(DOMAIN).tier == ProviderTier.COMMUNITY


async def test_configured_pair_sinks(mass: MusicAssistant) -> None:
    """Configured pair sinks are used as they are, mapped to their channel pairs."""
    with _system():
        player = await _add_provider(mass, {"pa_sink_name": HDMI, **PAIRS})
    assert player._pair_sinks == {"Front": (0, 1), "Center": (2, 3), "Surround": (4, 5)}
    assert player.flow_mode is False


async def test_settings_texts(mass: MusicAssistant) -> None:
    """Labels and option titles of the settings resolve from strings.json."""
    with _system():
        player = await _add_provider(mass, {"pa_sink_name": HDMI, **PAIRS})
        entries = await mass.config._resolve_provider_config_entries(player.provider)
    token = TRANSLATION_RESOLVER.set(partial(mass.translations.get_translation, locale="en"))
    try:
        serialized = {entry.key: entry.to_dict() for entry in entries}
    finally:
        TRANSLATION_RESOLVER.reset(token)
    assert serialized["sink_front_stereo"]["label"] == "Front L/R sink"
    assert [o["title"] for o in serialized["sink_front_stereo"]["options"]] == [
        "Auto-detect",
        *SINKS,
    ]
    assert [o["title"] for o in serialized["multichannel_layout"]["options"]] == [
        "5.1 Surround (6 channels)",
        "7.1 Surround (8 channels)",
    ]


async def test_without_pulseaudio(mass: MusicAssistant) -> None:
    """Without libpulse the provider reports a handled setup error."""
    with _system(pulse=False), pytest.raises(MusicAssistantError, match="PulseAudio"):
        await mass.config.setup_provider(DOMAIN)


async def test_routing(mass: MusicAssistant) -> None:
    """A multichannel local file is read from disk; anything else uses the stream of the server."""
    with _system():
        player = await _add_provider(mass, {"pa_sink_name": HDMI, **PAIRS})
    cases = [
        (_queue_item(StreamType.LOCAL_FILE, "/media/a.flac", 6), ("/media/a.flac", 6), False),
        (_queue_item(StreamType.LOCAL_FILE, "/media/b.flac", 2), ("http://ma/x.flac", 2), True),
        (_queue_item(StreamType.HTTP, None, 2), ("http://ma/x.flac", 2), True),
    ]
    for item, expected, uses_stream in cases:
        with (
            patch.object(type(player), "_playback_loop", AsyncMock()) as loop,
            patch.object(
                mass.streams, "resolve_stream_url", AsyncMock(return_value="http://ma/x.flac")
            ) as url,
            patch.object(
                mass.player_queues, "get_active_queue", return_value=MagicMock(queue_id="q")
            ),
            patch.object(mass.player_queues, "get_item", return_value=item),
        ):
            await player.play_media(PlayerMedia(uri="x", queue_item_id="qi"))
            await asyncio.sleep(0.05)
        assert loop.call_args.args[-3:-1] == expected
        assert url.called is uses_stream


@needs_ffmpeg
@pytest.mark.parametrize("seek", [0, 1.0])
async def test_plays_each_pair(mass: MusicAssistant, surround_file: Path, seek: float) -> None:
    """Each sink receives its own channel pair of the file, from the seek position on."""
    with _system():
        player = await _add_provider(mass, {"pa_sink_name": HDMI, **PAIRS})
    await _play(mass, player, _queue_item(StreamType.LOCAL_FILE, str(surround_file), 6, seek))
    _assert_pairs(player.sample_rate, 2.0 - seek)
    # drained before closing, so the end of the file is heard
    for sink in TONES:
        assert FakeSink.events.index((sink, "drain")) < FakeSink.events.index((sink, "close"))


@needs_ffmpeg
async def test_pause_keeps_audio(mass: MusicAssistant, surround_file: Path) -> None:
    """A pause holds the audio and the elapsed time; nothing is skipped on resume."""
    with _system():
        player = await _add_provider(mass, {"pa_sink_name": HDMI, **PAIRS})
    FakeSink.realtime = True
    try:
        task = asyncio.create_task(
            _play(mass, player, _queue_item(StreamType.LOCAL_FILE, str(surround_file), 6))
        )
        await asyncio.sleep(0.6)
        await player.pause()
        frozen = player.corrected_elapsed_time
        await asyncio.sleep(0.5)
        assert player.corrected_elapsed_time == frozen
        await player.play()
        await task
    finally:
        FakeSink.realtime = False
    _assert_pairs(player.sample_rate, 2.0)


@needs_ffmpeg
async def test_stop_does_not_drain(mass: MusicAssistant, surround_file: Path) -> None:
    """A stop closes the streams at once."""
    with _system():
        player = await _add_provider(mass, {"pa_sink_name": HDMI, **PAIRS})
    FakeSink.realtime = True
    try:
        await _play(
            mass, player, _queue_item(StreamType.LOCAL_FILE, str(surround_file), 6), stop_after=0.4
        )
    finally:
        FakeSink.realtime = False
    assert not any(action == "drain" for _, action in FakeSink.events)
