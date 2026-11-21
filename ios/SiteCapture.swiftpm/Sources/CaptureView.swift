import SwiftUI
import AVKit
import PhotosUI
import UniformTypeIdentifiers

struct PickedMovie: Transferable {
    let url: URL
    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(contentType: .movie) { m in SentTransferredFile(m.url) } importing: { received in
            let ext = received.file.pathExtension.isEmpty ? "mov" : received.file.pathExtension
            let dest = FileManager.default.temporaryDirectory.appendingPathComponent("picked-\(UUID().uuidString).\(ext)")
            try FileManager.default.copyItem(at: received.file, to: dest)
            return PickedMovie(url: dest)
        }
    }
}

struct CaptureView: View {
    @EnvironmentObject var state: AppState
    @StateObject private var cam = CameraModel()
    @State private var pickerItem: PhotosPickerItem?
    @State private var clip: URL?
    @State private var player: AVPlayer?
    @AppStorage("sheet") private var sheet = ""
    @AppStorage("selectedTab") private var tab = 0

    var body: some View {
        NavigationStack {
            ZStack {
                BlueprintGrid().ignoresSafeArea()
                VStack(spacing: 16) {
                    viewfinder
                    if let clip { reviewControls(clip) } else { recordControls }
                    sheetField
                    if let e = state.error ?? cam.error {
                        Text(e).font(.footnote).foregroundStyle(Theme.red).multilineTextAlignment(.center)
                    }
                    Spacer(minLength: 0)
                }
                .padding(16)
            }
            .navigationTitle("Capture")
            .navigationBarTitleDisplayMode(.inline)
        }
        .task {
            // Demo/test hook for the simulator: `-clipPath /abs/file.mp4 [-autoCheck YES]` loads a clip as if picked.
            if let p = UserDefaults.standard.string(forKey: "clipPath"), clip == nil, FileManager.default.fileExists(atPath: p) {
                let u = URL(fileURLWithPath: p)
                setClip(u)
                if UserDefaults.standard.bool(forKey: "autoCheck") {
                    if await state.upload(u) { discard(); tab = 1 }
                }
            }
            await cam.start()
        }
        .onDisappear { cam.stopSession() }
        .onChange(of: cam.recorded) { _, new in if let new { setClip(new) } }
        .onChange(of: pickerItem) { _, item in
            guard let item else { return }
            Task {
                if let m = try? await item.loadTransferable(type: PickedMovie.self) { setClip(m.url) }
                pickerItem = nil
            }
        }
    }

    private func setClip(_ url: URL) {
        clip = url
        player = AVPlayer(url: url)
    }

    private func discard() {
        player?.pause(); player = nil; clip = nil; cam.recorded = nil; state.error = nil
    }

    // MARK: pieces

    @ViewBuilder private var viewfinder: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 20).fill(Color.black)
            if let player {
                VideoPlayer(player: player).clipShape(RoundedRectangle(cornerRadius: 20))
            } else if cam.available {
                CameraPreview(session: cam.session).clipShape(RoundedRectangle(cornerRadius: 20))
            } else {
                VStack(spacing: 10) {
                    Image(systemName: cam.denied ? "video.slash" : "camera.metering.unknown")
                        .font(.system(size: 40)).foregroundStyle(Theme.blue)
                    Text(cam.denied ? "Camera or microphone access denied. Enable it in Settings > SiteCapture."
                                    : "No camera available (simulator?). Pick a video from Photos instead.")
                        .font(.callout).multilineTextAlignment(.center).foregroundStyle(.white.opacity(0.85))
                        .padding(.horizontal, 24)
                }
            }
            if cam.isRecording {
                VStack { HStack {
                    Label("REC", systemImage: "circle.fill").font(.caption.weight(.heavy))
                        .foregroundStyle(.white).padding(8)
                        .background(Theme.red, in: Capsule())
                    Spacer()
                }; Spacer() }.padding(12)
            }
            if state.busy && clip != nil {
                Color.black.opacity(0.55).clipShape(RoundedRectangle(cornerRadius: 20))
                VStack(spacing: 10) {
                    ProgressView(value: state.uploadProgress < 1 ? state.uploadProgress : nil).tint(Theme.blue).frame(width: 180)
                    Text(state.uploadProgress < 1 ? "Uploading \(Int(state.uploadProgress * 100))%" : "Analysing clip...")
                        .font(.callout.weight(.semibold))
                }
            }
        }
        .overlay(RoundedRectangle(cornerRadius: 20).stroke(Theme.blue.opacity(0.6), lineWidth: 1.5))
        .aspectRatio(3.0 / 4.0, contentMode: .fit)
        .frame(maxHeight: 460)
    }

    private var recordControls: some View {
        HStack(spacing: 40) {
            PhotosPicker(selection: $pickerItem, matching: .videos) {
                VStack(spacing: 4) {
                    Image(systemName: "photo.on.rectangle").font(.title2)
                    Text("Photos").font(.caption2)
                }.frame(width: 56)
            }
            recordButton
            Color.clear.frame(width: 56, height: 1)
        }
    }

    private var recordButton: some View {
        Button { cam.toggle() } label: {
            ZStack {
                Circle().stroke(Theme.blue.opacity(0.3), lineWidth: 6)
                Circle().trim(from: 0, to: cam.elapsed / CameraModel.maxSeconds)
                    .stroke(Theme.red, style: StrokeStyle(lineWidth: 6, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                RoundedRectangle(cornerRadius: cam.isRecording ? 8 : 34)
                    .fill(Theme.red)
                    .frame(width: cam.isRecording ? 32 : 68, height: cam.isRecording ? 32 : 68)
                    .animation(.easeInOut(duration: 0.15), value: cam.isRecording)
                if cam.isRecording {
                    Text("\(Int(ceil(CameraModel.maxSeconds - cam.elapsed)))")
                        .font(.caption.monospacedDigit().weight(.bold)).foregroundStyle(.white)
                        .offset(y: 54)
                }
            }
            .frame(width: 84, height: 84)
        }
        .disabled(!cam.available)
        .opacity(cam.available ? 1 : 0.4)
        .accessibilityLabel(cam.isRecording ? "Stop recording" : "Start recording")
    }

    private func reviewControls(_ url: URL) -> some View {
        HStack(spacing: 12) {
            Button("Retake", role: .cancel) { discard() }
                .foregroundStyle(Theme.ink)
                .padding(.vertical, 14).frame(maxWidth: .infinity)
                .background(Theme.paper)
                .overlay(Rectangle().stroke(Theme.ink, lineWidth: 1.5))
                .disabled(state.busy)
            PrimaryButton(title: state.busy ? "Checking..." : "Check", systemImage: "checkmark.seal.fill", disabled: state.busy) {
                Task {
                    if await state.upload(url) { discard(); tab = 1 }
                }
            }
        }
    }

    private var sheetField: some View {
        HStack {
            Text("SHEET").font(.system(size: 11, weight: .bold)).tracking(1).foregroundStyle(Theme.ink2)
            TextField("hint, e.g. P101 (optional)", text: $sheet)
                .textInputAutocapitalization(.characters).autocorrectionDisabled()
                .foregroundStyle(Theme.ink)
        }
        .padding(12)
        .background(Theme.paper)
        .overlay(Rectangle().stroke(Theme.line, lineWidth: 1))
    }
}
