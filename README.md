# Video Trim

Trim and compress the PC's videos from the phone. The phone app is a remote control: the PC lists
the folders, streams the preview, runs ffmpeg and keeps the output.

- `server/trim_server.py`: the PC side. Python standard library + ffmpeg. Same cut and same output
  names as `video_trim_gui.py`. Settings in `server/config.json` (made on first start: port, key,
  browsable folders `roots`, output folder `out_dir`, name presets `poses`). Starts hidden through `launch-trim-hidden.vbs`.
- `ios/`: the SwiftUI app (XcodeGen project). In the app's Settings tab type the PC address
  (for example `192.168.1.10:8776`) and the key from `server/config.json`.
- Tests: `python server/tests/test_trim_server.py` (real server on a spare port, throwaway folder),
  `ios/Tests` (simulator tests against `mock_server.py`).

The source video is only read. An output name that already exists gets `_2`, `_3`, ...
