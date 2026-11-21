# SiteCapture (iPhone)

Records up to 20 s of site video + audio and sends it to our backend
(`POST /api/video`), then shows claim cards (conflict / consistent / unclear) with the
evidence crop and boxes. You can also type a claim (`POST /api/check`). The app talks only
to our backend; it holds no Gemini key or any other secret.

The project is a Swift Playgrounds-style app package, `SiteCapture.swiftpm`
(`Package.swift` with `.iOSApplication`). There is no hand-written `.pbxproj`.
SwiftUI, iOS 17+, no third-party dependencies.

## Run on your iPhone

1. Open `ios/SiteCapture.swiftpm` in Xcode (double-click it, or `open ios/SiteCapture.swiftpm`).
2. Select the `SiteCapture` target, Signing & Capabilities, and set your **Team**.
   If Xcode says the bundle id is taken, change it (e.g. `com.<you>.SiteCapture`) in the same
   screen.
3. Plug in the iPhone, trust the Mac, enable Developer Mode on the phone if asked
   (Settings > Privacy & Security), choose the phone as the run destination, press Run.
   First launch: Settings > General > VPN & Device Management > trust your developer profile.
4. Accept the Camera, Microphone and Local Network prompts.

## Backend on the same Wi-Fi

1. Find the Mac's LAN IP: `ipconfig getifaddr en0` (use `en1` if empty).
2. Start the backend bound to all interfaces. The backend currently binds `127.0.0.1`, which the
   phone cannot reach, so the launcher must use `--host 0.0.0.0`, for example
   `uvicorn app:app --host 0.0.0.0 --port 8000` (adjust to the real entry point).
3. In the app: Settings tab, set Backend to `http://<mac-ip>:8000`, tap **Test connection**
   (calls `GET /api/health`; shows cache mode and run spend). Optionally set a sheet hint
   (`P101`, `A201`, `E200`, or free text); it is sent as `?sheet=`.
4. Capture tab: tap the red button to record (countdown ring, auto-stops at 20 s, haptics on
   start and stop), review the clip, tap **Check**. Upload progress is shown, then the Results
   tab opens.

Cleartext HTTP to local addresses is allowed through `NSAllowsLocalNetworking`. For any
non-local host, use HTTPS.

## Simulator

The simulator has no camera, so the record button is disabled there. Use the **Photos** button
(PhotosPicker) to pick a video and upload it exactly as a recording would be. Add a video to the
simulator by dragging a `.mov`/`.mp4` onto its window. In the simulator, `localhost` reaches the Mac,
so the backend can stay on `127.0.0.1`.

Settings > **Mock responses (demo)** skips the network and loads 3 bundled sample cards, handy for
UI work. You can also launch with `-mock YES -selectedTab 1` to jump to the Results tab.

## Build from the command line

```
xcodebuild -workspace ios/SiteCapture.swiftpm -scheme SiteCapture \
  -destination 'generic/platform=iOS Simulator' build CODE_SIGNING_ALLOWED=NO
```

(`xcodebuild` has no `-packagePath`; pointing `-workspace` at the `.swiftpm` directory works.)

## Notes

- Clips are recorded at 720p so 20 s stays far below the backend's 50 MB cap; the app refuses
  larger picked files before uploading.
- Evidence boxes use `box_2d_on_crop = [ymin, xmin, ymax, xmax]` on a 0-1000 scale, drawn over the
  displayed crop image.
- Card timestamps come from the matching entry in `claims[]` (`t_start_s`).
