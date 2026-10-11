// Sends this account's sound to the PA on the headphone jack, through the Multi-Output device
// "Orchid PA": BlackHole 2ch (master, so Hermes' WhatsApp jams can record Studio; Pistil's
// AVAudioEngine writes only to the master leg) plus External Headphones. Creates the device
// if it is missing, then makes it the default output.
// Exit status: 0 done, 2 nothing in the headphone jack, 3 BlackHole 2ch not installed.
import Foundation
import CoreAudio

let name = "Orchid PA"
let uid = "com.orchid.demo.pa"

func address(_ selector: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}
func devices() -> [AudioDeviceID] {
    var a = address(kAudioHardwarePropertyDevices); var n: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &n)
    var ids = [AudioDeviceID](repeating: 0, count: Int(n) / MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &n, &ids)
    return ids
}
func string(_ id: AudioDeviceID, _ selector: AudioObjectPropertySelector) -> String {
    var a = address(selector); var cf: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    let err = withUnsafeMutablePointer(to: &cf) { AudioObjectGetPropertyData(id, &a, 0, nil, &size, $0) }
    return err == noErr ? (cf as String) : ""
}
func builtIn(_ id: AudioDeviceID) -> Bool {
    var a = address(kAudioDevicePropertyTransportType)
    var transport: UInt32 = 0; var size = UInt32(MemoryLayout<UInt32>.size)
    return AudioObjectGetPropertyData(id, &a, 0, nil, &size, &transport) == noErr
        && transport == kAudioDeviceTransportTypeBuiltIn
}
func volume(_ id: AudioDeviceID) -> Float32? {
    for element: AudioObjectPropertyElement in [0, 1] {
        var a = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyVolumeScalar,
                                           mScope: kAudioDevicePropertyScopeOutput, mElement: element)
        var value: Float32 = 0; var size = UInt32(MemoryLayout<Float32>.size)
        if AudioObjectGetPropertyData(id, &a, 0, nil, &size, &value) == noErr { return value }
    }
    return nil
}

var blackHole: AudioDeviceID?, jack: AudioDeviceID?, pa: AudioDeviceID?
for id in devices() {
    let n = string(id, kAudioObjectPropertyName)
    if n.contains("BlackHole 2ch") { blackHole = id }
    if n.contains("Headphones") && builtIn(id) { jack = id }
    if string(id, kAudioDevicePropertyDeviceUID) == uid { pa = id }
}
guard let blackHole else { print("BlackHole 2ch is not installed"); exit(3) }
guard let jack else { print("nothing is plugged into the headphone jack"); exit(2) }

if pa == nil {
    let blackHoleUID = string(blackHole, kAudioDevicePropertyDeviceUID)
    let description: [String: Any] = [
        kAudioAggregateDeviceNameKey: name,
        kAudioAggregateDeviceUIDKey: uid,
        kAudioAggregateDeviceSubDeviceListKey: [
            [kAudioSubDeviceUIDKey: blackHoleUID, kAudioSubDeviceDriftCompensationKey: 1],
            [kAudioSubDeviceUIDKey: string(jack, kAudioDevicePropertyDeviceUID), kAudioSubDeviceDriftCompensationKey: 1],
        ],
        kAudioAggregateDeviceMasterSubDeviceKey: blackHoleUID,
        kAudioAggregateDeviceIsStackedKey: 1,
        kAudioAggregateDeviceIsPrivateKey: 0,
    ]
    var created = AudioDeviceID(0)
    let err = AudioHardwareCreateAggregateDevice(description as CFDictionary, &created)
    if err != noErr { print("could not create \(name) (error \(err))"); exit(1) }
    pa = created
}

var a = address(kAudioHardwarePropertyDefaultOutputDevice)
var device = pa!
let err = AudioObjectSetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil,
                                     UInt32(MemoryLayout<AudioDeviceID>.size), &device)
if err != noErr { print("could not select \(name) (error \(err))"); exit(1) }
let level = volume(jack).map { " at \(Int(($0 * 100).rounded()))% volume" } ?? ""
print("Sound goes to the PA (\(string(jack, kAudioObjectPropertyName))\(level))")
