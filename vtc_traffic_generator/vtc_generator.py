import ffmpeg
import subprocess
import pulsectl
import os
import signal
import sys
import time
import json
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCServer

class VtcClient:
    print("Creating client object")
    def __init__(self, ip='0.0.0.0', port=100):
        self.ip = ip
        self.port = int(port)
        self.video_pid = 0


# Initialize client, call from controller
# Set pulse audio devices and device volume
# Check for v4l2 kernel mod
def initialize_vtc_client():
    print ("Initializing VTC client")

    # Set up audio devices
    pulse = pulsectl.Pulse()
    sinks = pulse.sink_list()
    sources = pulse.source_list()

    if "name='virtual_speaker'" not in str(sinks) or "name='virtual_mic'" not in str(sources):
        print("Creating PulseAudio virtual devices")
        subprocess.run('pactl load-module module-null-sink sink_name="virtual_speaker" sink_properties=device.description="virtual_speaker"', capture_output=True, shell=True)
        subprocess.run('pactl load-module module-remap-source master="virtual_speaker.monitor" source_name="virtual_mic" source_properties=device.description="virtual_mic"', capture_output=True, shell=True)

    # Set volume levels and unmute devices
    for sink in sinks:
        pulse.volume_set_all_chans(sink, .7)
        pulse.mute(sink, False)

    for source in sources:
        pulse.volume_set_all_chans(source, .7)
        pulse.mute(source, False)

    # Check for v4l2 virtual webcam kernel module
    if 'v4l2loopback' not in str(subprocess.run(['lsmod'], capture_output=True)):
        print("Error: v4l2loopback kernel module not loaded. Try: sudo modprobe v4l2loopback video_nr=5 exclusive_caps=1")

    print(config['bot_name'] + " configured.")


# XMLRPC needed
def play_audio():
    try:
        process = (
            ffmpeg
                .input('/home/dave/Desktop/line_9.flac')
                .output('virtual_speaker', format='pulse', device='virtual_speaker')
        )
        process = process.run(capture_stdout=True, capture_stderr=True)

    except ffmpeg.Error as e:
        print('stdout:', e.stdout.decode('utf8'))
        print('stderr:', e.stderr.decode('utf8'))
        raise e


# XMLRPC needed
def play_video():
    # Setup streaming from file to v4l2 device
    try:
        process = (
            ffmpeg
                .input('man_1_270.mp4', re=None, stream_loop=-1)
                .filter('format', 'yuv420p')
                .drawtext(text=config['bot_name'], x='(w-text_w)/2', y='h-th-20', fontcolor='red', fontsize=50)
                .output('/dev/video5', format='v4l2')
        )

        # Launch video playback
        print("Launching video playback")
        process = process.run_async(pipe_stdin=True)

    except ffmpeg.Error as e:
        print('stdout:', e.stdout.decode('utf8'))
        print('stderr:', e.stderr.decode('utf8'))
        raise e

    return process.pid


# XMLRPC
def stop_video(video_pid):
    print("Stopping video")
    os.kill(video_pid, signal.SIGTERM)


def run_controller():

    num_clients = len(config['vtc_clients'])
    VTC_clients = []

    # Instantiate clients
    for x in range(num_clients):

        # Create VTC client
        VTC_clients.append(VtcClient(config['vtc_clients'][x][0], config['vtc_clients'][x][1]))

        # Initialize client devices
        uri = 'http://' + VTC_clients[x].ip + ':' + str(VTC_clients[x].port)
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.initialize_vtc_client()

        # Start client video stream to virtual camera device
        with xmlrpc.client.ServerProxy(uri) as proxy:
            VTC_clients[x].video_pid = proxy.play_video()

        # Testing Video stopping capability
        time.sleep(15)

        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.stop_video(VTC_clients[x].video_pid)

    # Begin client dialog

    print("DONE")


def run_client(config):
    # Register functions and respond to calls indefinitely
    server = SimpleXMLRPCServer(("0.0.0.0", config['c2_port']), allow_none=True)
    print("Listening on port: " + str(config['c2_port']))

    server.register_function(initialize_vtc_client, "initialize_vtc_client")
    server.register_function(play_video, "play_video")
    server.register_function(stop_video, "stop_video")

    server.serve_forever()


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("Error: must specify configuration JSON file.")
        exit()

    # read config
    with open(sys.argv[1], 'r') as infile:
        config = json.load(infile)

    print(config['version'])
    print ("Role: " + config['role'])

    if config['role'] == 'controller':
        run_controller()

    elif config['role'] == 'client':
        run_client(config)
