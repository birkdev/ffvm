from typing import Optional
from enum import Enum
from tempfile import TemporaryDirectory
from shutil import which
from rich.progress import Progress
from rich.console import Console
from rich.table import Table
from pathlib import Path
import threading
import typer
import subprocess
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


def make_output_paths(
    input_dir,
    output_dir,
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


def run_with_progress(
    command, duration, description, capture_stderr=False
) -> str | None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
        text=True,
    )
    stderr = []

    if capture_stderr:
        stderr_thread = threading.Thread(target=read_stderr, args=(process, stderr))
        stderr_thread.start()
        progress_bar(duration, description, process)
        stderr_thread.join()

    else:
        progress_bar(duration, description, process)

    return "".join(stderr) if capture_stderr else None


def run_vmaf(command, duration, description):
    output = run_with_progress(command, duration, description, capture_stderr=True)

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

            for test_video in test_videos:
                output_video = tmp / f"{test_video.stem}_crf{crf}.mp4"
                cmd = build_encode_cmd(test_video, output_video, vcodec, crf)
                run_with_progress(cmd, get_duration(test_video), "Encoding...")

                cmd = build_vmaf_cmd(output_video, test_video)
                score = run_vmaf(cmd, get_duration(test_video), "Scoring...")
                vmaf_scores.append(score)

            average_vmaf = sum(vmaf_scores) / len(vmaf_scores)

            if average_vmaf > target_vmaf:
                crf_min = crf
            else:
                crf_max = crf
            crf = round((crf_min + crf_max) / 2)

    return crf


@app.command()
def encode(
    input_video: Path = typer.Argument(
        ..., exists=True, dir_okay=False, resolve_path=True
    ),
    vcodec: VideoCodecs = VideoCodecs.libx264,
    crf: int = 23,
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

    encode_cmd = build_encode_cmd(
        input_video, output_video, vcodec, crf, acodec, ab, resolution
    )

    run_with_progress(
        encode_cmd,
        get_duration(input_video),
        "Encoding...",
    )

    if compare:
        vmaf_cmd = build_vmaf_cmd(output_video, input_video)
        score = run_vmaf(vmaf_cmd, get_duration(input_video), "Scoring...")
        console.print(
            f"[bold green]Successfully encoded[/bold green] [cyan]{input_video}[/cyan]"
        )
        console.print(f"[bold green]VMAF score:[/bold green] [cyan]{score}[/cyan]")

    else:
        console.print(
            f"[bold green]Successfully encoded[/bold green] [cyan]{input_video}[/cyan]"
        )


@app.command()
def batch(
    input_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True
    ),
    vcodec: VideoCodecs = VideoCodecs.libx264,
    crf: int = 23,
    # optional vcodec parameters here soon
    acodec: AudioCodecs = AudioCodecs.copy,
    ab: Optional[str] = None,
    resolution: Optional[str] = None,
    compare: bool = False,
    # verbose: bool = False,
    overwrite: bool = False,
    recursive: bool = False,
    output_dir: Optional[Path] = typer.Option(None, file_okay=False, resolve_path=True),
):
    if output_dir is None:
        output_dir = input_dir

    input_videos = find_videos(input_dir, recursive)
    output_videos = make_output_paths(input_dir, output_dir, input_videos, vcodec, crf)
    vmaf_scores = []

    if not overwrite:
        for output_file in output_videos:
            if output_file.exists():
                typer.confirm(f"{output_file} already exists. Overwrite?", abort=True)
                print("\033[A\033[2K", end="")

    for input_video, output_video in zip(input_videos, output_videos):
        encode_cmd = build_encode_cmd(
            input_video, output_video, vcodec, crf, acodec, ab, resolution
        )

        run_with_progress(
            encode_cmd,
            get_duration(input_video),
            "Encoding...",
        )

        if compare:
            vmaf_cmd = build_vmaf_cmd(output_video, input_video)
            score = run_vmaf(vmaf_cmd, get_duration(input_video), "Scoring...")
            vmaf_scores.append(score)

    console.print(
        "[bold green]Successfully encoded[/bold green] [cyan]all videos[/cyan]"
    )
    console.print()

    if compare:
        table = Table(title="VMAF Scores")
        table.add_column("Video", style="cyan")
        table.add_column("Score", style="magenta", justify="right")

        for output_video, score in zip(output_videos, vmaf_scores):
            table.add_row(output_video.name, str(score))

        console.print(table)


@app.command()
def sweep(
    input_video: Path = typer.Argument(
        ..., exists=True, dir_okay=False, resolve_path=True
    ),
    vcodec: VideoCodecs = VideoCodecs.libx264,
    # optional vcodec parameters
    target_vmaf: float = 93.0,
    crf_min: int = 23,
    crf_max: int = 32,
    acodec: AudioCodecs = AudioCodecs.copy,
    ab: Optional[str] = None,
    resolution: Optional[str] = None,
    output_video: Path = typer.Argument(..., dir_okay=False, resolve_path=True),
):
    if output_video.exists():
        typer.confirm(f"{output_video} already exists. Overwrite?", abort=True)
        print("\033[A\033[2K", end="")

    crf = sweeping(input_video, vcodec, target_vmaf, crf_min, crf_max)

    cmd = build_encode_cmd(
        input_video, output_video, vcodec, crf, acodec, ab, resolution
    )
    run_with_progress(cmd, get_duration(input_video), "Encoding...")

    console.print(
        f"[bold green]Successfully encoded[/bold green] [cyan]{input_video}[/cyan]"
    )


@app.command()
def batch_sweep(
    input_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False, resolve_path=True
    ),
    vcodec: VideoCodecs = VideoCodecs.libx264,
    # optional vcodec parameters
    target_vmaf: float = 93.0,
    crf_min: int = 23,
    crf_max: int = 32,
    acodec: AudioCodecs = AudioCodecs.copy,
    ab: Optional[str] = None,
    resolution: Optional[str] = None,
    # verbose: bool = False,
    overwrite: bool = False,
    recursive: bool = False,
    output_dir: Optional[Path] = typer.Option(None, file_okay=False, resolve_path=True),
):
    if output_dir is None:
        output_dir = input_dir

    input_videos = find_videos(input_dir, recursive)
    output_videos = []
    crfs = []

    for input_video in input_videos:
        crfs.append(sweeping(input_video, vcodec, target_vmaf, crf_min, crf_max))

    output_videos = make_output_paths(input_dir, output_dir, input_videos, vcodec, crfs)

    if not overwrite:
        for output_file in output_videos:
            if output_file.exists():
                typer.confirm(f"{output_file} already exists. Overwrite?", abort=True)
                print("\033[A\033[2K", end="")

    for input_video, output_video, c in zip(input_videos, output_videos, crfs):
        encode_cmd = build_encode_cmd(
            input_video, output_video, vcodec, c, acodec, ab, resolution
        )

        run_with_progress(
            encode_cmd,
            get_duration(input_video),
            "Encoding...",
        )

    console.print(
        "[bold green]Successfully encoded[/bold green] [cyan]all videos[/cyan]"
    )


if __name__ == "__main__":
    app()
