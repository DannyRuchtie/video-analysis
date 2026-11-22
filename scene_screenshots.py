#!/usr/bin/env python3
"""Save one representative JPEG and its timestamp for every detected shot."""

from argparse import ArgumentParser

from video import VideoError, command_arguments, extract_frames, inspect_video, output_directory, write_json


def screenshot_manifest(timeline):
    return [
        {**shot.details(), "image": shot.filename}
        for shot in timeline.shots
    ]


def main():
    parser = ArgumentParser(description=__doc__)
    args = command_arguments(parser, "scene-screenshots")
    try:
        timeline = inspect_video(args.video, args.threshold, args.min_shot)
        with output_directory(args.output) as output:
            extract_frames(args.video, timeline, output)
            write_json(output / "shots.json", {
                "duration": timeline.duration,
                "shots": screenshot_manifest(timeline),
            })
    except (OSError, VideoError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(f"Saved {len(timeline.shots)} screenshots to {args.output.resolve()}")


if __name__ == "__main__":
    main()
