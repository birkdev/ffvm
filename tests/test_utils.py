from ffvm.cli import (
    format_time,
    size_converter,
    clamp_sweep_crf,
)
import typer
import pytest


def test_format_time_seconds_int():
    assert format_time(45) == "45s"


def test_format_time_seconds_float():
    assert format_time(45.7) == "45s"


def test_format_time_minutes():
    assert format_time(154) == "2m 34s"


def test_format_time_hours():
    assert format_time(12680) == "3h 31m"


def test_format_time_zero():
    assert format_time(0) == "0s"


def test_format_time_edge():
    assert format_time(60) == "1m 0s"


def test_size_converter_bytes():
    assert size_converter(355) == "355 B"


def test_size_converter_kb():
    assert size_converter(35000) == "34 KB"


def test_size_converter_mb():
    assert size_converter(350000000) == "334 MB"


def test_size_converter_gb():
    assert size_converter(66000000000) == "61 GB"


def test_size_converter_tb():
    assert size_converter(9600000000000) == "8.7 TB"


def test_size_converter_edge():
    assert size_converter(1024) == "1024 B"


def test_clamp_sweep_crf_valid():
    assert clamp_sweep_crf(23, 32) == (23, 32)


def test_clamp_sweep_crf_high_min():
    assert clamp_sweep_crf(32, 23) == (15, 23)


def test_clamp_sweep_crf_narrow_range():
    with pytest.raises(typer.BadParameter):
        clamp_sweep_crf(12, 13)
