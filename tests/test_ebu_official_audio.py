"""Optional official EBU v5 files: set EBU_LOUDNESS_TEST_DIR to the extracted ZIP.

References: Tech 3341 (2023) table 1 and Tech 3342 (2023) table 1.
Only Integrated, maximum True Peak and LRA cases apply to this UI.
Momentary/short-term display tests and full meter certification are out of scope.
"""
import os
import re
from pathlib import Path

import pytest

from app.services.audio_analysis import analyze_audio

CASES = (
    [(3341, n, "integrated_lufs", -33 if n == 2 else -23, 0.1, 0.1) for n in range(1, 9)]
    + [(3341, n, "true_peak_dbtp", -6 if n < 19 else 3 if n == 19 else 0, 0.4, 0.2) for n in range(15, 24)]
    + [(3342, n, "loudness_range_lu", value, 1, 1) for n, value in enumerate([10, 5, 20, 15, 5, 15], 1)]
)


@pytest.mark.parametrize("document,case,metric,expected,below,above", CASES)
def test_official_ebu_file(document, case, metric, expected, below, above):
    location = os.environ.get("EBU_LOUDNESS_TEST_DIR")
    if not location:
        pytest.skip("Official EBU files not configured; set EBU_LOUDNESS_TEST_DIR")
    pattern = re.compile(rf"seq[-_]{document}[-_](?:2011[-_])?0?{case}(?:[-_.]|$)", re.I)
    files = [p for p in Path(location).rglob("*") if p.suffix.lower() == ".wav" and pattern.search(p.name)]
    assert files, f"Missing official sequence {document}-{case} in {location}"
    for path in files:
        result = analyze_audio(path)
        actual = getattr(result, metric)
        assert actual is not None and expected-below <= actual <= expected+above, (path.name, metric, actual, expected)
