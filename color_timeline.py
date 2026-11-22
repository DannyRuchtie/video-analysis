#!/usr/bin/env python3
"""Save a duration-proportional PNG color strip and a JSON shot palette."""

from argparse import ArgumentParser
import struct
import zlib

from video import VideoError, command_arguments, inspect_video, output_directory, write_json


def average_colors(timeline):
    colors = []
    ends = [*timeline.times[1:], timeline.duration]
    for shot in timeline.shots:
        totals = [0.0, 0.0, 0.0]
        for frame in range(shot.start_frame, shot.end_frame):
            interval = ends[frame] - timeline.times[frame]
            pixel = frame * 3
            for channel in range(3):
                totals[channel] += timeline.pixels[pixel + channel] * interval
        duration = shot.end - shot.start
        rgb = tuple(max(0, min(255, round(total / duration))) for total in totals)
        colors.append(rgb)
    return colors


def color_hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def write_color_strip(path, timeline, colors, width=2400, height=160):
    count = len(timeline.shots)
    width = max(width, count)
    row = bytearray()
    for shot, rgb in zip(timeline.shots, colors):
        start = len(row) // 3
        ideal_end = round(shot.end / timeline.duration * width)
        # Give every shot a pixel, even in films with very short cuts.
        end = min(max(ideal_end, start + 1), width - (count - shot.number))
        row.extend(bytes(rgb) * (end - start))

    header = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress((b"\x00" + row) * height))
        + png_chunk(b"IEND", b"")
    )


def main():
    parser = ArgumentParser(description=__doc__)
    args = command_arguments(parser, "color-timeline")
    try:
        timeline = inspect_video(args.video, args.threshold, args.min_shot, with_colors=True)
        colors = average_colors(timeline)
        with output_directory(args.output) as output:
            write_color_strip(output / "color-strip.png", timeline, colors)
            write_json(output / "colors.json", {
                "duration": timeline.duration,
                "shots": [
                    {**shot.details(), "rgb": rgb, "color": color_hex(rgb)}
                    for shot, rgb in zip(timeline.shots, colors)
                ],
            })
    except (OSError, VideoError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(f"Saved {len(colors)} shot colors to {args.output.resolve()}")


if __name__ == "__main__":
    main()
