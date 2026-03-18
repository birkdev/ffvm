from tempfile import TemporaryDirectory
from rich.progress import Progress
from rich.console import Console
from rich.table import Table
from rich.text import Text
from typing import Optional
from pathlib import Path
from shutil import which
from enum import Enum
import subprocess
import threading
import typer
import time
import re

app = typer.Typer()
console = Console()


def get_ffmpeg() -> str:
    path = which("ffmpeg")
    if path is None:
        console.print("[bold red]ffmpeg not found in PATH[/bold red]")
        raise typer.Exit(1)
    return path


class VideoCodecs(str, Enum):
    copy = "copy"
    libx264 = "libx264"
    libx265 = "libx265"
    libsvtav1 = "libsvtav1"


class AudioCodecs(str, Enum):
    copy = "copy"
    aac = "aac"
    libopus = "libopus"


def find_videos(path: Path, recursive: bool = False) -> list[Path]:
    video_containers = [
        ".mp4",
        ".m4v",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
        ".ts",
        ".flv",
        ".ogg",
        ".ogv",
        ".mxf",
    ]

    if recursive:
        return [f for f in path.rglob("*") if f.suffix.lower() in video_containers]

    return [f for f in path.glob("*") if f.suffix.lower() in video_containers]


def clamp_crf(crf: int = 23, vcodec: VideoCodecs = VideoCodecs.libx264) -> int:
    if vcodec in (VideoCodecs.libx264, VideoCodecs.libx265) and crf > 51:
        console.print(
            "CRF value too high! Automatically lowered to [cyan]51[/cyan].",
            style="yellow",
        )
        crf = 51

    return crf


def clamp_sweep_crf(
    crf_min: int = 23, crf_max: int = 32, vcodec: VideoCodecs = VideoCodecs.libx264
) -> tuple[int, int]:
    if vcodec in (VideoCodecs.libx264, VideoCodecs.libx265) and crf_max > 51:
        console.print(
            "Max CRF value too high! Automatically lowered to [cyan]51[/cyan]."
        )
        crf_max = 51

    if crf_min > crf_max:
        crf_min = crf_max * 2 // 3
        console.print(
            "Min CRF value higher than max CRF value! "
            f"Automatically lowered min CRF to [cyan]{crf_min}[/cyan]."
        )

    if crf_max - crf_min < 2:
        raise typer.BadParameter("CRF range too narrow for sweep")

    return crf_min, crf_max


def make_output_paths(
    input_dir: Path,
    output_dir: Path,
    input_videos: list[Path],
    vcodec: VideoCodecs = VideoCodecs.libx264,
    crf: int | list[int] = 23,
) -> list[Path]:
    output_videos = []
    for i, input_video in enumerate(input_videos):
        c = crf[i] if isinstance(crf, list) else crf
        relative_path = input_video.relative_to(input_dir)
        output_video = (
            output_dir
            / relative_path.parent
            / (
                input_video.stem
                + "_"
                + vcodec.value
                + "_"
                + str(c)
                + input_video.suffix
            )
        )
        output_video.parent.mkdir(parents=True, exist_ok=True)
        output_videos.append(output_video)

    return output_videos


def get_duration(video: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        video,
    ]
    total_duration = subprocess.run(command, capture_output=True, text=True)

    return float(total_duration.stdout.strip())


def size_converter(size: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if size > 1024:
            size /= 1024
        else:
            if size >= 10:
                return f"{size:.0f} {unit}"
            else:
                return f"{size:.1f} {unit}"

    if size >= 10:
        return f"{size:.0f} {units[-1]}"
    else:
        return f"{size:.1f} {units[-1]}"


def format_time(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if seconds < 60:
        return f"{seconds}s"

    if seconds < 3600:
        return f"{minutes}m {secs}s"

    return f"{hours}h {minutes}m"


def extract_segments(input_video: Path) -> list[tuple[float, float]]:
    video_duration = get_duration(input_video)
    start = video_duration * 0.10
    end = video_duration * 0.90
    usable = end - start

    num_segments = max(1, min(7, round(video_duration / 180)))
    segment_length = max(1.0, min(30.0, usable / (num_segments * 2)))

    gap = usable / (num_segments + 1)
    segments = []

    for i in range(1, num_segments + 1):
        center = start + gap * i
        seg_start = center - segment_length / 2
        segments.append((round(seg_start, 2), round(segment_length, 2)))

    return segments


def build_encode_cmd(
    input_video: Path,
    output_video: Path,
    vcodec: VideoCodecs = VideoCodecs.libx264,
    crf: int = 23,
    acodec: AudioCodecs = AudioCodecs.copy,
    ab: Optional[str] = None,
    resolution: Optional[str] = None,
) -> list:
    cmd = [
        get_ffmpeg(),
        "-i",
        input_video,
        "-vcodec",
        vcodec.value,
        "-crf",
        str(crf),
        "-acodec",
        acodec.value,
    ]

    if ab:
        cmd += ["-ab", ab]

    if resolution is not None:
        cmd += ["-s", resolution]

    cmd += ["-progress", "pipe:1", "-y", output_video]
    return cmd


def build_vmaf_cmd(output_video: Path, input_video: Path) -> list:
    cmd = [
        get_ffmpeg(),
        "-i",
        output_video,
        "-i",
        input_video,
        "-lavfi",
        "libvmaf=n_threads=0",
        "-f",
        "null",
        "-",
        "-progress",
        "pipe:1",
    ]

    return cmd


def build_cut_cmd(
    input_video: Path, output_video: Path, cut_start: float, cut_length: float
) -> list:
    cmd = [
        get_ffmpeg(),
        "-ss",
        str(cut_start),
        "-t",
        str(cut_length),
        "-i",
        input_video,
        "-c",
        "copy",
        "-y",
        output_video,
    ]

    return cmd


def progress_bar(
    duration: float, description: str, process: subprocess.Popen[str]
) -> None:
    with Progress(transient=True) as progress:
        task = progress.add_task(description, total=duration)
        if process.stdout:
            for line in process.stdout:
                if line.startswith("out_time_ms="):
                    value = line.split("=", 1)[1].strip()
                    if value != "N/A":
                        current_time = float(value) / 1000000
                        progress.update(task, completed=current_time)
        progress.update(task, completed=duration)


def read_stderr(process: subprocess.Popen[str], output: list[str]) -> None:
    if process.stderr:
        output.append(process.stderr.read())


def run_with_progress(command, duration, description) -> str:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stderr = []
    stderr_thread = threading.Thread(target=read_stderr, args=(process, stderr))
    stderr_thread.start()
    progress_bar(duration, description, process)
    stderr_thread.join()
    process.wait()

    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {''.join(stderr)}")

    return "".join(stderr)


def run_vmaf(command, duration, description):
    output = run_with_progress(command, duration, description)

    if output is None:
        return 0.0

    for line in output.splitlines():
        match = re.search(r"VMAF score: (\d+\.\d+)", line)

        if match:
            return round(float(match.group(1)), 2)


def sweeping(
    input_video: Path,
    vcodec: VideoCodecs,
    target_vmaf: float,
    crf_min: int,
    crf_max: int,
) -> int:
    segments = extract_segments(input_video)
    crf_range = crf_max - crf_min
    crf = round(crf_min + (crf_range / 2))

    with TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        test_videos = []
        for start, length in segments:
            output_video = tmp / f"tmpvid_{start}.mp4"
            cmd = build_cut_cmd(input_video, output_video, start, length)
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            test_videos.append(output_video)

        while crf_max - crf_min > 1:
            vmaf_scores = []

            for i, test_video in enumerate(test_videos, start=1):
                output_video = tmp / f"{test_video.stem}_crf{crf}.mp4"
                cmd = build_encode_cmd(test_video, output_video, vcodec, crf)
                run_with_progress(
                    cmd,
                    get_duration(test_video),
                    f"Sweeping CRF {crf} [{i}/{len(test_videos)}] Encoding",
                )

                cmd = build_vmaf_cmd(output_video, test_video)
                score = run_vmaf(
                    cmd,
                    get_duration(test_video),
                    f"Sweeping CRF {crf} [{i}/{len(test_videos)}] Scoring",
                )
                vmaf_scores.append(score)

            average_vmaf = sum(vmaf_scores) / len(vmaf_scores)

            if average_vmaf > target_vmaf:
                crf_min = crf
            else:
                crf_max = crf
            crf = round((crf_min + crf_max) / 2)

    return crf


@app.command(help="Encode a single video")
def encode(
    input_video: Path = typer.Argument(
        ..., exists=True, dir_okay=False, resolve_path=True
    ),
    vcodec: VideoCodecs = VideoCodecs.libx264,
    crf: int = typer.Option(23, min=0, max=63),
    # optional vcodec parameters here soon
    acodec: AudioCodecs = AudioCodecs.copy,
    ab: Optional[str] = None,
    resolution: Optional[str] = None,
    compare: bool = False,
    output_video: Path = typer.Argument(..., dir_okay=False, resolve_path=True),
):
    if output_video.exists():
        typer.confirm(f"{output_video} already exists. Overwrite?", abort=True)
        print("\033[A\033[2K", end="")

    crf = clamp_crf(crf, vcodec)

    input_size = input_video.stat().st_size

    encode_cmd = build_encode_cmd(
        input_video, output_video, vcodec, crf, acodec, ab, resolution
    )

    encode_start = time.time()

    try:
        run_with_progress(
            encode_cmd,
            get_duration(input_video),
            f"Encoding {input_video.name}",
        )

    except RuntimeError as e:
        console.print(f"[bold red]Encoding failed: {e}[/bold red]")
        raise typer.Exit(1)

    encode_time = format_time(time.time() - encode_start)

    output_size = output_video.stat().st_size
    reduction = (1 - output_size / input_size) * 100

    table = Table(title="Results")
    table.add_column("Video", style="cyan", justify="center")
    table.add_column("Size Reduction", justify="center")
    table.add_column("Encode Time", justify="center")

    row = [
        Text(output_video.name, justify="left"),
        Text(
            f"{size_converter(input_size)} → {size_converter(output_size)} "
            f"({reduction:.0f}%)",
            justify="right",
        ),
        Text(encode_time, justify="right"),
    ]

    if compare:
        table.add_column("VMAF", justify="center")
        vmaf_cmd = build_vmaf_cmd(output_video, input_video)
        score = run_vmaf(
            vmaf_cmd, get_duration(input_video), f"Scoring {output_video.name}"
        )
        row.append(Text(str(score), justify="right"))

    table.add_row(*row)
    console.print(table)


@app.command(help="Encode multiple videos")
def batch(
    input_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True
    ),
    vcodec: VideoCodecs = VideoCodecs.libx264,
    crf: int = typer.Option(23, min=0, max=63),
    # optional vcodec parameters
    acodec: AudioCodecs = AudioCodecs.copy,
    ab: Optional[str] = None,
    resolution: Optional[str] = None,
    compare: bool = False,
    overwrite: bool = False,
    recursive: bool = False,
    output_dir: Optional[Path] = typer.Option(None, file_okay=False, resolve_path=True),
):
    if output_dir is None:
        output_dir = input_dir

    crf = clamp_crf(crf, vcodec)

    input_videos = find_videos(input_dir, recursive)
    output_videos = make_output_paths(input_dir, output_dir, input_videos, vcodec, crf)

    input_sizes = []
    output_sizes = []
    size_reductions = []
    encode_times = []
    vmaf_scores = []
    vmaf_times = []

    if not overwrite:
        for output_file in output_videos:
            if output_file.exists():
                typer.confirm(f"{output_file} already exists. Overwrite?", abort=True)
                print("\033[A\033[2K", end="")

    for i, (input_video, output_video) in enumerate(
        zip(input_videos, output_videos), start=1
    ):
        encode_cmd = build_encode_cmd(
            input_video, output_video, vcodec, crf, acodec, ab, resolution
        )

        encode_start = time.time()

        try:
            run_with_progress(
                encode_cmd,
                get_duration(input_video),
                f"Encoding [{i}/{len(input_videos)}] {input_video.name}",
            )
            input_sizes.append(input_video.stat().st_size)
            encode_times.append(format_time(time.time() - encode_start))
            output_sizes.append(output_video.stat().st_size)

            if compare:
                vmaf_start = time.time()
                vmaf_cmd = build_vmaf_cmd(output_video, input_video)
                score = run_vmaf(
                    vmaf_cmd,
                    get_duration(input_video),
                    f"Scoring [{i}/{len(input_videos)}] {output_video}",
                )
                vmaf_scores.append(score)
                vmaf_times.append(format_time(time.time() - vmaf_start))

        except RuntimeError as e:
            console.print(f"[yellow]Skipping {input_video.name}: {e}[/yellow]")
            continue

    table = Table(title="Results")
    table.add_column("Video", style="cyan", justify="center")
    table.add_column("Size Reduction", justify="center")
    table.add_column("Encode Time", justify="center")

    if compare:
        table.add_column("VMAF", justify="center")
        table.add_column("Score Time")

    for in_size, out_size in zip(input_sizes, output_sizes):
        size_reductions.append((1 - out_size / in_size) * 100)

    for vid, in_size, out_size, reduction, t in zip(
        output_videos, input_sizes, output_sizes, size_reductions, encode_times
    ):
        row = [
            Text(vid.name, justify="left"),
            Text(
                f"{size_converter(in_size)} → {size_converter(out_size)} "
                f"({reduction:.0f}%)",
                justify="right",
            ),
            Text(t, justify="right"),
        ]
        if compare:
            row.append(Text(str(vmaf_scores.pop(0)), justify="right"))
            row.append(Text(str(vmaf_times.pop(0)), justify="right"))
        table.add_row(*row)

    console.print(table)


@app.command(help="Find the optimal CRF for a target VMAF score")
def sweep(
    input_video: Path = typer.Argument(
        ..., exists=True, dir_okay=False, resolve_path=True
    ),
    vcodec: VideoCodecs = VideoCodecs.libx264,
    # optional vcodec parameters
    target_vmaf: float = typer.Option(93.0, min=0.1, max=100),
    crf_min: int = typer.Option(23, min=0, max=62),
    crf_max: int = typer.Option(32, min=1, max=63),
    acodec: AudioCodecs = AudioCodecs.copy,
    ab: Optional[str] = None,
    resolution: Optional[str] = None,
    output_video: Path = typer.Argument(..., dir_okay=False, resolve_path=True),
):
    if output_video.exists():
        typer.confirm(f"{output_video} already exists. Overwrite?", abort=True)
        print("\033[A\033[2K", end="")

    crf_min, crf_max = clamp_sweep_crf(crf_min, crf_max, vcodec)

    input_size = input_video.stat().st_size

    try:
        crf = sweeping(input_video, vcodec, target_vmaf, crf_min, crf_max)

    except RuntimeError as e:
        console.print(f"[bold red]Sweep failed: {e}[/bold red]")
        raise typer.Exit(1)

    cmd = build_encode_cmd(
        input_video, output_video, vcodec, crf, acodec, ab, resolution
    )

    encode_start = time.time()

    try:
        run_with_progress(
            cmd, get_duration(input_video), f"Encoding {input_video.name}"
        )

    except RuntimeError as e:
        console.print(f"[bold red]Encoding failed: {e}[/bold red]")
        raise typer.Exit(1)

    encode_time = format_time(time.time() - encode_start)

    output_size = output_video.stat().st_size
    reduction = (1 - output_size / input_size) * 100

    table = Table(title="Results")
    table.add_column("Video", style="cyan", justify="center")
    table.add_column("Size Reduction", justify="center")
    table.add_column("CRF", justify="center")
    table.add_column("Encode Time", justify="center")

    table.add_row(
        Text(output_video.name, justify="left"),
        Text(
            f"{size_converter(input_size)} → {size_converter(output_size)} "
            f"({reduction:.0f}%)",
            justify="right",
        ),
        Text(str(crf), justify="right"),
        Text(encode_time, justify="right"),
    )
    console.print(table)


@app.command(help="Find optimal CRF and encode multiple videos")
def batch_sweep(
    input_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True
    ),
    vcodec: VideoCodecs = VideoCodecs.libx264,
    # optional vcodec parameters
    target_vmaf: float = typer.Option(93.0, min=0.1, max=100),
    crf_min: int = typer.Option(23, min=0, max=62),
    crf_max: int = typer.Option(32, min=1, max=63),
    acodec: AudioCodecs = AudioCodecs.copy,
    ab: Optional[str] = None,
    resolution: Optional[str] = None,
    overwrite: bool = False,
    recursive: bool = False,
    output_dir: Optional[Path] = typer.Option(None, file_okay=False, resolve_path=True),
):
    if output_dir is None:
        output_dir = input_dir

    crf_min, crf_max = clamp_sweep_crf(crf_min, crf_max, vcodec)

    input_videos = find_videos(input_dir, recursive)
    output_videos = []
    successful_videos = []
    crfs = []

    for input_video in input_videos:
        try:
            crfs.append(sweeping(input_video, vcodec, target_vmaf, crf_min, crf_max))
            successful_videos.append(input_video)

        except RuntimeError as e:
            console.print(f"[yellow]Skipping {input_video.name}: {e}[/yellow]")
            continue

    input_videos = successful_videos
    output_videos = make_output_paths(input_dir, output_dir, input_videos, vcodec, crfs)

    if not overwrite:
        for output_file in output_videos:
            if output_file.exists():
                typer.confirm(f"{output_file} already exists. Overwrite?", abort=True)
                print("\033[A\033[2K", end="")

    input_sizes = []
    output_sizes = []
    size_reductions = []
    encode_times = []

    for i, (input_video, output_video, c) in enumerate(
        zip(input_videos, output_videos, crfs), start=1
    ):
        encode_cmd = build_encode_cmd(
            input_video, output_video, vcodec, c, acodec, ab, resolution
        )

        encode_start = time.time()

        try:
            run_with_progress(
                encode_cmd,
                get_duration(input_video),
                f"Encoding [{i}/{len(input_videos)}] {input_video.name}",
            )
            input_sizes.append(input_video.stat().st_size)
            encode_times.append(format_time(time.time() - encode_start))
            output_sizes.append(output_video.stat().st_size)

        except RuntimeError as e:
            console.print(f"[yellow]Skipping {input_video.name}: {e}[/yellow]")
            continue

    for in_size, out_size in zip(input_sizes, output_sizes):
        size_reductions.append((1 - out_size / in_size) * 100)

    table = Table(title="Results")
    table.add_column("Video", style="cyan", justify="center")
    table.add_column("Size Reduction", justify="center")
    table.add_column("CRF", justify="center")
    table.add_column("Encode Time", justify="center")

    for vid, in_size, out_size, reduction, c, t in zip(
        output_videos, input_sizes, output_sizes, size_reductions, crfs, encode_times
    ):
        table.add_row(
            Text(vid.name, justify="left"),
            Text(
                f"{size_converter(in_size)} → {size_converter(out_size)} "
                f"({reduction:.0f}%)",
                justify="right",
            ),
            Text(str(c), justify="right"),
            Text(t, justify="right"),
        )

    console.print(table)


if __name__ == "__main__":
    app()
