import SwiftUI
@preconcurrency import AVFoundation
import UIKit

/// AVCaptureSession + AVCaptureMovieFileOutput, max 20 s.
@MainActor
final class CameraModel: NSObject, ObservableObject, AVCaptureFileOutputRecordingDelegate {
    static let maxSeconds: Double = 20

    let session = AVCaptureSession()
    private let output = AVCaptureMovieFileOutput()
    private var configured = false

    @Published var available = false
    @Published var denied = false
    @Published var isRecording = false
    @Published var elapsed: Double = 0
    @Published var recorded: URL?
    @Published var error: String?
    private var timer: Timer?

    func start() async {
        #if targetEnvironment(simulator)
        available = false
        return
        #else
        let video = await AVCaptureDevice.requestAccess(for: .video)
        let audio = await AVCaptureDevice.requestAccess(for: .audio)
        guard video, audio else { denied = true; return }
        if !configured { configure() }
        guard available else { return }
        let s = session
        DispatchQueue.global(qos: .userInitiated).async { if !s.isRunning { s.startRunning() } }
        #endif
    }

    func stopSession() {
        let s = session
        DispatchQueue.global(qos: .userInitiated).async { if s.isRunning { s.stopRunning() } }
    }

    private func configure() {
        configured = true
        guard let cam = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
              let vin = try? AVCaptureDeviceInput(device: cam) else { available = false; return }
        session.beginConfiguration()
        session.sessionPreset = .hd1280x720  // keeps 20 s well under the 50 MB limit
        if session.canAddInput(vin) { session.addInput(vin) }
        if let mic = AVCaptureDevice.default(for: .audio), let ain = try? AVCaptureDeviceInput(device: mic), session.canAddInput(ain) {
            session.addInput(ain)
        }
        if session.canAddOutput(output) { session.addOutput(output) }
        output.maxRecordedDuration = CMTime(seconds: Self.maxSeconds, preferredTimescale: 600)
        session.commitConfiguration()
        available = true
    }

    func toggle() {
        isRecording ? stopRecording() : startRecording()
    }

    func startRecording() {
        guard available, !output.isRecording else { return }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("clip-\(UUID().uuidString).mov")
        recorded = nil; error = nil; elapsed = 0
        output.startRecording(to: url, recordingDelegate: self)
        isRecording = true
        UIImpactFeedbackGenerator(style: .heavy).impactOccurred()
        let began = Date()
        timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.elapsed = min(Date().timeIntervalSince(began), Self.maxSeconds) }
        }
    }

    func stopRecording() {
        if output.isRecording { output.stopRecording() }
    }

    nonisolated func fileOutput(_ output: AVCaptureFileOutput, didFinishRecordingTo url: URL, from _: [AVCaptureConnection], error: Error?) {
        // maxRecordedDuration reached reports an error but the file is valid.
        let ok = (error as NSError?)?.userInfo[AVErrorRecordingSuccessfullyFinishedKey] as? Bool ?? (error == nil)
        Task { @MainActor in
            self.timer?.invalidate(); self.timer = nil
            self.isRecording = false
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            if ok { self.recorded = url } else { self.error = error?.localizedDescription }
        }
    }
}

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession
    func makeUIView(context: Context) -> PreviewView {
        let v = PreviewView()
        v.previewLayer.session = session
        v.previewLayer.videoGravity = .resizeAspectFill
        return v
    }
    func updateUIView(_ uiView: PreviewView, context: Context) {}

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }
}
