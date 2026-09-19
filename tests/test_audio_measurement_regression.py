"""Generated signals with independent analytic expectations; no certification claim."""
import math
import shutil
import subprocess

import pytest

from app.config import settings
from app.services.audio_analysis import analyze_audio, probe_audio

pytestmark = pytest.mark.skipif(not shutil.which(settings.FFMPEG_PATH), reason="FFmpeg unavailable")


def render(tmp_path, expression, duration, *, rate=48000, codec="pcm_f32le"):
    path = tmp_path / "signal.wav"
    subprocess.run([settings.FFMPEG_PATH, "-v", "error", "-f", "lavfi", "-i",
                    f"aevalsrc={expression}:s={rate}:d={duration}", "-c:a", codec, "-y", str(path)], check=True)
    return path


def test_low_level_and_partial_final_frame_peak(tmp_path):
    low = 10 ** (-57.1 / 20)
    path = render(tmp_path, f"if(lt(t\\,1)\\,{low}*sin(2*PI*1000*t)\\,0.5*sin(2*PI*1000*t))", 1.05)
    result = analyze_audio(path)
    assert result.sample_peak_dbfs == pytest.approx(20 * math.log10(0.5), abs=0.001)
    assert result.true_peak_dbtp == pytest.approx(20 * math.log10(0.5), abs=0.1)
    path = render(tmp_path, f"{low}*sin(2*PI*1000*t)", 1.05)
    result = analyze_audio(path)
    assert result.sample_peak_dbfs == pytest.approx(-57.1, abs=0.001)
    assert result.true_peak_dbtp == pytest.approx(-57.1, abs=0.1)


def test_fullscale_997_hz_calibration(tmp_path):
    result = analyze_audio(render(tmp_path, "sin(2*PI*997*t)", 10))
    assert result.integrated_status == "valid"
    assert result.integrated_lufs == pytest.approx(-3.01, abs=0.1)
    assert result.sample_peak_dbfs == pytest.approx(0, abs=0.001)
    assert result.ffmpeg_version and result.analyzer_version == 2


@pytest.mark.parametrize("seconds,status", [(0.05, "too_short"), (0.39, "too_short"), (0.4, "valid"), (2.99, "valid"), (3, "valid")])
def test_short_measurement_validity(tmp_path, seconds, status):
    result = analyze_audio(render(tmp_path, "0.5*sin(2*PI*997*t)", seconds))
    assert result.integrated_status == status
    assert (result.integrated_lufs is not None) == (status == "valid")
    assert result.lra_status == ("too_short" if seconds < 3 else "short_programme")
    assert (result.loudness_range_lu is not None) == (seconds >= 3)


@pytest.mark.parametrize("expression,status", [("0", "silence"), ("0.00001*sin(2*PI*997*t)", "below_gate")])
def test_silence_and_below_gate_are_not_fake_measurements(tmp_path, expression, status):
    result = analyze_audio(render(tmp_path, expression, 4))
    assert result.integrated_status == status
    assert result.lra_status == status
    assert result.integrated_lufs is None and result.loudness_range_lu is None
    assert "Infinity" not in result.model_dump_json() and "NaN" not in result.model_dump_json()


def test_channels_dc_offset_and_inter_sample_peak(tmp_path):
    # A quarter-rate sine sampled at pi/4 has sample peaks 3.01 dB below its continuous peak.
    result = analyze_audio(render(tmp_path, "0.5*sin(2*PI*12000*t+PI/4)|0.1+0.2*sin(2*PI*1000*t)", 3))
    assert result.sample_peak_dbfs_by_channel == pytest.approx([-9.0309, 20*math.log10(0.3)], abs=0.001)
    assert result.true_peak_dbtp_by_channel[0] > result.sample_peak_dbfs_by_channel[0] + 2.5
    assert result.true_peak_dbtp == max(result.true_peak_dbtp_by_channel)
    assert result.dc_offset_by_channel == pytest.approx([0, 0.1], abs=1e-6)
    assert result.duration_seconds == pytest.approx(3, abs=0.001)


@pytest.mark.parametrize("levels,expected", [([-20,-30],10), ([-20,-15],5), ([-40,-20],20), ([-50,-35,-20,-35,-50],15)])
def test_tech3342_synthetic_lra_cases(tmp_path, levels, expected):
    # Tech 3342 section 6.2, 20 seconds per segment, mono 1 kHz sine.
    amplitude = str(10**(levels[-1]/20))
    for index in reversed(range(len(levels)-1)):
        amplitude = f"if(lt(t\\,{20*(index+1)})\\,{10**(levels[index]/20)}\\,{amplitude})"
    result = analyze_audio(render(tmp_path, f"({amplitude})*sin(2*PI*1000*t)", 20*len(levels)))
    assert result.loudness_range_lu == pytest.approx(expected, abs=1)


@pytest.mark.parametrize("rate", [44100,48000,88200,96000,176400,192000])
@pytest.mark.parametrize("codec,format", [("pcm_s16le","pcm_s16"),("pcm_s24le","pcm_s24"),("pcm_s32le","pcm_s32"),("pcm_f32le","pcm_f32")])
def test_supported_rates_and_formats(tmp_path, rate, codec, format):
    result = analyze_audio(render(tmp_path, "0.5*sin(2*PI*997*t)", 0.1, rate=rate, codec=codec))
    assert result.sample_rate_hz == rate and result.sample_format == format
    assert result.sample_peak_dbfs == pytest.approx(-6.0206, abs=0.01)
    assert result.true_peak_dbtp == pytest.approx(-6.0206, abs=0.1)


def test_compressed_decoder_float_is_not_float_pcm(tmp_path):
    source = render(tmp_path, "0.5*sin(2*PI*997*t)", 1)
    target = tmp_path / "signal.mp3"
    subprocess.run([settings.FFMPEG_PATH, "-v", "error", "-i", str(source), "-y", str(target)], check=True)
    result = probe_audio(target)
    assert result.codec == "mp3" and result.sample_format is None


@pytest.mark.parametrize("case,segments,expected", [
    (1, [(20,-23)], -23), (2, [(20,-33)], -33),
    (3, [(10,-36),(60,-23),(10,-36)], -23),
    (4, [(10,-72),(10,-36),(60,-23),(10,-36),(10,-72)], -23),
    (5, [(20,-26),(20.1,-20),(20,-26)], -23),
])
def test_tech3341_generated_integrated_cases(tmp_path, case, segments, expected):
    # Table 1, cases 1–5. Generated from the published signal definitions.
    amplitude = str(10**(segments[-1][1]/20))
    boundaries = [sum(s[0] for s in segments[:i+1]) for i in range(len(segments)-1)]
    for index in reversed(range(len(segments)-1)):
        amplitude = f"if(lt(t\\,{boundaries[index]})\\,{10**(segments[index][1]/20)}\\,{amplitude})"
    expression = f"({amplitude})*sin(2*PI*1000*t)"
    result = analyze_audio(render(tmp_path, expression+'|'+expression, sum(s[0] for s in segments)))
    assert result.integrated_lufs == pytest.approx(expected, abs=0.1)


@pytest.mark.parametrize("case,divisor,amplitude,phase,expected", [
    (15,4,0.5,0,-6), (16,4,0.5,45,-6), (17,6,0.5,60,-6), (18,8,0.5,67.5,-6), (19,4,1.41,45,3),
])
def test_tech3341_generated_true_peak_cases(tmp_path, case, divisor, amplitude, phase, expected):
    # Table 1, 10 ms linear fades, accepted tolerance +0.2 / -0.4 dBTP.
    expression = f"{amplitude}*min(1\\,min(t/0.01\\,(1-t)/0.01))*sin(2*PI*{48000/divisor}*t+{phase}*PI/180)"
    result = analyze_audio(render(tmp_path, expression+'|'+expression, 1))
    assert expected-0.4 <= result.true_peak_dbtp <= expected+0.2


def test_all_passes_select_first_audio_stream(tmp_path):
    target = tmp_path / "multiple.mkv"
    subprocess.run([settings.FFMPEG_PATH, "-v", "error", "-f", "lavfi", "-i", "aevalsrc=0.1*sin(2*PI*997*t):d=4:s=48000",
                    "-f", "lavfi", "-i", "aevalsrc=0.8*sin(2*PI*997*t):d=4:s=44100",
                    "-map", "0:a", "-map", "1:a", "-c:a", "pcm_f32le", "-y", str(target)], check=True)
    result = analyze_audio(target)
    assert result.sample_rate_hz == 48000
    assert result.sample_peak_dbfs == pytest.approx(-20, abs=0.001)
    assert result.true_peak_dbtp == pytest.approx(-20, abs=0.1)
    assert result.integrated_lufs == pytest.approx(-23.01, abs=0.1)


def test_short_first_stream_duration_is_not_container_duration(tmp_path):
    target = tmp_path / "mixed-durations.mkv"
    subprocess.run([settings.FFMPEG_PATH, "-v", "error", "-f", "lavfi", "-i", "aevalsrc=0.1*sin(2*PI*997*t):d=0.2:s=48000",
                    "-f", "lavfi", "-i", "aevalsrc=0.8*sin(2*PI*997*t):d=5:s=44100",
                    "-map", "0:a", "-map", "1:a", "-c:a", "pcm_f32le", "-y", str(target)], check=True)
    result = analyze_audio(target)
    assert result.duration_seconds == pytest.approx(0.2, abs=0.001)
    assert result.integrated_status == "too_short" and result.lra_status == "too_short"
